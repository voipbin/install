"""Tests for scripts/tls_bootstrap.py — sops-only seed logic (PR #4).

Covers the 4 state cases for design §4.8 idempotency:
  - first run: all 5 init keys generated.
  - repeat:    all 5 keys present → no-op.
  - partial:   one pair missing → that pair regenerated, others untouched.
  - corrupt:   half of a pair set (cert only or privkey only) → error.
"""

from __future__ import annotations

import base64

import pytest
import yaml

from scripts.tls_bootstrap import (
    BootstrapError,
    BootstrapResult,
    seed_secrets_yaml,
    run as tls_run,
)


# ---------------------------------------------------------------------------
# seed_secrets_yaml — pure mutation function
# ---------------------------------------------------------------------------


class TestSeedFirstRun:
    def test_generates_all_five_keys(self):
        data: dict = {}
        result = seed_secrets_yaml(data, domain="example.com")
        for key in (
            "JWT_KEY",
            "SSL_CERT_API_BASE64",
            "SSL_PRIVKEY_API_BASE64",
            "SSL_CERT_HOOK_BASE64",
            "SSL_PRIVKEY_HOOK_BASE64",
        ):
            assert key in data, f"{key} should have been generated"
            assert data[key], f"{key} should not be empty"
            assert key in result.generated

    def test_jwt_key_is_64_hex_chars(self):
        data: dict = {}
        seed_secrets_yaml(data)
        assert len(data["JWT_KEY"]) == 64
        int(data["JWT_KEY"], 16)  # raises if not hex

    def test_ssl_values_are_valid_base64_pem(self):
        data: dict = {}
        seed_secrets_yaml(data, domain="example.com")
        for key in (
            "SSL_CERT_API_BASE64",
            "SSL_CERT_HOOK_BASE64",
            "SSL_PRIVKEY_API_BASE64",
            "SSL_PRIVKEY_HOOK_BASE64",
        ):
            pem = base64.b64decode(data[key])
            assert pem.startswith(b"-----BEGIN"), f"{key} is not PEM"


class TestSeedRepeat:
    def test_all_keys_preserved_on_second_call(self):
        data: dict = {}
        seed_secrets_yaml(data, domain="example.com")
        snapshot = dict(data)

        result = seed_secrets_yaml(data, domain="example.com")
        assert result.generated == ()
        assert set(result.skipped) == {
            "JWT_KEY",
            "SSL_CERT_API_BASE64",
            "SSL_PRIVKEY_API_BASE64",
            "SSL_CERT_HOOK_BASE64",
            "SSL_PRIVKEY_HOOK_BASE64",
        }
        # Byte-equal preservation.
        for key, value in snapshot.items():
            assert data[key] == value, f"{key} was regenerated unexpectedly"


class TestSeedPartial:
    def test_api_pair_only_regenerates_api(self):
        data: dict = {}
        seed_secrets_yaml(data, domain="example.com")
        # Drop both api keys to simulate partial state of "api pair missing"
        del data["SSL_CERT_API_BASE64"]
        del data["SSL_PRIVKEY_API_BASE64"]
        hook_cert_before = data["SSL_CERT_HOOK_BASE64"]
        hook_priv_before = data["SSL_PRIVKEY_HOOK_BASE64"]
        jwt_before = data["JWT_KEY"]

        result = seed_secrets_yaml(data, domain="example.com")
        assert "SSL_CERT_API_BASE64" in result.generated
        assert "SSL_PRIVKEY_API_BASE64" in result.generated
        assert "SSL_CERT_HOOK_BASE64" not in result.generated
        assert "JWT_KEY" not in result.generated
        # Hook + JWT untouched
        assert data["SSL_CERT_HOOK_BASE64"] == hook_cert_before
        assert data["SSL_PRIVKEY_HOOK_BASE64"] == hook_priv_before
        assert data["JWT_KEY"] == jwt_before


class TestSeedCorruptHalfState:
    def test_cert_present_without_privkey_raises(self):
        data = {
            "SSL_CERT_API_BASE64": "fake-cert",
            # privkey deliberately missing
        }
        with pytest.raises(BootstrapError, match="half-state"):
            seed_secrets_yaml(data, domain="example.com")

    def test_privkey_present_without_cert_raises(self):
        data = {
            "SSL_PRIVKEY_HOOK_BASE64": "fake-privkey",
        }
        with pytest.raises(BootstrapError, match="half-state"):
            seed_secrets_yaml(data, domain="example.com")


# ---------------------------------------------------------------------------
# run — file I/O wrapper
# ---------------------------------------------------------------------------


class TestRun:
    def test_writes_secrets_yaml_when_absent(self, tmp_path):
        path = tmp_path / "secrets.yaml"
        result = tls_run(path, domain="example.com")
        assert path.exists()
        loaded = yaml.safe_load(path.read_text())
        assert "JWT_KEY" in loaded
        assert len(result.generated) == 5

    def test_repeat_run_byte_equal(self, tmp_path):
        path = tmp_path / "secrets.yaml"
        tls_run(path, domain="example.com")
        first = path.read_text()
        tls_run(path, domain="example.com")
        second = path.read_text()
        assert first == second

    def test_corrupt_half_state_in_file_raises(self, tmp_path):
        path = tmp_path / "secrets.yaml"
        path.write_text(
            yaml.safe_dump(
                {"SSL_CERT_API_BASE64": "x"}, default_flow_style=False
            )
        )
        with pytest.raises(BootstrapError):
            tls_run(path, domain="example.com")
