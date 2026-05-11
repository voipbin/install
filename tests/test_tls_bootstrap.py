"""Tests for scripts/tls_bootstrap.py — self-signed cert + multi-namespace Secret bootstrap.

Self-healing-on-retry contract (see design §5.6):
  - All SSL keys empty in voipbin-secret AND no voipbin-tls in any
    configured namespace → patch opaque + create voipbin-tls in each ns.
  - All SSL keys empty AND voipbin-tls exists in ANY configured ns →
    delete from ALL configured ns, then fresh-generate.
  - Both SSL keys non-empty → skip all writes.
  - Partial fill → BootstrapError.

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
    DEFAULT_TLS_NAMESPACES,
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
# Module-level invariants
# ---------------------------------------------------------------------------

class TestModuleConstants:
    def test_default_namespaces_includes_both(self):
        assert "bin-manager" in DEFAULT_TLS_NAMESPACES
        assert "square-manager" in DEFAULT_TLS_NAMESPACES
        assert len(DEFAULT_TLS_NAMESPACES) == 2

    def test_secret_name_defaults_not_redacted(self):
        # Regression: previously these were literally "***" by accident.
        from scripts.tls_bootstrap import DEFAULT_TLS_SECRET, DEFAULT_OPAQUE_SECRET
        assert DEFAULT_TLS_SECRET == "voipbin-tls"
        assert DEFAULT_OPAQUE_SECRET == "voipbin-secret"


# ---------------------------------------------------------------------------
# bootstrap_voipbin_tls_secret — kubectl interaction mocks
# ---------------------------------------------------------------------------

def _mock_subprocess_run(get_responses):
    """Build a side_effect that consumes kubectl invocations.

    `get_responses` is a list of (returncode, stdout_bytes) tuples,
    consumed in order for kubectl get secret calls. Any other call
    (patch/apply/delete) returns (0, b"").
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


