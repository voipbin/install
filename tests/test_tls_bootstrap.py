"""Tests for scripts/tls_bootstrap.py — self-signed cert + Secret bootstrap.

Atomic-pair contract (see design §5.2.1):
  - Both SSL keys empty in voipbin-secret -> patch both + create voipbin-tls.
  - Both SSL keys non-empty -> skip both Secrets.
  - Partial fill -> BootstrapError.

Private key never written to disk: assertions on subprocess.run args
verify stdin pipe usage, not tempfile module.
"""

from __future__ import annotations

import base64
import json
from unittest.mock import MagicMock, patch

import pytest

from cryptography import x509
from cryptography.hazmat.primitives import serialization

from scripts.tls_bootstrap import (
    BootstrapError,
    DEFAULT_HOSTS,
    bootstrap_voipbin_tls_secret,
    _generate_self_signed,
)


# ---------------------------------------------------------------------------
# Cert generation
# ---------------------------------------------------------------------------

class TestGenerateSelfSigned:
    def test_returns_pem_bytes_pair(self):
        cert_pem, key_pem = _generate_self_signed(("api.example.com",))
        assert cert_pem.startswith(b"-----BEGIN CERTIFICATE-----")
        assert b"-----BEGIN PRIVATE KEY-----" in key_pem

    def test_san_list_contains_all_five_hosts(self):
        hosts = tuple(f"{h}.example.com" for h in DEFAULT_HOSTS)
        cert_pem, _ = _generate_self_signed(hosts)
        cert = x509.load_pem_x509_certificate(cert_pem)
        san_ext = cert.extensions.get_extension_for_class(
            x509.SubjectAlternativeName
        )
        san_dns = [n.value for n in san_ext.value]
        assert sorted(san_dns) == sorted(hosts)

    def test_issuer_equals_subject(self):
        cert_pem, _ = _generate_self_signed(("api.example.com",))
        cert = x509.load_pem_x509_certificate(cert_pem)
        assert cert.issuer == cert.subject

    def test_validity_days(self):
        cert_pem, _ = _generate_self_signed(("api.example.com",), valid_days=3650)
        cert = x509.load_pem_x509_certificate(cert_pem)
        delta = cert.not_valid_after_utc - cert.not_valid_before_utc
        # 10 years minus the 5-minute backdate
        assert 3649 < delta.days <= 3650

    def test_empty_hostnames_raises(self):
        with pytest.raises(ValueError):
            _generate_self_signed(())

    def test_key_is_pkcs8(self):
        _, key_pem = _generate_self_signed(("api.example.com",))
        # PKCS8 unencrypted keys load via load_pem_private_key
        key = serialization.load_pem_private_key(key_pem, password=None)
        assert key is not None


# ---------------------------------------------------------------------------
# bootstrap_voipbin_tls_secret — kubectl interaction mocks
# ---------------------------------------------------------------------------

def _mock_subprocess_run(get_responses):
    """Build a side_effect that consumes kubectl invocations.

    `get_responses` is a list of (returncode, stdout_bytes) tuples,
    consumed in order. Any patch/apply call returns (0, b"").
    """
    calls: list[dict] = []
    get_iter = iter(get_responses)

    def side_effect(args, **kwargs):
        result = MagicMock()
        result.returncode = 0
        result.stdout = b""
        result.stderr = b""
        # Capture call info
        calls.append({"args": args, "input": kwargs.get("input")})
        if "get" in args and "secret" in args:
            try:
                rc, stdout = next(get_iter)
            except StopIteration:
                rc, stdout = 0, b'{"data": {}}'
            result.returncode = rc
            result.stdout = stdout
        return result

    return side_effect, calls


