"""Tests for scripts/k8s.py — placeholder substitution logic."""

import pytest

from scripts.k8s import _build_substitution_map


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

class FakeConfig:
    """Minimal stand-in for InstallerConfig."""

    def __init__(self, data: dict):
        self._data = data

    def get(self, key, default=None):
        return self._data.get(key, default)


@pytest.fixture
def sample_config():
    return FakeConfig({
        "domain": "voipbin.example.com",
        "gcp_project_id": "my-project-123",
        "region": "us-central1",
    })


@pytest.fixture
def sample_secrets():
    return {
        "jwt_key": "test-jwt-key-abc",
        "cloudsql_password": "db-pass-xyz",
        "redis_password": "redis-pass-123",
        "rabbitmq_user": "voipbin",
        "rabbitmq_password": "rmq-pass-456",
        "api_signing_key": "sign-key-789",
    }


@pytest.fixture
def sample_tf_outputs():
    return {
        "cloudsql_instance_name": "prod-mysql",
        "cloudsql_proxy_sa_name": "sa-proxy",
        "recording_bucket_name": "my-project-123-recordings",
    }


# ---------------------------------------------------------------------------
# _build_substitution_map
# ---------------------------------------------------------------------------

class TestBuildSubstitutionMap:
    def test_secrets_mapped(self, sample_config, sample_secrets, sample_tf_outputs):
        subs = _build_substitution_map(sample_config, sample_tf_outputs, sample_secrets)
        assert subs["PLACEHOLDER_JWT_KEY"] == "test-jwt-key-abc"
        assert subs["PLACEHOLDER_DB_PASSWORD"] == "db-pass-xyz"
        assert subs["PLACEHOLDER_REDIS_PASSWORD"] == "redis-pass-123"
        assert subs["PLACEHOLDER_RABBITMQ_USER"] == "voipbin"
        assert subs["PLACEHOLDER_RABBITMQ_PASSWORD"] == "rmq-pass-456"
        assert subs["PLACEHOLDER_API_SIGNING_KEY"] == "sign-key-789"

    def test_config_mapped(self, sample_config, sample_secrets, sample_tf_outputs):
        subs = _build_substitution_map(sample_config, sample_tf_outputs, sample_secrets)
        assert subs["PLACEHOLDER_DOMAIN"] == "voipbin.example.com"
        assert subs["PLACEHOLDER_PROJECT_ID"] == "my-project-123"
        assert subs["PLACEHOLDER_REGION"] == "us-central1"
        assert subs["PLACEHOLDER_DB_NAME"] == "voipbin"
        assert subs["PLACEHOLDER_ACME_EMAIL"] == "admin@voipbin.example.com"

    def test_terraform_outputs_mapped(self, sample_config, sample_secrets, sample_tf_outputs):
        subs = _build_substitution_map(sample_config, sample_tf_outputs, sample_secrets)
        assert subs["PLACEHOLDER_INSTANCE_NAME"] == "prod-mysql"
        assert subs["PLACEHOLDER_CLOUDSQL_SA"] == "sa-proxy"
        assert subs["PLACEHOLDER_RECORDING_BUCKET_NAME"] == "my-project-123-recordings"

    def test_derived_rabbitmq_address(self, sample_config, sample_secrets, sample_tf_outputs):
        subs = _build_substitution_map(sample_config, sample_tf_outputs, sample_secrets)
        expected = "amqp://voipbin:rmq-pass-456@rabbitmq.infrastructure.svc.cluster.local:5672/"
        assert subs["PLACEHOLDER_RABBITMQ_ADDRESS"] == expected

    def test_derived_redis_address(self, sample_config, sample_secrets, sample_tf_outputs):
        subs = _build_substitution_map(sample_config, sample_tf_outputs, sample_secrets)
        expected = "redis://:redis-pass-123@redis.infrastructure.svc.cluster.local:6379/0"
        assert subs["PLACEHOLDER_REDIS_ADDRESS"] == expected

    def test_db_user_defaults_to_root(self, sample_config, sample_secrets, sample_tf_outputs):
        subs = _build_substitution_map(sample_config, sample_tf_outputs, sample_secrets)
        assert subs["PLACEHOLDER_DB_USER"] == "root"

    def test_defaults_when_secrets_empty(self, sample_config, sample_tf_outputs):
        subs = _build_substitution_map(sample_config, sample_tf_outputs, {})
        assert subs["PLACEHOLDER_RABBITMQ_USER"] == "voipbin"
        assert subs["PLACEHOLDER_JWT_KEY"] == ""
        assert subs["PLACEHOLDER_DB_USER"] == "root"

    def test_defaults_when_tf_outputs_empty(self, sample_config, sample_secrets):
        subs = _build_substitution_map(sample_config, {}, sample_secrets)
        assert subs["PLACEHOLDER_INSTANCE_NAME"] == "voipbin-mysql"
        assert subs["PLACEHOLDER_CLOUDSQL_SA"] == "voipbin-cloudsql-proxy"
        assert subs["PLACEHOLDER_RECORDING_BUCKET_NAME"] == "my-project-123-voipbin-recordings"

    def test_all_keys_are_placeholder_prefixed(self, sample_config, sample_secrets, sample_tf_outputs):
        subs = _build_substitution_map(sample_config, sample_tf_outputs, sample_secrets)
        for key in subs:
            assert key.startswith("PLACEHOLDER_"), f"Key {key} missing PLACEHOLDER_ prefix"

    def test_substitution_covers_all_manifest_placeholders(
        self, sample_config, sample_secrets, sample_tf_outputs
    ):
        """Every PLACEHOLDER_* used in k8s/ manifests must have a substitution entry."""
        subs = _build_substitution_map(sample_config, sample_tf_outputs, sample_secrets)
        known_placeholders = {
            "PLACEHOLDER_JWT_KEY",
            "PLACEHOLDER_DB_USER",
            "PLACEHOLDER_DB_PASSWORD",
            "PLACEHOLDER_REDIS_PASSWORD",
            "PLACEHOLDER_RABBITMQ_PASSWORD",
            "PLACEHOLDER_API_SIGNING_KEY",
            "PLACEHOLDER_DOMAIN",
            "PLACEHOLDER_PROJECT_ID",
            "PLACEHOLDER_REGION",
            "PLACEHOLDER_DB_NAME",
            "PLACEHOLDER_ACME_EMAIL",
            "PLACEHOLDER_INSTANCE_NAME",
            "PLACEHOLDER_CLOUDSQL_SA",
            "PLACEHOLDER_RECORDING_BUCKET_NAME",
            "PLACEHOLDER_RABBITMQ_USER",
            "PLACEHOLDER_RABBITMQ_ADDRESS",
            "PLACEHOLDER_REDIS_ADDRESS",
            "PLACEHOLDER_STATIC_IP_NAME_API_MANAGER",
            "PLACEHOLDER_STATIC_IP_NAME_HOOK_MANAGER",
            "PLACEHOLDER_STATIC_IP_NAME_ADMIN",
            "PLACEHOLDER_STATIC_IP_NAME_TALK",
            "PLACEHOLDER_STATIC_IP_NAME_MEET",
        }
        for placeholder in known_placeholders:
            assert placeholder in subs, f"Missing substitution for {placeholder}"


