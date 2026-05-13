"""PR-AA: cert_provision sops re-encrypt tempfile name regression tests.

Pins the dogfood iter#8 (2026-05-13) failure mode where
`tempfile.mkstemp(prefix="secrets.", suffix=".plain", dir=config._dir)`
produced filenames like `secrets.XXXXXX.plain` that did NOT match
`.sops.yaml`'s `creation_rules[].path_regex: secrets\\.yaml$`. sops 3.12.x
resolves `.sops.yaml` rules from the working directory BEFORE honoring
`--gcp-kms` / `--age` on the command line, so a non-matching tempfile name
fails with `error loading config: no matching creation rules found`.

Fix: rename the tempfile so the produced name ends in `.secrets.yaml`,
which satisfies the `secrets\\.yaml$` regex.

Also exercises the M4 orphan-plaintext sweep added in PR-AA: any
`secrets.*.plain` files left behind by PR-Z's failed iter#8 must be
unlinked on entry to `_persist_secrets_after_reissue` so plaintext
secrets do not linger on disk.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from unittest import mock

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from scripts import pipeline as pl  # noqa: E402
from scripts.config import InstallerConfig  # noqa: E402
from scripts import secretmgr  # noqa: E402


DOMAIN = "dev.voipbin-install.example.com"
KMS = "projects/test/locations/global/keyRings/r/cryptoKeys/k"


def _make_cfg(tmp_path: Path) -> InstallerConfig:
    """Build an InstallerConfig pointed at a fresh tmp workdir with .sops.yaml."""
    secretmgr.write_sops_config(KMS, tmp_path)
    secrets_file = tmp_path / "secrets.yaml"
    secrets_file.write_text("ENCRYPTED_PLACEHOLDER\n")
    cfg = InstallerConfig(config_dir=tmp_path)
    cfg._data = {"cert_mode": "self_signed", "domain": DOMAIN}
    return cfg


def _capture_tempfile_name_passed_to_encrypt(
    tmp_path: Path, monkeypatch
) -> Path:
    """Drive _persist_secrets_after_reissue with a stubbed encrypt fn that
    captures the path argument it received. Returns that captured Path."""
    cfg = _make_cfg(tmp_path)
    captured: dict[str, Path] = {}

    def _stub_encrypt(path, kms_key_id):
        captured["path"] = Path(path)
        # Pretend success so os.replace runs (it will replace the placeholder
        # secrets.yaml with our captured tempfile bytes — fine for a test).
        return True

    monkeypatch.setattr(secretmgr, "encrypt_with_sops", _stub_encrypt)

    ok = pl._persist_secrets_after_reissue(cfg, {"foo": "bar"})
    assert ok is True, "stub returned True; persist should succeed"
    assert "path" in captured, "encrypt_with_sops was never invoked"
    return captured["path"]


class TestTempfileNameMatchesSopsPathRegex:
    """M1: load the regex from the live .sops.yaml output (the source of
    truth, NOT a hard-coded literal) and assert the produced tempfile name
    matches it."""

    def test_tempfile_name_matches_sops_path_regex(self, tmp_path, monkeypatch):
        captured = _capture_tempfile_name_passed_to_encrypt(tmp_path, monkeypatch)
        # Source of truth: read the rule's path_regex from the on-disk
        # .sops.yaml that secretmgr.write_sops_config produced.
        sops_yaml = tmp_path / ".sops.yaml"
        sops_cfg = yaml.safe_load(sops_yaml.read_text())
        rule_regex = sops_cfg["creation_rules"][0]["path_regex"]
        assert re.search(rule_regex, captured.name), (
            f"tempfile name {captured.name!r} does not match .sops.yaml's "
            f"path_regex {rule_regex!r} — sops will reject the encrypt call."
        )


class TestTempfileLocation:
    """M-baseline: the tempfile must live next to secrets.yaml under
    config._dir, NOT in /tmp. Putting it in /tmp would (a) violate the
    .sops.yaml rule resolution scope and (b) leak plaintext outside the
    operator's controlled workdir."""

    def test_tempfile_lives_in_config_dir(self, tmp_path, monkeypatch):
        captured = _capture_tempfile_name_passed_to_encrypt(tmp_path, monkeypatch)
        assert captured.parent == tmp_path, (
            f"tempfile created at {captured.parent}, expected under {tmp_path}"
        )