class TestAtomicPairContract:
    def test_both_keys_empty_patches_both(self):
        responses = [
            # _read_secret_ssl_keys: voipbin-secret has no SSL data
            (0, json.dumps({"data": {
                "JWT_KEY": base64.b64encode(b"jwt").decode(),
            }}).encode()),
            # _secret_exists: voipbin-tls missing
            (1, b""),
        ]
        side_effect, calls = _mock_subprocess_run(responses)
        with patch("scripts.tls_bootstrap.subprocess.run", side_effect=side_effect):
            result = bootstrap_voipbin_tls_secret(hostnames=["api.example.com"])
        assert result.voipbin_tls_action == "created"
        assert result.voipbin_secret_action == "patched"
        # At least 1 apply (tls Secret create) + 1 patch
        applies = [c for c in calls if c["args"][:2] == ["kubectl", "apply"]]
        patches = [c for c in calls if "patch" in c["args"]]
        assert len(applies) == 1
        assert len(patches) == 1
        # Apply was via stdin pipe (no -f file path)
        assert applies[0]["input"] is not None

    def test_both_keys_populated_skips_both(self):
        responses = [
            (0, json.dumps({"data": {
                "SSL_CERT_BASE64": base64.b64encode(b"realcert").decode(),
                "SSL_PRIVKEY_BASE64": base64.b64encode(b"realkey").decode(),
                "JWT_KEY": base64.b64encode(b"jwt").decode(),
            }}).encode()),
        ]
        side_effect, calls = _mock_subprocess_run(responses)
        with patch("scripts.tls_bootstrap.subprocess.run", side_effect=side_effect):
            result = bootstrap_voipbin_tls_secret(hostnames=["api.example.com"])
        assert result.voipbin_tls_action == "skipped"
        assert result.voipbin_secret_action == "skipped-prefilled"
        applies = [c for c in calls if c["args"][:2] == ["kubectl", "apply"]]
        patches = [c for c in calls if "patch" in c["args"]]
        assert applies == []
        assert patches == []

    def test_partial_fill_raises(self):
        responses = [
            (0, json.dumps({"data": {
                "SSL_CERT_BASE64": base64.b64encode(b"realcert").decode(),
                # SSL_PRIVKEY_BASE64 missing
            }}).encode()),
        ]
        side_effect, _ = _mock_subprocess_run(responses)
        with patch("scripts.tls_bootstrap.subprocess.run", side_effect=side_effect):
            with pytest.raises(BootstrapError, match="partially set"):
                bootstrap_voipbin_tls_secret(hostnames=["api.example.com"])

    def test_voipbin_tls_exists_triggers_stale_cleanup(self):
        """A pre-existing voipbin-tls (from a partial prior run) is deleted
        and recreated together with the voipbin-secret patch, ensuring both
        Secrets end up holding the SAME cert pair."""
        responses = [
            (0, json.dumps({"data": {}}).encode()),  # no SSL keys in voipbin-secret
            (0, b"secret/voipbin-tls\n"),            # voipbin-tls exists
            (0, b""),                                # kubectl delete voipbin-tls
            (0, b""),                                # kubectl patch voipbin-secret
            (0, b""),                                # kubectl apply voipbin-tls (recreate)
        ]
        side_effect, calls = _mock_subprocess_run(responses)
        with patch("scripts.tls_bootstrap.subprocess.run", side_effect=side_effect):
            result = bootstrap_voipbin_tls_secret(hostnames=["api.example.com"])
        assert result.voipbin_tls_action == "created"
        assert result.voipbin_secret_action == "patched"
        deletes = [c for c in calls if c["args"][:2] == ["kubectl", "-n"] and "delete" in c["args"]]
        assert len(deletes) == 1, "stale voipbin-tls must be deleted exactly once"
        applies = [c for c in calls if c["args"][:2] == ["kubectl", "apply"]]
        assert len(applies) == 1, "voipbin-tls must be recreated after delete"

    def test_patch_payload_shape_preserves_non_ssl_keys(self):
        responses = [
            (0, json.dumps({"data": {}}).encode()),
            (1, b""),
        ]
        side_effect, calls = _mock_subprocess_run(responses)
        with patch("scripts.tls_bootstrap.subprocess.run", side_effect=side_effect):
            bootstrap_voipbin_tls_secret(hostnames=["api.example.com"])
        patch_call = next(c for c in calls if "patch" in c["args"])
        # patch arg structure: kubectl -n ns patch secret name --type=merge -p '<json>'
        assert "--type=merge" in patch_call["args"]
        idx = patch_call["args"].index("-p")
        patch_json = json.loads(patch_call["args"][idx + 1])
        # Only SSL keys are written; merge-patch leaves other keys alone
        assert set(patch_json["data"].keys()) == {"SSL_CERT_BASE64", "SSL_PRIVKEY_BASE64"}
        # No _API_/_HOOK_ suffixed keys
        for k in patch_json["data"]:
            assert "_API_" not in k
            assert "_HOOK_" not in k

    def test_tls_secret_data_has_correct_keys(self):
        responses = [
            (0, json.dumps({"data": {}}).encode()),
            (1, b""),
        ]
        side_effect, calls = _mock_subprocess_run(responses)
        with patch("scripts.tls_bootstrap.subprocess.run", side_effect=side_effect):
            bootstrap_voipbin_tls_secret(hostnames=["api.example.com"])
        apply_call = next(c for c in calls if c["args"][:2] == ["kubectl", "apply"])
        body = json.loads(apply_call["input"])
        assert body["type"] == "kubernetes.io/tls"
        assert set(body["data"].keys()) == {"tls.crt", "tls.key"}

    def test_secret_not_found_treated_as_both_empty(self):
        """Fresh install: voipbin-secret does not exist yet at first
        bootstrap call. Should fall through the 'both empty' path."""
        # _read_secret_ssl_keys: NotFound -> ("", "")
        # _secret_exists for voipbin-tls: not found
        calls: list[dict] = []

        def side_effect(args, **kwargs):
            result = MagicMock()
            result.returncode = 0
            result.stdout = b""
            result.stderr = b""
            calls.append({"args": args, "input": kwargs.get("input")})
            if "get" in args and "secret" in args and "voipbin-secret" in args:
                result.returncode = 1
                result.stderr = b'Error from server (NotFound): secrets "voipbin-secret" not found'
            elif "get" in args and "secret" in args and "voipbin-tls" in args:
                result.returncode = 1
            return result

        with patch("scripts.tls_bootstrap.subprocess.run", side_effect=side_effect):
            result = bootstrap_voipbin_tls_secret(hostnames=["api.example.com"])
        assert result.voipbin_tls_action == "created"
        assert result.voipbin_secret_action == "patched"

    def test_unexpected_kubectl_error_raises(self):
        """Non-NotFound errors (e.g. permission denied) must raise."""
        def side_effect(args, **kwargs):
            result = MagicMock()
            result.returncode = 1
            result.stdout = b""
            result.stderr = b"Error from server (Forbidden): user cannot read secrets"
            return result

        with patch("scripts.tls_bootstrap.subprocess.run", side_effect=side_effect):
            with pytest.raises(BootstrapError, match="failed to read Secret"):
                bootstrap_voipbin_tls_secret(hostnames=["api.example.com"])

    def test_no_tempfile_or_disk_write(self):
        """Verify bootstrap never touches operator filesystem.

        Asserts that scripts.tls_bootstrap module does not import
        tempfile and never writes a file via builtins.open.
        """
        import scripts.tls_bootstrap as mod
        import inspect
        source = inspect.getsource(mod)
        assert "import tempfile" not in source
        assert "tempfile.NamedTemporaryFile" not in source
        # The module must not call open() for writing
        for forbidden in ("open(", "with open"):
            # allow `# open(` comments but not real calls — simple
            # textual check; tighter check would parse AST
            for line in source.splitlines():
                stripped = line.strip()
                if stripped.startswith("#"):
                    continue
                assert forbidden not in stripped, f"forbidden token in line: {stripped}"
