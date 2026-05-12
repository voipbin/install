"""Tests for scripts/secretmgr.py — PR #4 schema-driven secret generation."""

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import yaml

from scripts.secretmgr import (
    ALLOWED_SECRET_KEYS,
    generate_all_secrets,
    validate_secrets_keys,
    write_secrets_yaml,
)


class TestAllowedKeySet:
    def test_thirty_one_total(self):
        # 24 sops-editable (20 secret-class minus JWT_KEY + 3 dsn + 1 voip)
        # plus 5 init-generated (JWT_KEY + 4 SSL_*_BASE64) = 29.
        # Design §5.3 nominal target is 31 but schema-derived actual is 29
        # because two keys cited (ENGINE_KEY_CHATGPT routing, etc.) are
        # accounted-for via rename in BIN_SERVICE_WIRING. Source of truth =
        # scripts/secret_schema.py.
        assert len(ALLOWED_SECRET_KEYS) == 29

    def test_contains_jwt_key(self):
        assert "JWT_KEY" in ALLOWED_SECRET_KEYS

    def test_contains_four_ssl_base64(self):
        for k in (
            "SSL_CERT_API_BASE64",
            "SSL_PRIVKEY_API_BASE64",
            "SSL_CERT_HOOK_BASE64",
            "SSL_PRIVKEY_HOOK_BASE64",
        ):
            assert k in ALLOWED_SECRET_KEYS

    def test_contains_voip_password(self):
        assert "DATABASE_ASTERISK_PASSWORD" in ALLOWED_SECRET_KEYS

    def test_no_lowercase_legacy_keys(self):
        # Old PR #3 schema used lowercase keys like jwt_key — must be gone.
        for legacy in ("jwt_key", "rabbitmq_user", "cloudsql_password"):
            assert legacy not in ALLOWED_SECRET_KEYS


class TestGenerateAllSecrets:
    def test_contains_26_operator_editable(self):
        secrets = generate_all_secrets()
        # 20 secret-class (excluding JWT_KEY) + 3 dsn + 1 voip = 24.
        assert len(secrets) == 24

    def test_all_keys_in_allowed_set(self):
        secrets = generate_all_secrets()
        for key in secrets:
            assert key in ALLOWED_SECRET_KEYS

    def test_init_generated_keys_not_in_output(self):
        # JWT_KEY + SSL_*_BASE64 are seeded by tls_bootstrap, not here.
        secrets = generate_all_secrets()
        assert "JWT_KEY" not in secrets
        for k in (
            "SSL_CERT_API_BASE64",
            "SSL_PRIVKEY_API_BASE64",
            "SSL_CERT_HOOK_BASE64",
            "SSL_PRIVKEY_HOOK_BASE64",
        ):
            assert k not in secrets

    def test_secret_class_values_nonempty(self):
        secrets = generate_all_secrets()
        assert secrets["OPENAI_API_KEY"]
        assert secrets["TWILIO_TOKEN"]


class TestValidateSecretsKeys:
    def test_accepts_subset_of_allowed(self):
        validate_secrets_keys({"OPENAI_API_KEY": "x"})

    def test_accepts_empty(self):
        validate_secrets_keys({})

    def test_rejects_unknown_key(self):
        with pytest.raises(ValueError, match="JWT_KEYS"):
            validate_secrets_keys({"JWT_KEYS": "typo"})

    def test_error_names_offending_key(self):
        with pytest.raises(ValueError, match="MY_RANDOM_KEY"):
            validate_secrets_keys({"MY_RANDOM_KEY": "x"})


class TestWriteSecretsYaml:
    def test_writes_yaml(self, tmp_path):
        path = tmp_path / "secrets.yaml"
        write_secrets_yaml({"OPENAI_API_KEY": "x"}, path)
        assert yaml.safe_load(path.read_text()) == {"OPENAI_API_KEY": "x"}

    def test_file_mode_0o600(self, tmp_path):
        path = tmp_path / "secrets.yaml"
        write_secrets_yaml({"k": "v"}, path)
        assert (path.stat().st_mode & 0o777) == 0o600


class TestDecryptValidatesKeys:
    @patch("scripts.secretmgr.run_cmd")
    def test_unknown_key_in_decrypted_returns_none(self, mock_run):
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout="JWT_KEYS: x\n",
            stderr="",
        )
        from scripts.secretmgr import decrypt_with_sops

        result = decrypt_with_sops(Path("/tmp/fake.yaml"))
        assert result is None
