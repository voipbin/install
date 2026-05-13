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
    across operator runs. PR-AA's own `cert-staging-*.secrets.yaml` pattern
    is also covered by the sweep so the symmetric leak (SIGKILL during the
    encrypt window of a PR-AA invocation) is closed."""

    def test_orphan_secrets_plaintext_swept_on_entry(self, tmp_path, monkeypatch):
        cfg = _make_cfg(tmp_path)
        # Simulate iter#8 leftovers AND prior-PR-AA-invocation leftovers.
        orphan_pr_z_a = tmp_path / "secrets.abc123.plain"
        orphan_pr_z_b = tmp_path / "secrets.def456.plain"
        orphan_pr_aa = tmp_path / "cert-staging-xyz789.secrets.yaml"
        orphan_pr_z_a.write_text("PLAINTEXT_SECRET_A\n")
        orphan_pr_z_b.write_text("PLAINTEXT_SECRET_B\n")
        orphan_pr_aa.write_text("PLAINTEXT_FROM_PRIOR_PR_AA_RUN\n")
        # Pre-create an UNRELATED file that must NOT be swept (E4 guard).
        unrelated = tmp_path / "operator-notes.plain"
        unrelated.write_text("operator's own notes; sweep must NOT touch\n")
        # Capture orphan state at the moment encrypt runs.
        observed: dict[str, list[str]] = {"orphans_during_encrypt": []}

        def _stub_encrypt(path, kms_key_id):
            observed["orphans_during_encrypt"] = sorted(
                p.name for p in tmp_path.glob("secrets.*.plain")
            ) + sorted(
                # Exclude the in-flight tempfile (the path being encrypted).
                p.name for p in tmp_path.glob("cert-staging-*.secrets.yaml")
                if p != Path(path)
            )
            return True

        monkeypatch.setattr(secretmgr, "encrypt_with_sops", _stub_encrypt)

        ok = pl._persist_secrets_after_reissue(cfg, {"k": "v"})
        assert ok is True
        # The sweep ran BEFORE encrypt; all three orphans were already gone.
        assert observed["orphans_during_encrypt"] == [], (
            f"orphan plaintext files survived into encrypt phase: "
            f"{observed['orphans_during_encrypt']}"
        )
        # Orphans deleted.
        assert not orphan_pr_z_a.exists()
        assert not orphan_pr_z_b.exists()
        assert not orphan_pr_aa.exists()
        # E4 guard: unrelated `.plain` file MUST survive — sweep must not
        # broaden to `*.plain` or it would delete operator-owned files.
        assert unrelated.exists(), (
            "sweep glob over-matched and deleted operator-owned `*.plain` "
            "files (E4 mutant survived). Restrict glob to `secrets.*.plain` "
            "and `cert-staging-*.secrets.yaml`."
        )


class TestPersistDestinationContract:
    """E1/E2: pin the destination contract.

    E1: if `os.replace` is removed/swapped for a no-op, the canonical
    `secrets.yaml` would never be updated and downstream stages would
    consume the stale prior-encrypted blob. PR body's mock tests miss this.

    E2: if `write_secrets_yaml` is misdirected to write directly to
    `config.secrets_path` (the canonical encrypted file), it would clobber
    encrypted data with plaintext — a catastrophic leak. Pin that the
    canonical path's bytes change ONLY via the encrypted-tempfile-replace
    path, never as a plaintext write."""

    def test_secrets_yaml_destination_bytes_change_via_replace(
        self, tmp_path, monkeypatch
    ):
        cfg = _make_cfg(tmp_path)
        # Simulate sops by writing a deterministic ENCRYPTED-shaped marker
        # to the tempfile that we can recognise on the canonical path.
        ENCRYPTED_MARKER = "SOPS_ENCRYPTED_BLOB_MARKER\n"
        captured_paths: dict[str, Path] = {}

        def _stub_encrypt(path, kms_key_id):
            captured_paths["encrypt_arg"] = Path(path)
            # Overwrite the tempfile with the encrypted marker, simulating
            # sops' --in-place behavior.
            Path(path).write_text(ENCRYPTED_MARKER)
            return True

        monkeypatch.setattr(secretmgr, "encrypt_with_sops", _stub_encrypt)
        ok = pl._persist_secrets_after_reissue(cfg, {"k": "v"})
        assert ok is True
        # Canonical secrets.yaml MUST now contain the encrypted marker, NOT
        # the original placeholder. Proves os.replace ran with the correct
        # source/dest. Catches E1 (os.replace removed).
        canonical = (tmp_path / "secrets.yaml").read_text()
        assert canonical == ENCRYPTED_MARKER, (
            f"secrets.yaml not updated via os.replace; contents: {canonical!r}"
        )

    def test_plaintext_never_written_to_canonical_secrets_path(
        self, tmp_path, monkeypatch
    ):
        cfg = _make_cfg(tmp_path)
        # Capture every call write_secrets_yaml receives — its destination
        # path MUST be the tempfile, never the canonical secrets.yaml.
        write_targets: list[Path] = []
        real_write = secretmgr.write_secrets_yaml

        def _spy_write(secrets_dict, path):
            write_targets.append(Path(path))
            real_write(secrets_dict, path)

        monkeypatch.setattr(secretmgr, "write_secrets_yaml", _spy_write)
        monkeypatch.setattr(secretmgr, "encrypt_with_sops", lambda p, k: True)
        ok = pl._persist_secrets_after_reissue(cfg, {"k": "v"})
        assert ok is True
        canonical = cfg.secrets_path.resolve()
        for target in write_targets:
            assert target.resolve() != canonical, (
                f"write_secrets_yaml wrote PLAINTEXT directly to canonical "
                f"secrets.yaml at {canonical} — catastrophic leak (E2 mutant). "
                f"Plaintext writes must go to a tempfile that sops then "
                f"encrypts in-place before os.replace promotes it."
            )


class TestKmsKeyIdPropagation:
    """E3: pin that the kms_key_id read from `.sops.yaml` is the SAME value
    passed to `encrypt_with_sops`. A mutation that hardcodes a wrong key
    would re-encrypt with the wrong recipient and lock the operator out
    on next decrypt. The mock test was previously blind to this because
    it ignored the kms_key_id arg."""

    def test_kms_key_id_from_sops_yaml_propagates_to_encrypt(
        self, tmp_path, monkeypatch
    ):
        cfg = _make_cfg(tmp_path)
        captured: dict[str, str] = {}

        def _stub_encrypt(path, kms_key_id):
            captured["kms_key_id"] = kms_key_id
            return True

        monkeypatch.setattr(secretmgr, "encrypt_with_sops", _stub_encrypt)
        ok = pl._persist_secrets_after_reissue(cfg, {"k": "v"})
        assert ok is True
        # The key passed to sops MUST equal the one declared in .sops.yaml.
        assert captured.get("kms_key_id") == KMS, (
            f"kms_key_id mis-propagation: .sops.yaml has {KMS!r} but "
            f"encrypt_with_sops received {captured.get('kms_key_id')!r}. "
            f"A wrong key would lock operators out of their encrypted "
            f"secrets on next decrypt (E3 mutant)."
        )


# ---------------------------------------------------------------------------
# Real sops + age round-trip — the actual-execution gate.
# ---------------------------------------------------------------------------

_SOPS = shutil.which("sops")
_AGE_KEYGEN = shutil.which("age-keygen")


def _generate_age_keypair(tmp_path: Path) -> tuple[Path, str]:
    """Generate an age keypair, return (keyfile_path, recipient_string).

    age-keygen writes the FULL keypair (including a `# public key:` comment
    line) to the OUTPUT FILE, and prints `Public key: <recipient>` to STDERR
    (capital P, no `#`). Earlier PR-AA versions parsed stderr looking for
    `# public key:` which is the keyfile syntax — that never appears in
    stderr and broke the test on every age version. Parse the keyfile
    instead; it is the most stable surface across age releases.
    """
    keyfile = tmp_path / "age.key"
    rc = subprocess.run(
        [_AGE_KEYGEN, "-o", str(keyfile)],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert rc.returncode == 0, f"age-keygen failed: {rc.stderr}"
    recipient = None
    for line in keyfile.read_text().splitlines():
        line = line.strip()
        if line.lower().startswith("# public key:"):
            recipient = line.split(":", 1)[1].strip()
            break
    assert recipient, (
        f"could not extract age recipient from keyfile contents:\n"
        f"{keyfile.read_text()!r}\nstderr={rc.stderr!r}"
    )
    return keyfile, recipient


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
        keyfile, recipient = _generate_age_keypair(tmp_path)

        # Write a .sops.yaml with the SAME path_regex shape that
        # write_sops_config produces in production.
        sops_yaml = tmp_path / ".sops.yaml"
        sops_yaml.write_text(yaml.safe_dump({
            "creation_rules": [
                {"path_regex": r"secrets\.yaml$", "age": recipient}
            ]
        }))

        # Use the EXACT same tempfile pattern PR-AA uses in production.
        import tempfile
        fd, tmp_str = tempfile.mkstemp(
            prefix="cert-staging-", suffix=".secrets.yaml", dir=str(tmp_path),
        )
        os.close(fd)
        plaintext_path = Path(tmp_str)
        plaintext_path.write_text("foo: bar\nbaz: qux\n")

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

        # Round-trip via decrypt to confirm payload integrity.
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
        positive test."""
        keyfile, recipient = _generate_age_keypair(tmp_path)

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
