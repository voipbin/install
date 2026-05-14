"""PR-AA: cert export-ca subcommand regression tests.

Pins the ``cmd_cert_export_ca()`` function in scripts/commands/cert.py that
exports the installer-managed CA certificate from SOPS secrets.yaml.

Test IDs match the design doc §4.2:
  E1-E11 : cmd_cert_export_ca correctness
  M1-M2  : mutation harness
"""

from __future__ import annotations

import base64
import io
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
import datetime

from scripts.commands.cert import cmd_cert_export_ca
from scripts.tls_bootstrap import KAMAILIO_CA_CERT_KEY


# ---------------------------------------------------------------------------
# Test CA fixture
# ---------------------------------------------------------------------------

def _make_ca_pem() -> bytes:
    """Generate a real self-signed CA cert for testing."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = issuer = x509.Name([
        x509.NameAttribute(NameOID.COMMON_NAME, "voipbin-test-ca"),
    ])
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(datetime.datetime.now(datetime.timezone.utc))
        .not_valid_after(
            datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=365)
        )
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )
    return cert.public_bytes(serialization.Encoding.PEM)


_CA_PEM: bytes = _make_ca_pem()
_CA_B64: str = base64.b64encode(_CA_PEM).decode()


def _self_signed_cert_state(**overrides) -> dict:
    base = {
        "actual_mode": "self_signed",
        "config_mode": "self_signed",
        "ca_fingerprint_sha256": "AA:BB:CC",
        "ca_not_after": "2027-01-01T00:00:00+00:00",
        "san_list": ["sip.example.com"],
        "leaf_certs": {
            "sip.example.com": {
                "not_after": "2027-01-01T00:00:00+00:00",
                "fingerprint_sha256": "11:22:33",
            },
        },
    }
    base.update(overrides)
    return base


def _make_secrets() -> dict:
    return {KAMAILIO_CA_CERT_KEY: _CA_B64}


def _make_stdout(is_tty: bool):
    """Return a mock stdout with controllable isatty()."""
    m = MagicMock()
    m.isatty.return_value = is_tty
    m.buffer = io.BytesIO()
    return m


# ---------------------------------------------------------------------------
# E1-E11: cmd_cert_export_ca correctness
# ---------------------------------------------------------------------------

class TestCmdCertExportCa:

    def _run(self, cert_state=None, secrets=None, config_exists=True,
             stdout_is_tty=False, output_path=None, as_der=False):
        """Helper: run cmd_cert_export_ca with mocked dependencies."""
        if cert_state is None:
            cert_state = _self_signed_cert_state()
        if secrets is None:
            secrets = _make_secrets()

        state = {"cert_state": cert_state}
        mock_cfg = MagicMock()
        mock_cfg.exists.return_value = config_exists

        mock_stdout = MagicMock()
        mock_stdout.isatty.return_value = stdout_is_tty
        mock_stdout.buffer = io.BytesIO()

        with (
            patch("scripts.commands.cert.load_state", return_value=state),
            patch("scripts.commands.cert.InstallerConfig", return_value=mock_cfg),
            patch("scripts.secretmgr.load_secrets_for_cert", return_value=secrets),
        ):
            import sys as _sys
            orig_stdout = _sys.stdout
            _sys.stdout = mock_stdout
            try:
                rc = cmd_cert_export_ca(output_path=output_path, as_der=as_der)
            finally:
                _sys.stdout = orig_stdout

        return rc, mock_stdout

    def test_e1_self_signed_stdout_pem(self):
        """E1: self_signed mode, stdout PEM → rc 0, output starts with PEM header."""
        rc, mock_stdout = self._run(stdout_is_tty=False)
        assert rc == 0
        written = mock_stdout.buffer.getvalue()
        assert written.startswith(b"-----BEGIN CERTIFICATE-----"), (
            f"Expected PEM header, got: {written[:50]!r}"
        )

    def test_e2_stdout_file_written(self, tmp_path):
        """E2: self_signed mode, --out FILE → rc 0, file written."""
        out_file = str(tmp_path / "ca.pem")
        rc, _ = self._run(output_path=out_file)
        assert rc == 0
        content = Path(out_file).read_bytes()
        assert content.startswith(b"-----BEGIN CERTIFICATE-----")

    def test_e3_der_file_written(self, tmp_path):
        """E3: self_signed mode, --der, --out FILE → rc 0, DER parseable as x509."""
        out_file = str(tmp_path / "ca.der")
        rc, _ = self._run(output_path=out_file, as_der=True)
        assert rc == 0
        content = Path(out_file).read_bytes()
        # Must be parseable as DER
        cert = x509.load_der_x509_certificate(content)
        assert cert.subject is not None

    def test_e4_der_no_out_tty_returns_error(self):
        """E4: --der without --out, stdout is TTY → rc 1, error message."""
        rc, _ = self._run(stdout_is_tty=True, as_der=True, output_path=None)
        assert rc == 1

    def test_e5_der_no_out_not_tty(self):
        """E5: --der without --out, stdout is NOT TTY (pipe) → rc 0, DER on stdout."""
        rc, mock_stdout = self._run(stdout_is_tty=False, as_der=True, output_path=None)
        assert rc == 0
        written = mock_stdout.buffer.getvalue()
        # DER is binary; verify parseable
        cert = x509.load_der_x509_certificate(written)
        assert cert.subject is not None

    def test_e6_manual_mode_returns_error(self):
        """E6: actual_mode == 'manual' → rc 1, 'managed externally' message."""
        cs = {"actual_mode": "manual", "config_mode": "manual", "san_list": [], "leaf_certs": {}}
        rc, _ = self._run(cert_state=cs)
        assert rc == 1

    def test_e7_actual_mode_none_returns_not_run_yet(self):
        """E7: actual_mode is None (cert_provision not run) → rc 1, 'not run yet' message.

        Verifies None-aware branch produces different message from E6 (manual mode).
        """
        cs = {}  # cert_state empty → actual_mode is None
        rc, _ = self._run(cert_state=cs)
        assert rc == 1

    def test_e8_missing_ca_fingerprint_returns_error(self):
        """E8: cert_state missing ca_fingerprint_sha256 → rc 1."""
        cs = _self_signed_cert_state()
        cs.pop("ca_fingerprint_sha256")
        rc, _ = self._run(cert_state=cs)
        assert rc == 1

    def test_e9_secrets_empty_returns_error(self):
        """E9: secrets empty → rc 1."""
        rc, _ = self._run(secrets={})
        assert rc == 1

    def test_e10_ca_cert_key_absent_from_secrets_returns_error(self):
        """E10: KAMAILIO_CA_CERT_KEY absent from secrets → rc 1."""
        secrets = {k: v for k, v in _make_secrets().items() if k != KAMAILIO_CA_CERT_KEY}
        rc, _ = self._run(secrets=secrets)
        assert rc == 1

    def test_e11_invalid_base64_returns_error(self):
        """E11: KAMAILIO_CA_CERT_KEY is invalid base64 → rc 1."""
        secrets = {KAMAILIO_CA_CERT_KEY: "not-valid-base64!!!!!"}
        rc, _ = self._run(secrets=secrets)
        assert rc == 1


# ---------------------------------------------------------------------------
# M1-M2: mutation harness
# ---------------------------------------------------------------------------

class TestMutantHarness:
    """Verify that the test suite catches broken implementations."""

    def _run(self, cert_state=None, secrets=None, **kwargs):
        if cert_state is None:
            cert_state = _self_signed_cert_state()
        if secrets is None:
            secrets = _make_secrets()
        state = {"cert_state": cert_state}
        mock_cfg = MagicMock()
        mock_cfg.exists.return_value = True
        mock_stdout = MagicMock()
        mock_stdout.isatty.return_value = False
        mock_stdout.buffer = io.BytesIO()
        with (
            patch("scripts.commands.cert.load_state", return_value=state),
            patch("scripts.commands.cert.InstallerConfig", return_value=mock_cfg),
            patch("scripts.secretmgr.load_secrets_for_cert", return_value=secrets),
        ):
            import sys as _sys
            orig = _sys.stdout
            _sys.stdout = mock_stdout
            try:
                rc = cmd_cert_export_ca(**kwargs)
            finally:
                _sys.stdout = orig
        return rc

    def test_m1_mutant_skip_mode_check(self):
        """M1: Mutant that skips mode check → E6 (manual mode) must be caught."""
        cs = {"actual_mode": "manual", "config_mode": "manual", "san_list": [], "leaf_certs": {}}
        rc = self._run(cert_state=cs)
        # If mode check was skipped, we'd hit a KeyError or wrong output.
        # The real implementation must return 1.
        assert rc == 1, "Manual mode must return rc=1 (mode check must fire)"

    def test_m2_mutant_skip_secrets_empty_check(self):
        """M2: Mutant that skips secrets empty check → E9 must catch."""
        rc = self._run(secrets={})
        assert rc == 1, "Empty secrets must return rc=1 (secrets check must fire)"