class TestTempfileCleanup:
    """The function must unlink its plaintext tempfile in BOTH success and
    failure paths so plaintext secrets never linger on disk."""

    def test_tempfile_cleaned_on_sops_failure(self, tmp_path, monkeypatch):
        cfg = _make_cfg(tmp_path)
        # encrypt_with_sops returns False → finally block must still unlink.
        monkeypatch.setattr(secretmgr, "encrypt_with_sops", lambda p, k: False)
        ok = pl._persist_secrets_after_reissue(cfg, {"k": "v"})
        assert ok is False
        # No `cert-staging-*.secrets.yaml` orphan should remain.
        residue = list(tmp_path.glob("cert-staging-*.secrets.yaml"))
        assert residue == [], f"orphan tempfiles after failure: {residue}"

    def test_tempfile_cleaned_on_sops_success(self, tmp_path, monkeypatch):
        cfg = _make_cfg(tmp_path)
        monkeypatch.setattr(secretmgr, "encrypt_with_sops", lambda p, k: True)
        ok = pl._persist_secrets_after_reissue(cfg, {"k": "v"})
        assert ok is True
        # The on-disk file is renamed to secrets.yaml via os.replace; no
        # cert-staging tempfile should remain.
        residue = list(tmp_path.glob("cert-staging-*.secrets.yaml"))
        assert residue == [], f"orphan tempfiles after success: {residue}"


class TestOrphanPlaintextSweep:
    """M4: PR-Z's broken naming pattern (`secrets.XXXXXX.plain`) could leave
    plaintext orphans on disk when the pipeline aborted mid-encrypt (which
    is exactly what happened in iter#8). The PR-AA sweep must remove them
    on entry, BEFORE the new encrypt attempt, so plaintext cannot linger
    across operator runs."""

    def test_orphan_secrets_plaintext_swept_on_entry(self, tmp_path, monkeypatch):
        cfg = _make_cfg(tmp_path)
        # Simulate iter#8 leftovers.
        orphan_a = tmp_path / "secrets.abc123.plain"
        orphan_b = tmp_path / "secrets.def456.plain"
        orphan_a.write_text("PLAINTEXT_SECRET_A\n")
        orphan_b.write_text("PLAINTEXT_SECRET_B\n")
        # Capture whether orphans existed at the moment encrypt was called.
        observed: dict[str, list[str]] = {"during_encrypt": []}

        def _stub_encrypt(path, kms_key_id):
            observed["during_encrypt"] = sorted(
                p.name for p in tmp_path.glob("secrets.*.plain")
            )
            return True

        monkeypatch.setattr(secretmgr, "encrypt_with_sops", _stub_encrypt)

        ok = pl._persist_secrets_after_reissue(cfg, {"k": "v"})
        assert ok is True
        # The sweep must have run BEFORE encrypt, so orphans were already gone.
        assert observed["during_encrypt"] == [], (
            f"orphan plaintext files survived into encrypt phase: "
            f"{observed['during_encrypt']}"
        )
        # And of course they must not exist after the call either.
        assert not orphan_a.exists()
        assert not orphan_b.exists()


# ---------------------------------------------------------------------------
# Real sops + age round-trip — the actual-execution gate.
# ---------------------------------------------------------------------------

_SOPS = shutil.which("sops")
_AGE_KEYGEN = shutil.which("age-keygen")


