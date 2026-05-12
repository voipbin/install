"""Tests for scripts/k8s.py — schema-driven placeholder substitution (PR #4)."""

import pytest

from scripts.k8s import _build_substitution_map
from scripts.secret_schema import BIN_SECRET_KEYS, VOIP_SECRET_KEYS


class FakeConfig:
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
        "kamailio_internal_lb_address": "10.99.0.5",
    })


@pytest.fixture
def sample_secrets():
    # New schema: uppercase keys matching Secret keys.
    return {
        "JWT_KEY": "test-jwt",
        "OPENAI_API_KEY": "sk-fake",
        "REDIS_PASSWORD": "rpass",
    }


@pytest.fixture
def sample_tf_outputs():
    return {
        "cloudsql_instance_name": "prod-mysql",
        "cloudsql_proxy_sa_name": "sa-proxy",
        "recording_bucket_name": "my-project-123-recordings",
    }


class TestBuildSubstitutionMap:
    def test_all_53_bin_keys_have_placeholders(self, sample_config, sample_secrets, sample_tf_outputs):
        subs = _build_substitution_map(sample_config, sample_tf_outputs, sample_secrets)
        for key in BIN_SECRET_KEYS:
            assert f"PLACEHOLDER_{key}" in subs

    def test_all_10_voip_keys_have_placeholders(self, sample_config, sample_secrets, sample_tf_outputs):
        subs = _build_substitution_map(sample_config, sample_tf_outputs, sample_secrets)
        for key in VOIP_SECRET_KEYS:
            assert f"PLACEHOLDER_{key}" in subs

    def test_sops_secret_overrides_schema_default(self, sample_config, sample_secrets, sample_tf_outputs):
        subs = _build_substitution_map(sample_config, sample_tf_outputs, sample_secrets)
        assert subs["PLACEHOLDER_JWT_KEY"] == "test-jwt"
        assert subs["PLACEHOLDER_OPENAI_API_KEY"] == "sk-fake"
        assert subs["PLACEHOLDER_REDIS_PASSWORD"] == "rpass"

    def test_schema_default_when_secret_absent(self, sample_config, sample_tf_outputs):
        subs = _build_substitution_map(sample_config, sample_tf_outputs, {})
        # OPENAI_API_KEY default per secret_schema.
        assert subs["PLACEHOLDER_OPENAI_API_KEY"] == "dummy-openai-key"
        # CLICKHOUSE_ADDRESS default.
        assert "clickhouse.infrastructure" in subs["PLACEHOLDER_CLICKHOUSE_ADDRESS"]

    def test_top_level_tokens(self, sample_config, sample_secrets, sample_tf_outputs):
        subs = _build_substitution_map(sample_config, sample_tf_outputs, sample_secrets)
        assert subs["PLACEHOLDER_DOMAIN"] == "voipbin.example.com"
        assert subs["PLACEHOLDER_PROJECT_ID"] == "my-project-123"
        assert subs["PLACEHOLDER_REGION"] == "us-central1"
        assert subs["PLACEHOLDER_ACME_EMAIL"] == "admin@voipbin.example.com"

    def test_kamailio_lb_from_config(self, sample_config, sample_secrets, sample_tf_outputs):
        subs = _build_substitution_map(sample_config, sample_tf_outputs, sample_secrets)
        assert subs["PLACEHOLDER_KAMAILIO_INTERNAL_LB_ADDRESS"] == "10.99.0.5"
        assert subs["PLACEHOLDER_KAMAILIO_INTERNAL_LB_NAME"] == "kamailio-internal-lb"

    def test_terraform_outputs_mapped(self, sample_config, sample_secrets, sample_tf_outputs):
        subs = _build_substitution_map(sample_config, sample_tf_outputs, sample_secrets)
        assert subs["PLACEHOLDER_INSTANCE_NAME"] == "prod-mysql"
        assert subs["PLACEHOLDER_CLOUDSQL_SA"] == "sa-proxy"
        assert subs["PLACEHOLDER_RECORDING_BUCKET_NAME"] == "my-project-123-recordings"

    def test_static_ip_tokens_fallback(self, sample_config, sample_secrets):
        subs = _build_substitution_map(sample_config, {}, sample_secrets)
        assert subs["PLACEHOLDER_STATIC_IP_NAME_API_MANAGER"] == "api-manager-static-ip"
        assert subs["PLACEHOLDER_STATIC_IP_ADDRESS_API_MANAGER"] == ""

    def test_static_ip_addresses_from_terraform(self, sample_config, sample_secrets):
        tf = {
            "api_manager_static_ip_address": "10.0.0.1",
            "hook_manager_static_ip_address": "10.0.0.2",
            "admin_static_ip_address": "10.0.0.3",
            "talk_static_ip_address": "10.0.0.4",
            "meet_static_ip_address": "10.0.0.5",
        }
        subs = _build_substitution_map(sample_config, tf, sample_secrets)
        assert subs["PLACEHOLDER_STATIC_IP_ADDRESS_API_MANAGER"] == "10.0.0.1"
        assert subs["PLACEHOLDER_STATIC_IP_ADDRESS_MEET"] == "10.0.0.5"

    def test_obsolete_tokens_absent(self, sample_config, sample_secrets, sample_tf_outputs):
        subs = _build_substitution_map(sample_config, sample_tf_outputs, sample_secrets)
        # PR #4 removed these from the schema (legacy backend Secret/ConfigMap
        # keys no longer in 53-key voipbin Secret).
        # Note: PLACEHOLDER_RABBITMQ_USER / PLACEHOLDER_RABBITMQ_PASSWORD remain
        # because k8s/infrastructure/rabbitmq/secret.yaml still seeds broker
        # bootstrap credentials (separate from the bin-* RABBITMQ_ADDRESS).
        for obsolete in (
            "PLACEHOLDER_DB_USER",
            "PLACEHOLDER_DB_PASSWORD",
            "PLACEHOLDER_DB_NAME",
            "PLACEHOLDER_API_SIGNING_KEY",
        ):
            assert obsolete not in subs, f"{obsolete} should have been removed"