class TestSelfHealingContract:
    def test_both_keys_empty_patches_opaque_and_creates_tls_in_each_ns(self):
        responses = [
            # _read_secret_ssl_keys: voipbin-secret has no SSL data
            (0, json.dumps({"data": {
                "JWT_KEY": base64.b64encode(b"jwt").decode(),
            }}).encode()),
            # _secret_exists checks: voipbin-tls missing in ns1, ns2
            (1, b""),
            (1, b""),
        ]
        side_effect, calls = _mock_subprocess_run(responses)
        with patch("scripts.tls_bootstrap.subprocess.run", side_effect=side_effect):
            result = bootstrap_voipbin_tls_secret(
                namespaces=["bin-manager", "square-manager"],
                hostnames=["api.example.com"],
            )
        assert result.voipbin_tls_action == {
            "bin-manager": "created",
            "square-manager": "created",
        }
        assert result.voipbin_secret_action == "patched"
        applies = [c for c in calls if c["args"][:2] == ["kubectl", "apply"]]
        patches = [c for c in calls if "patch" in c["args"]]
        # 2 voipbin-tls creates + 1 voipbin-secret patch
        assert len(applies) == 2
        assert len(patches) == 1
        # Apply was via stdin pipe (no -f file path)
        assert all(c["input"] is not None for c in applies)

    def test_both_keys_populated_skips_all_writes(self):
        responses = [
            (0, json.dumps({"data": {
                "SSL_CERT_BASE64": base64.b64encode(b"realcert").decode(),
                "SSL_PRIVKEY_BASE64": base64.b64encode(b"realkey").decode(),
                "JWT_KEY": base64.b64encode(b"jwt").decode(),
            }}).encode()),
        ]
        side_effect, calls = _mock_subprocess_run(responses)
        with patch("scripts.tls_bootstrap.subprocess.run", side_effect=side_effect):
            result = bootstrap_voipbin_tls_secret(
                namespaces=["bin-manager", "square-manager"],
                hostnames=["api.example.com"],
            )
        assert result.voipbin_tls_action == {
            "bin-manager": "skipped",
            "square-manager": "skipped",
        }
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
                bootstrap_voipbin_tls_secret(
                    namespaces=["bin-manager", "square-manager"],
                    hostnames=["api.example.com"],
                )

    def test_stale_in_any_ns_triggers_delete_from_all_ns(self):
        """If voipbin-tls exists in only ONE configured namespace, the
        stale-cleanup must delete from BOTH (idempotent --ignore-not-found
        on the namespace that didn't have it)."""
        responses = [
            (0, json.dumps({"data": {}}).encode()),  # opaque has no SSL keys
            (0, b"secret/voipbin-tls\n"),            # ns1 has voipbin-tls
            (1, b""),                                # ns2 missing voipbin-tls
        ]
        side_effect, calls = _mock_subprocess_run(responses)
        with patch("scripts.tls_bootstrap.subprocess.run", side_effect=side_effect):
            result = bootstrap_voipbin_tls_secret(
                namespaces=["bin-manager", "square-manager"],
                hostnames=["api.example.com"],
            )
        assert result.voipbin_tls_action == {
            "bin-manager": "created",
            "square-manager": "created",
        }
        deletes = [c for c in calls if "delete" in c["args"]]
        # MUST delete from BOTH namespaces (one is a no-op with
        # --ignore-not-found), not just the one that had it.
        assert len(deletes) == 2
        for d in deletes:
            assert "--ignore-not-found" in d["args"]
        # Then 2 fresh creates
        applies = [c for c in calls if c["args"][:2] == ["kubectl", "apply"]]
        assert len(applies) == 2

    def test_stale_in_all_ns_triggers_delete_from_all(self):
        responses = [
            (0, json.dumps({"data": {}}).encode()),
            (0, b"secret/voipbin-tls\n"),  # ns1 has it
            (0, b"secret/voipbin-tls\n"),  # ns2 has it
        ]
        side_effect, calls = _mock_subprocess_run(responses)
        with patch("scripts.tls_bootstrap.subprocess.run", side_effect=side_effect):
            bootstrap_voipbin_tls_secret(
                namespaces=["bin-manager", "square-manager"],
                hostnames=["api.example.com"],
            )
        deletes = [c for c in calls if "delete" in c["args"]]
        assert len(deletes) == 2

    def test_patch_payload_shape_preserves_non_ssl_keys(self):
        responses = [
            (0, json.dumps({"data": {}}).encode()),
            (1, b""),
            (1, b""),
        ]
        side_effect, calls = _mock_subprocess_run(responses)
        with patch("scripts.tls_bootstrap.subprocess.run", side_effect=side_effect):
            bootstrap_voipbin_tls_secret(
                namespaces=["bin-manager", "square-manager"],
                hostnames=["api.example.com"],
            )
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

    def test_opaque_secret_patch_only_in_bin_manager(self):
        """voipbin-secret must be patched ONLY in opaque_secret_namespace
        (default bin-manager), never in square-manager."""
        responses = [
            (0, json.dumps({"data": {}}).encode()),
            (1, b""),
            (1, b""),
        ]
        side_effect, calls = _mock_subprocess_run(responses)
        with patch("scripts.tls_bootstrap.subprocess.run", side_effect=side_effect):
            bootstrap_voipbin_tls_secret(
                namespaces=["bin-manager", "square-manager"],
                hostnames=["api.example.com"],
            )
        patches = [c for c in calls if "patch" in c["args"]]
        assert len(patches) == 1
        # The -n flag should be bin-manager
        ns_idx = patches[0]["args"].index("-n")
        assert patches[0]["args"][ns_idx + 1] == "bin-manager"

    def test_tls_secret_data_has_correct_keys(self):
        responses = [
            (0, json.dumps({"data": {}}).encode()),
            (1, b""),
            (1, b""),
        ]
        side_effect, calls = _mock_subprocess_run(responses)
        with patch("scripts.tls_bootstrap.subprocess.run", side_effect=side_effect):
            bootstrap_voipbin_tls_secret(
                namespaces=["bin-manager", "square-manager"],
                hostnames=["api.example.com"],
            )
        apply_calls = [c for c in calls if c["args"][:2] == ["kubectl", "apply"]]
        assert len(apply_calls) == 2
        for apply_call in apply_calls:
            body = json.loads(apply_call["input"])
            assert body["type"] == "kubernetes.io/tls"
            assert set(body["data"].keys()) == {"tls.crt", "tls.key"}

    def test_tls_secret_same_cert_in_all_namespaces(self):
        """Both voipbin-tls Secrets must hold the IDENTICAL cert pair
        from a single generation, not two separately-generated certs."""
        responses = [
            (0, json.dumps({"data": {}}).encode()),
            (1, b""),
            (1, b""),
        ]
        side_effect, calls = _mock_subprocess_run(responses)
        with patch("scripts.tls_bootstrap.subprocess.run", side_effect=side_effect):
            bootstrap_voipbin_tls_secret(
                namespaces=["bin-manager", "square-manager"],
                hostnames=["api.example.com"],
            )
        apply_calls = [c for c in calls if c["args"][:2] == ["kubectl", "apply"]]
        bodies = [json.loads(c["input"]) for c in apply_calls]
        assert bodies[0]["data"]["tls.crt"] == bodies[1]["data"]["tls.crt"]
        assert bodies[0]["data"]["tls.key"] == bodies[1]["data"]["tls.key"]

    def test_secret_not_found_treated_as_both_empty(self):
        """Fresh install: voipbin-secret does not exist yet at first
        bootstrap call. Should fall through the 'both empty' path."""
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
            result = bootstrap_voipbin_tls_secret(
                namespaces=["bin-manager", "square-manager"],
                hostnames=["api.example.com"],
            )
        assert result.voipbin_tls_action == {
            "bin-manager": "created",
            "square-manager": "created",
        }
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
                bootstrap_voipbin_tls_secret(
                    namespaces=["bin-manager"],
                    hostnames=["api.example.com"],
                )

    def test_empty_namespaces_raises(self):
        with pytest.raises(ValueError, match="namespaces"):
            bootstrap_voipbin_tls_secret(
                namespaces=[],
                hostnames=["api.example.com"],
            )

    def test_default_namespaces_uses_both(self):
        """When namespaces=None, the function must default to BOTH
        bin-manager and square-manager."""
        responses = [
            (0, json.dumps({"data": {}}).encode()),
            (1, b""),
            (1, b""),
        ]
        side_effect, calls = _mock_subprocess_run(responses)
        with patch("scripts.tls_bootstrap.subprocess.run", side_effect=side_effect):
            result = bootstrap_voipbin_tls_secret(
                hostnames=["api.example.com"],
            )
        assert set(result.voipbin_tls_action.keys()) == {"bin-manager", "square-manager"}

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
            for line in source.splitlines():
                stripped = line.strip()
                if stripped.startswith("#"):
                    continue
                assert forbidden not in stripped, f"forbidden token in line: {stripped}"
