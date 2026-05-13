"""PR-Z Phase B tests: pipeline cert_provision stage."""

from __future__ import annotations

import base64
import os
import shutil
import sys
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts import pipeline as pl
from scripts.cert_lifecycle import (
    CertLifecycleError,
    CertLifecycleResult,
    seed_kamailio_certs,
)
from scripts.config import InstallerConfig
from scripts.tls_bootstrap import (
    KAMAILIO_CA_CERT_KEY,
    KAMAILIO_CA_KEY_KEY,
    KAMAILIO_PAIRS,
    _generate_ca,
    _issue_leaf_signed_by_ca,
    _b64,
)


DOMAIN = "example.com"
SAN_LIST = ["sip.example.com", "registrar.example.com"]


def _seed_secrets_dict() -> dict:
    """Synthesize a complete self_signed secrets dict for materialization tests."""
    secrets = {}
    cfg = {"cert_mode": "self_signed", "domain": DOMAIN}
    state = {}
    seed_kamailio_certs(secrets, state, cfg)
    return secrets, state


class TestPipelineCertProvisionStage:
    def test_stage_inserted_at_index_6(self):
        stages = list(pl.APPLY_STAGES)
        assert "cert_provision" in stages
        assert stages.index("cert_provision") == 6
        # Must lie between reconcile_k8s_outputs and ansible_run
        assert stages[5] == "reconcile_k8s_outputs"
        assert stages[7] == "ansible_run"

    def test_runner_calls_seed_kamailio_certs(self, tmp_path, monkeypatch):
        monkeypatch.setattr(pl, "INSTALLER_DIR", tmp_path)
        monkeypatch.setattr(pl, "STATE_FILE", tmp_path / ".voipbin-state.yaml")
        called = {}

        def fake_seed(secrets_dict, cert_state, config, now=None):
            called["yes"] = True
            cert_state["san_list"] = SAN_LIST
            cert_state["actual_mode"] = "self_signed"
            cert_state["config_mode"] = "self_signed"
            return CertLifecycleResult(did_reissue=False, mode="self_signed")

        monkeypatch.setattr(
            "scripts.cert_lifecycle.seed_kamailio_certs", fake_seed
        )
        # Avoid actually loading from sops
        monkeypatch.setattr(
            pl, "_load_secrets_for_cert_stage", lambda c: {}
        )

        cfg = InstallerConfig(config_dir=tmp_path)
        cfg._data = {"cert_mode": "self_signed", "domain": DOMAIN}
        ok = pl._run_cert_provision(cfg, {}, dry_run=False, auto_approve=True)
        assert ok is True
        assert called.get("yes") is True

    def test_runner_returns_false_on_certlifecycle_error(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.setattr(pl, "INSTALLER_DIR", tmp_path)
        monkeypatch.setattr(pl, "STATE_FILE", tmp_path / ".voipbin-state.yaml")

        def raises(*a, **kw):
            raise CertLifecycleError("boom")

        monkeypatch.setattr(
            "scripts.cert_lifecycle.seed_kamailio_certs", raises
        )
        monkeypatch.setattr(
            pl, "_load_secrets_for_cert_stage", lambda c: {}
        )

        cfg = InstallerConfig(config_dir=tmp_path)
        cfg._data = {"cert_mode": "self_signed", "domain": DOMAIN}
        ok = pl._run_cert_provision(cfg, {}, dry_run=False, auto_approve=True)
        assert ok is False

    def test_state_updated_on_success(self, tmp_path, monkeypatch):
        monkeypatch.setattr(pl, "INSTALLER_DIR", tmp_path)
        monkeypatch.setattr(pl, "STATE_FILE", tmp_path / ".voipbin-state.yaml")

        secrets, state = _seed_secrets_dict()

        def fake_seed(secrets_dict, cert_state, config, now=None):
            for k, v in secrets.items():
                secrets_dict[k] = v
            cert_state.update(state)
            return CertLifecycleResult(did_reissue=False, mode="self_signed")

        monkeypatch.setattr(
            "scripts.cert_lifecycle.seed_kamailio_certs", fake_seed
        )
        monkeypatch.setattr(
            pl, "_load_secrets_for_cert_stage", lambda c: {}
        )

        cfg = InstallerConfig(config_dir=tmp_path)
        cfg._data = {"cert_mode": "self_signed", "domain": DOMAIN}
        ok = pl._run_cert_provision(cfg, {}, dry_run=False, auto_approve=True)
        assert ok is True
        persisted = pl.load_state()
        cs = persisted.get("cert_state") or {}
        assert cs.get("san_list") == SAN_LIST
        assert cs.get("actual_mode") == "self_signed"

    def test_dry_run_skips_file_writes(self, tmp_path, monkeypatch):
        monkeypatch.setattr(pl, "INSTALLER_DIR", tmp_path)
        monkeypatch.setattr(pl, "STATE_FILE", tmp_path / ".voipbin-state.yaml")
        cfg = InstallerConfig(config_dir=tmp_path)
        cfg._data = {"cert_mode": "self_signed", "domain": DOMAIN}
        ok = pl._run_cert_provision(cfg, {}, dry_run=True, auto_approve=True)
        assert ok is True
        # Dry-run must NOT have created the staging dir
        assert not (tmp_path / pl.CERT_STAGING_DIRNAME).exists()
        # Or written state with cert_state populated
        st = pl.load_state()
        assert not (st.get("cert_state") or {}).get("san_list")

    def test_idempotent_on_rerun(self, tmp_path, monkeypatch):
        """Second call short-circuits via cert_lifecycle when state is fresh."""
        monkeypatch.setattr(pl, "INSTALLER_DIR", tmp_path)
        monkeypatch.setattr(pl, "STATE_FILE", tmp_path / ".voipbin-state.yaml")

        secrets, state = _seed_secrets_dict()
        # Persist the result into state file directly to simulate a prior run.
        pl.save_state({"cert_state": dict(state)})
        # Have the loader return the already-seeded secrets dict each time.
        monkeypatch.setattr(
            pl, "_load_secrets_for_cert_stage", lambda c: dict(secrets)
        )
        # Skip the sops re-encrypt path (would fail without .sops.yaml).
        monkeypatch.setattr(
            pl, "_persist_secrets_after_reissue",
            lambda c, s: pytest.fail("must not persist secrets on idempotent rerun"),
        )

        cfg = InstallerConfig(config_dir=tmp_path)
        cfg._data = {"cert_mode": "self_signed", "domain": DOMAIN}
        ok = pl._run_cert_provision(cfg, {}, dry_run=False, auto_approve=True)
        assert ok is True

    def test_mid_write_failure_leaves_secrets_yaml_unmutated(
        self, tmp_path, monkeypatch
    ):
        """If sops re-encrypt fails, the encrypted secrets file stays intact.

        We exercise the _persist_secrets_after_reissue atomic path: the
        temp file is written, then os.replace is what flips the on-disk
        encrypted file. If the encrypt step fails, os.replace is NOT
        called and the original secrets.yaml is untouched.
        """
        monkeypatch.setattr(pl, "INSTALLER_DIR", tmp_path)
        monkeypatch.setattr(pl, "STATE_FILE", tmp_path / ".voipbin-state.yaml")
        # Pre-populate a fake encrypted file and stash its bytes.
        secrets_file = tmp_path / "secrets.yaml"
        secrets_file.write_text("ENCRYPTED_PLACEHOLDER\n")
        original_bytes = secrets_file.read_bytes()
        # Write a .sops.yaml so kms_key_id resolves.
        (tmp_path / ".sops.yaml").write_text(
            "creation_rules:\n  - gcp_kms: projects/test/locations/global/keyRings/r/cryptoKeys/k\n"
        )

        # Force the sops encrypt to fail mid-write.
        monkeypatch.setattr(
            "scripts.secretmgr.encrypt_with_sops",
            lambda path, kms: False,
        )

        cfg = InstallerConfig(config_dir=tmp_path)
        cfg._data = {"cert_mode": "self_signed", "domain": DOMAIN}
        ok = pl._persist_secrets_after_reissue(cfg, {"foo": "bar"})
        assert ok is False
        # Original encrypted blob untouched.
        assert secrets_file.read_bytes() == original_bytes


class TestStageLabelsCompleteness:
    def test_cert_provision_label_present(self):
        assert "cert_provision" in pl.STAGE_LABELS
        label = pl.STAGE_LABELS["cert_provision"]
        assert isinstance(label, str) and label.strip()


class TestPostSuccessCleanup:
    def test_cleanup_removes_staging_dir(self, tmp_path):
        staging = tmp_path / pl.CERT_STAGING_DIRNAME
        (staging / "sip.example.com").mkdir(parents=True)
        (staging / "sip.example.com" / "fullchain.pem").write_text("x")
        pl.cleanup_cert_staging(tmp_path)
        assert not staging.exists()

    def test_cleanup_swallows_rmtree_errors(self, tmp_path, monkeypatch):
        staging = tmp_path / pl.CERT_STAGING_DIRNAME
        staging.mkdir()
        def raising(*a, **kw):
            raise OSError("boom")
        monkeypatch.setattr(shutil, "rmtree", raising)
        # Must not raise
        pl.cleanup_cert_staging(tmp_path)

    def test_cleanup_idempotent_on_missing(self, tmp_path):
        # Does NOT exist — must be a clean no-op.
        pl.cleanup_cert_staging(tmp_path)
        # Run again — still no error.
        pl.cleanup_cert_staging(tmp_path)


class TestAnsibleRequirementsCollection:
    def test_requirements_yml_exists_with_ansible_posix(self):
        import yaml
        req = Path(__file__).resolve().parent.parent / "ansible" / "requirements.yml"
        assert req.exists(), "ansible/requirements.yml must exist"
        parsed = yaml.safe_load(req.read_text())
        cols = parsed.get("collections") or []
        names = {c.get("name") if isinstance(c, dict) else c for c in cols}
        assert "ansible.posix" in names

    def test_install_helper_invokes_ansible_galaxy(self, monkeypatch):
        from scripts import ansible_runner as ar
        called = {}

        class FakeResult:
            returncode = 0

        def fake_run_cmd(cmd, **kw):
            called["cmd"] = cmd
            return FakeResult()

        monkeypatch.setattr(ar, "run_cmd", fake_run_cmd)
        # Pretend requirements.yml exists.
        monkeypatch.setattr(
            ar.REQUIREMENTS_YML.__class__, "exists", lambda self: True,
            raising=False,
        )
        ok = ar._install_ansible_collections()
        assert ok is True
        cmd = called.get("cmd") or []
        joined = " ".join(cmd)
        assert "ansible-galaxy" in joined
        assert "collection" in joined
        assert "install" in joined


class TestStagingMaterialization:
    def test_self_signed_writes_two_san_dirs_with_concat_fullchain(
        self, tmp_path
    ):
        ca_cert_pem, ca_key_pem = _generate_ca()
        secrets = {
            KAMAILIO_CA_CERT_KEY: _b64(ca_cert_pem),
            KAMAILIO_CA_KEY_KEY: _b64(ca_key_pem),
        }
        for prefix, cert_key, priv_key in KAMAILIO_PAIRS:
            san = f"{prefix}.{DOMAIN}"
            leaf_pem, leaf_key_pem = _issue_leaf_signed_by_ca(
                san=san, ca_cert_pem=ca_cert_pem, ca_key_pem=ca_key_pem,
                wildcard=(prefix == "registrar"),
            )
            secrets[cert_key] = _b64(leaf_pem)
            secrets[priv_key] = _b64(leaf_key_pem)
        cert_state = {
            "actual_mode": "self_signed",
            "config_mode": "self_signed",
            "san_list": SAN_LIST,
        }
        pl._materialize_cert_staging(secrets, cert_state, tmp_path)
        staging = tmp_path / pl.CERT_STAGING_DIRNAME
        for san in SAN_LIST:
            d = staging / san
            assert (d / "fullchain.pem").exists()
            assert (d / "privkey.pem").exists()
            content = (d / "fullchain.pem").read_bytes()
            # Concatenated: contains BOTH leaf cert and CA cert PEMs.
            assert content.count(b"-----BEGIN CERTIFICATE-----") >= 2

    def test_manual_writes_two_san_dirs_no_ca_append(self, tmp_path):
        # In manual mode, the secrets dict holds operator-supplied PEMs
        # verbatim — _materialize_cert_staging must NOT append a CA.
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import rsa
        from cryptography.x509.oid import NameOID
        from datetime import datetime, timedelta, timezone

        def _selfsigned(san):
            key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
            now = datetime.now(timezone.utc)
            cert = (
                x509.CertificateBuilder()
                .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, san)]))
                .issuer_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, san)]))
                .public_key(key.public_key())
                .serial_number(x509.random_serial_number())
                .not_valid_before(now - timedelta(minutes=5))
                .not_valid_after(now + timedelta(days=30))
                .add_extension(
                    x509.SubjectAlternativeName([x509.DNSName(san)]),
                    critical=False,
                )
                .sign(key, hashes.SHA256())
            )
            return (
                cert.public_bytes(serialization.Encoding.PEM),
                key.private_bytes(
                    serialization.Encoding.PEM,
                    serialization.PrivateFormat.PKCS8,
                    serialization.NoEncryption(),
                ),
            )

        secrets = {}
        for prefix, cert_key, priv_key in KAMAILIO_PAIRS:
            san = f"{prefix}.{DOMAIN}"
            cert_pem, key_pem = _selfsigned(san)
            secrets[cert_key] = _b64(cert_pem)
            secrets[priv_key] = _b64(key_pem)
        cert_state = {
            "actual_mode": "manual",
            "config_mode": "manual",
            "san_list": SAN_LIST,
        }
        pl._materialize_cert_staging(secrets, cert_state, tmp_path)
        for san in SAN_LIST:
            d = tmp_path / pl.CERT_STAGING_DIRNAME / san
            content = (d / "fullchain.pem").read_bytes()
            # Manual: leaf PEM only, no CA concat.
            assert content.count(b"-----BEGIN CERTIFICATE-----") == 1

    def test_staging_modes_are_0700_and_0600(self, tmp_path):
        secrets = {
            "KAMAILIO_CERT_SIP_BASE64": _b64(b"dummy-leaf\n"),
            "KAMAILIO_PRIVKEY_SIP_BASE64": _b64(b"dummy-key\n"),
            "KAMAILIO_CERT_REGISTRAR_BASE64": _b64(b"dummy-leaf\n"),
            "KAMAILIO_PRIVKEY_REGISTRAR_BASE64": _b64(b"dummy-key\n"),
        }
        cert_state = {
            "actual_mode": "manual",
            "config_mode": "manual",
            "san_list": SAN_LIST,
        }
        pl._materialize_cert_staging(secrets, cert_state, tmp_path)
        staging = tmp_path / pl.CERT_STAGING_DIRNAME
        assert (os.stat(staging).st_mode & 0o777) == 0o700
        for san in SAN_LIST:
            d = staging / san
            assert (os.stat(d).st_mode & 0o777) == 0o700
            for fname in ("fullchain.pem", "privkey.pem"):
                f = d / fname
                assert (os.stat(f).st_mode & 0o777) == 0o600