class TestStaticIpPlaceholders:
    """The 5 PLACEHOLDER_STATIC_IP_NAME_* tokens added in PR #2 of the
    self-hosting redesign read from terraform_outputs and have safe
    default fallbacks matching the Terraform resource names."""

    def test_static_ip_tokens_from_terraform_outputs(self, sample_config, sample_secrets):
        tf_outputs = {
            "api_manager_static_ip_name": "api-manager-static-ip",
            "hook_manager_static_ip_name": "hook-manager-static-ip",
            "admin_static_ip_name": "admin-static-ip",
            "talk_static_ip_name": "talk-static-ip",
            "meet_static_ip_name": "meet-static-ip",
        }
        subs = _build_substitution_map(sample_config, tf_outputs, sample_secrets)
        assert subs["PLACEHOLDER_STATIC_IP_NAME_API_MANAGER"] == "api-manager-static-ip"
        assert subs["PLACEHOLDER_STATIC_IP_NAME_HOOK_MANAGER"] == "hook-manager-static-ip"
        assert subs["PLACEHOLDER_STATIC_IP_NAME_ADMIN"] == "admin-static-ip"
        assert subs["PLACEHOLDER_STATIC_IP_NAME_TALK"] == "talk-static-ip"
        assert subs["PLACEHOLDER_STATIC_IP_NAME_MEET"] == "meet-static-ip"

    def test_static_ip_tokens_fallback_when_tf_outputs_empty(self, sample_config, sample_secrets):
        subs = _build_substitution_map(sample_config, {}, sample_secrets)
        # Fallbacks match the Terraform resource names so PR #3a (which
        # adds the annotation references) does not break if a stale
        # state file lacks the new outputs.
        assert subs["PLACEHOLDER_STATIC_IP_NAME_API_MANAGER"] == "api-manager-static-ip"
        assert subs["PLACEHOLDER_STATIC_IP_NAME_HOOK_MANAGER"] == "hook-manager-static-ip"
        assert subs["PLACEHOLDER_STATIC_IP_NAME_ADMIN"] == "admin-static-ip"
        assert subs["PLACEHOLDER_STATIC_IP_NAME_TALK"] == "talk-static-ip"
        assert subs["PLACEHOLDER_STATIC_IP_NAME_MEET"] == "meet-static-ip"