@pytest.mark.skipif(
    _SOPS is None or _AGE_KEYGEN is None,
    reason="real-sops round-trip requires both `sops` and `age-keygen` on PATH",
)
class TestRealSopsAgeRoundTrip:
    """M2: actual-execution gate. Run the REAL sops binary against a
    tempfile produced by `_persist_secrets_after_reissue`'s naming pattern
    and confirm sops accepts it. Uses an in-test age keypair so the test
    is deterministic in any CI sandbox that has the binaries installed
    (no GCP / KMS dependency).

    This is the test that would have caught dogfood iter#8 before merge if
    PR-Z had included it."""

    def test_real_sops_accepts_tempfile_named_per_pr_aa(self, tmp_path):
        # 1. Generate an age recipient.
        keyfile = tmp_path / "age.key"
        rc = subprocess.run(
            [_AGE_KEYGEN, "-o", str(keyfile)],
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert rc.returncode == 0, f"age-keygen failed: {rc.stderr}"
        # age-keygen prints the public recipient on the second line of stderr.
        recipient = None
        for line in (rc.stderr + rc.stdout).splitlines():
            line = line.strip()
            if line.startswith("# public key:"):
                recipient = line.split(":", 1)[1].strip()
                break
        assert recipient, f"could not extract age recipient from output:\n{rc.stderr}\n{rc.stdout}"

        # 2. Write a .sops.yaml with the SAME path_regex shape that
        #    write_sops_config produces in production.
        sops_yaml = tmp_path / ".sops.yaml"
        sops_yaml.write_text(yaml.safe_dump({
            "creation_rules": [
                {"path_regex": r"secrets\.yaml$", "age": recipient}
            ]
        }))

        # 3. Use the EXACT same tempfile pattern PR-AA uses in production.
        import tempfile
        fd, tmp_str = tempfile.mkstemp(
            prefix="cert-staging-", suffix=".secrets.yaml", dir=str(tmp_path),
        )
        os.close(fd)
        plaintext_path = Path(tmp_str)
        plaintext_path.write_text("foo: bar\nbaz: qux\n")

        # 4. Run real sops --encrypt --in-place from the workdir so
        #    .sops.yaml is discovered.
        env = os.environ.copy()
        env["SOPS_AGE_KEY_FILE"] = str(keyfile)
        rc = subprocess.run(
            [_SOPS, "--encrypt", "--in-place", str(plaintext_path)],
            cwd=str(tmp_path),
            env=env,
            capture_output=True,
            text=True,
            timeout=15,
        )
        assert rc.returncode == 0, (
            f"sops --encrypt rejected the PR-AA-named tempfile {plaintext_path.name!r}: "
            f"{rc.stderr}"
        )

        # 5. Round-trip via decrypt to confirm payload integrity.
        rc = subprocess.run(
            [_SOPS, "--decrypt", str(plaintext_path)],
            cwd=str(tmp_path),
            env=env,
            capture_output=True,
            text=True,
            timeout=15,
        )
        assert rc.returncode == 0, f"sops --decrypt failed: {rc.stderr}"
        assert "foo: bar" in rc.stdout
        assert "baz: qux" in rc.stdout

    def test_real_sops_rejects_pr_z_broken_tempfile_name(self, tmp_path):
        """Negative control: confirm the original PR-Z naming pattern
        REPRODUCES the iter#8 failure on the same machine that runs the
        positive test. Without this, the positive test is unfalsifiable
        (we can't tell whether sops accepts everything or whether the new
        name actually matters)."""
        keyfile = tmp_path / "age.key"
        rc = subprocess.run(
            [_AGE_KEYGEN, "-o", str(keyfile)],
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert rc.returncode == 0
        recipient = None
        for line in (rc.stderr + rc.stdout).splitlines():
            line = line.strip()
            if line.startswith("# public key:"):
                recipient = line.split(":", 1)[1].strip()
                break
        assert recipient

        sops_yaml = tmp_path / ".sops.yaml"
        sops_yaml.write_text(yaml.safe_dump({
            "creation_rules": [
                {"path_regex": r"secrets\.yaml$", "age": recipient}
            ]
        }))

        import tempfile
        # PR-Z's BROKEN naming pattern.
        fd, tmp_str = tempfile.mkstemp(
            prefix="secrets.", suffix=".plain", dir=str(tmp_path),
        )
        os.close(fd)
        broken_path = Path(tmp_str)
        broken_path.write_text("foo: bar\n")

        env = os.environ.copy()
        env["SOPS_AGE_KEY_FILE"] = str(keyfile)
        rc = subprocess.run(
            [_SOPS, "--encrypt", "--in-place", str(broken_path)],
            cwd=str(tmp_path),
            env=env,
            capture_output=True,
            text=True,
            timeout=15,
        )
        # The bug we're guarding against: rc != 0 and the stderr matches.
        assert rc.returncode != 0, (
            "sops accepted the PR-Z broken tempfile name; the bug this PR "
            "fixes appears to no longer exist (sops version bump?). "
            "Re-evaluate before deleting the test."
        )
        assert "no matching creation rules found" in (rc.stderr + rc.stdout), (
            f"sops failed but with unexpected error — bug shape changed. "
            f"stderr={rc.stderr!r} stdout={rc.stdout!r}"
        )
