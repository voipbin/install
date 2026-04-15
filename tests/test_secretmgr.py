"""Tests for scripts/secretmgr.py — secret generation logic."""

import string
from pathlib import Path

import yaml
import pytest

from scripts.secretmgr import generate_all_secrets, write_secrets_yaml


class TestGenerateAllSecrets:
    def test_returns_all_keys(self):
        secrets = generate_all_secrets()
        expected_keys = {
            "jwt_key",
            "cloudsql_password",
            "redis_password",
            "rabbitmq_user",
            "rabbitmq_password",
            "api_signing_key",
        }
        assert set(secrets.keys()) == expected_keys

    def test_passwords_are_nonempty(self):
        secrets = generate_all_secrets()
        for key, value in secrets.items():
            assert value, f"{key} should not be empty"

    def test_passwords_have_sufficient_length(self):
        secrets = generate_all_secrets()
        assert len(secrets["cloudsql_password"]) == 24
        assert len(secrets["redis_password"]) == 24
        assert len(secrets["rabbitmq_password"]) == 24

    def test_rabbitmq_user_is_voipbin(self):
        secrets = generate_all_secrets()
        assert secrets["rabbitmq_user"] == "voipbin"

    def test_keys_are_base64(self):
        secrets = generate_all_secrets()
        # URL-safe base64 chars: A-Z, a-z, 0-9, -, _, =
        b64_chars = set(string.ascii_letters + string.digits + "-_=")
        for key_name in ("jwt_key", "api_signing_key"):
            value = secrets[key_name]
            assert all(c in b64_chars for c in value), f"{key_name} is not valid base64"

    def test_unique_across_calls(self):
        s1 = generate_all_secrets()
        s2 = generate_all_secrets()
        assert s1["jwt_key"] != s2["jwt_key"]
        assert s1["cloudsql_password"] != s2["cloudsql_password"]

    def test_passwords_are_alphanumeric(self):
        secrets = generate_all_secrets()
        alphanum = set(string.ascii_letters + string.digits)
        for key in ("cloudsql_password", "redis_password", "rabbitmq_password"):
            assert all(c in alphanum for c in secrets[key]), f"{key} has non-alphanumeric chars"


class TestWriteSecretsYaml:
    def test_creates_valid_yaml(self, tmp_path):
        secrets = {"jwt_key": "test123", "password": "abc"}
        path = tmp_path / "secrets.yaml"
        write_secrets_yaml(secrets, path)
        assert path.exists()
        loaded = yaml.safe_load(path.read_text())
        assert loaded == secrets

    def test_overwrites_existing(self, tmp_path):
        path = tmp_path / "secrets.yaml"
        write_secrets_yaml({"old": "value"}, path)
        write_secrets_yaml({"new": "value"}, path)
        loaded = yaml.safe_load(path.read_text())
        assert loaded == {"new": "value"}

    def test_restricts_file_permissions(self, tmp_path):
        path = tmp_path / "secrets.yaml"
        write_secrets_yaml({"key": "val"}, path)
        mode = path.stat().st_mode & 0o777
        assert mode == 0o600, f"Expected 0o600, got {oct(mode)}"
