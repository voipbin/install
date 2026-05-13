"""Single source of truth for VoIPBin Secret schemas (PR #4).

This module encodes:

1. The 53-key ``Secret/voipbin`` (ns ``bin-manager``) inventory derived from
   the production cluster extraction on 2026-05-12. Each entry carries the
   default placeholder/value the install repo will emit and a sensitivity
   class so downstream tooling can decide whether the key flows through
   sops or ``config.yaml`` substitution.

2. The 10-key ``Secret/voipbin`` (ns ``voip``) inventory.

3. The per-service env-wiring map for all 31 ``bin-*`` Deployments — the
   tuple ``(pod_env_name, secret_key_name)`` is the canonical rename
   contract. ``literal_env`` and ``field_env`` mirror the production
   manifest. ``ports`` is consumed by the manifest generator.

The four ``SSL_*_BASE64`` keys plus ``JWT_KEY`` are populated by
``scripts/tls_bootstrap.py`` on first ``voipbin-install init``. The
remaining ``secret``-class keys are operator-supplied via sops; ``config``
and ``dsn`` class keys default through this module.
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# 53-key bin-manager Secret inventory
# ---------------------------------------------------------------------------
# Class vocabulary: "secret" | "config" | "dsn" | "tls"
# Note: Operator-supplied rabbitmq_password must be URL-safe (no `@`, `:`, `/`, `?`);
# these chars corrupt the DSN userinfo per RFC 3986 when substituted into RABBITMQ_ADDRESS.
# The default values use PLACEHOLDER_* tokens where ``scripts/k8s.py`` does
# late-bound substitution, OR literal defaults that are safe to ship.

BIN_SECRET_KEYS: dict[str, dict[str, str]] = {
    "AUTHTOKEN_MESSAGEBIRD": {"default": "dummy-messagebird-token", "class": "secret"},
    "AUTHTOKEN_TELNYX": {"default": "dummy-telnyx-authtoken", "class": "secret"},
    "AWS_ACCESS_KEY": {"default": "dummy-aws-access-key", "class": "secret"},
    "AWS_SECRET_KEY": {"default": "dummy-aws-secret-key", "class": "secret"},
    "CARTESIA_API_KEY": {"default": "dummy-cartesia-key", "class": "secret"},
    "CLICKHOUSE_ADDRESS": {
        "default": "clickhouse.infrastructure.svc.cluster.local:9000",
        "class": "config",
    },
    "CLICKHOUSE_DATABASE": {"default": "default", "class": "config"},
    "DATABASE_DSN_ASTERISK": {
        "default": (
            "asterisk:PLACEHOLDER_DSN_PASSWORD_MYSQL_ASTERISK@tcp("
            "PLACEHOLDER_CLOUDSQL_MYSQL_PRIVATE_IP:3306)/asterisk"
        ),
        "class": "dsn",
    },
    "DATABASE_DSN_BIN": {
        "default": (
            "bin-manager:PLACEHOLDER_DSN_PASSWORD_MYSQL_BIN_MANAGER@tcp("
            "PLACEHOLDER_CLOUDSQL_MYSQL_PRIVATE_IP:3306)/bin_manager"
        ),
        "class": "dsn",
    },
    "DATABASE_DSN_POSTGRES": {
        "default": (
            "postgres://bin-manager:PLACEHOLDER_DSN_PASSWORD_POSTGRES_BIN_MANAGER@"
            "PLACEHOLDER_CLOUDSQL_POSTGRES_PRIVATE_IP:5432/"
            "bin_manager?sslmode=disable"
        ),
        "class": "dsn",
    },
    "DEEPGRAM_API_KEY": {"default": "dummy-deepgram-key", "class": "secret"},
    "DOMAIN_NAME_EXTENSION": {"default": "registrar.PLACEHOLDER_DOMAIN", "class": "config"},
    "DOMAIN_NAME_TRUNK": {"default": "trunk.PLACEHOLDER_DOMAIN", "class": "config"},
    "ELEVENLABS_API_KEY": {"default": "dummy-elevenlabs-key", "class": "secret"},
    "ENGINE_KEY_CHATGPT": {"default": "dummy-openai-engine-key", "class": "secret"},
    "EXTERNAL_SIP_GATEWAY_ADDRESSES": {"default": "", "class": "config"},
    "GCP_BUCKET_NAME_MEDIA": {
        "default": "PLACEHOLDER_PROJECT_ID-voipbin-media",
        "class": "config",
    },
    "GCP_BUCKET_NAME_TMP": {
        "default": "PLACEHOLDER_PROJECT_ID-voipbin-tmp",
        "class": "config",
    },
    "GCP_PROJECT_ID": {"default": "PLACEHOLDER_PROJECT_ID", "class": "config"},
    "GCP_PROJECT_NAME": {"default": "PLACEHOLDER_PROJECT_ID", "class": "config"},
    "GCP_REGION": {"default": "PLACEHOLDER_REGION", "class": "config"},
    "GOOGLE_API_KEY": {"default": "dummy-google-api-key", "class": "secret"},
    "HOMER_API_ADDRESS": {"default": "http://homer.local", "class": "config"},
    "HOMER_AUTH_TOKEN": {"default": "dummy-homer-token", "class": "secret"},
    "HOMER_WHITELIST": {"default": "", "class": "config"},
    "JWT_KEY": {"default": "PLACEHOLDER_JWT_KEY", "class": "secret"},
    "MAILGUN_API_KEY": {"default": "dummy-mailgun-key", "class": "secret"},
    "OPENAI_API_KEY": {"default": "dummy-openai-key", "class": "secret"},
    "PADDLE_API_KEY": {"default": "dummy-paddle-key", "class": "secret"},
    "PADDLE_PRICE_ID_BASIC": {"default": "", "class": "config"},
    "PADDLE_PRICE_ID_PROFESSIONAL": {"default": "", "class": "config"},
    "PADDLE_WEBHOOK_SECRET_KEY": {"default": "dummy-paddle-webhook-secret", "class": "secret"},
    "PROJECT_BASE_DOMAIN": {"default": "PLACEHOLDER_DOMAIN", "class": "config"},
    "PROJECT_BUCKET_NAME": {
        "default": "PLACEHOLDER_PROJECT_ID-voipbin-media",
        "class": "config",
    },
    "PROMETHEUS_ENDPOINT": {"default": "/metrics", "class": "config"},
    "PROMETHEUS_LISTEN_ADDRESS": {"default": ":2112", "class": "config"},
    "RABBITMQ_ADDRESS": {
        "default": "amqp://guest:PLACEHOLDER_RABBITMQ_PASSWORD@rabbitmq.infrastructure.svc.cluster.local:5672",
        "class": "config",
    },
    "REDIS_ADDRESS": {
        "default": "redis.infrastructure.svc.cluster.local:6379",
        "class": "config",
    },
    "REDIS_DATABASE": {"default": "1", "class": "config"},
    "REDIS_PASSWORD": {"default": "", "class": "secret"},
    "SENDGRID_API_KEY": {"default": "dummy-sendgrid-key", "class": "secret"},
    "SSL_CERT_API_BASE64": {"default": "PLACEHOLDER_SSL_CERT_API_BASE64", "class": "tls"},
    "SSL_CERT_HOOK_BASE64": {"default": "PLACEHOLDER_SSL_CERT_HOOK_BASE64", "class": "tls"},
    "SSL_PRIVKEY_API_BASE64": {"default": "PLACEHOLDER_SSL_PRIVKEY_API_BASE64", "class": "tls"},
    "SSL_PRIVKEY_HOOK_BASE64": {"default": "PLACEHOLDER_SSL_PRIVKEY_HOOK_BASE64", "class": "tls"},
    "STREAMING_LISTEN_PORT": {"default": "8080", "class": "config"},
    "STT_PROVIDER_PRIORITY": {"default": "GCP,AWS", "class": "config"},
    "TELNYX_CONNECTION_ID": {"default": "", "class": "config"},
    "TELNYX_PROFILE_ID": {"default": "", "class": "config"},
    "TELNYX_TOKEN": {"default": "dummy-telnyx-token", "class": "secret"},
    "TWILIO_SID": {"default": "dummy-twilio-sid", "class": "secret"},
    "TWILIO_TOKEN": {"default": "dummy-twilio-token", "class": "secret"},
    "XAI_API_KEY": {"default": "dummy-xai-key", "class": "secret"},
}

# Sanity: must be 53 keys.
assert len(BIN_SECRET_KEYS) == 53, f"BIN_SECRET_KEYS must have 53 entries (got {len(BIN_SECRET_KEYS)})"


# ---------------------------------------------------------------------------
# 10-key voip-ns Secret inventory
# ---------------------------------------------------------------------------

VOIP_SECRET_KEYS: dict[str, dict[str, str]] = {
    "DATABASE_ASTERISK_DATABASE": {"default": "asterisk", "class": "config"},
    "DATABASE_ASTERISK_HOST": {
        "default": "PLACEHOLDER_CLOUDSQL_PRIVATE_IP",
        "class": "config",
    },
    "DATABASE_ASTERISK_PASSWORD": {"default": "dummy-asterisk-password", "class": "secret"},
    "DATABASE_ASTERISK_PORT": {"default": "3306", "class": "config"},
    "DATABASE_ASTERISK_USERNAME": {"default": "asterisk", "class": "config"},
    "KAMAILIO_INTERNAL_LB_ADDRESS": {
        "default": "PLACEHOLDER_KAMAILIO_INTERNAL_LB_ADDRESS",
        "class": "config",
    },
    "KAMAILIO_INTERNAL_LB_NAME": {"default": "kamailio-internal-lb", "class": "config"},
    "RABBITMQ_ADDRESS": {
        "default": "amqp://guest:PLACEHOLDER_RABBITMQ_PASSWORD@rabbitmq.infrastructure.svc.cluster.local:5672",
        "class": "config",
    },
    "REDIS_ADDRESS": {
        "default": "redis.infrastructure.svc.cluster.local:6379",
        "class": "config",
    },
    "REDIS_PASSWORD": {"default": "", "class": "secret"},
}

assert len(VOIP_SECRET_KEYS) == 10


# ---------------------------------------------------------------------------
# Per-service env wiring for the 31 bin-* Deployments
# ---------------------------------------------------------------------------
# Schema per entry:
#   "ports":       list[(int, str)]    container ports + name
#   "secret_env":  list[(pod_env, secret_key)]
#   "field_env":   list[(pod_env, field_path)]  (optional)
#   "literal_env": list[(pod_env, literal_value)]
#
# Production-extracted on 2026-05-12 (see docs/plans/_bin_wiring_data.py.txt).

BIN_SERVICE_WIRING: dict[str, dict] = {
    "agent-manager": {
        "ports": [(2112, "metrics")],
        "secret_env": [
            ("DATABASE_DSN", "DATABASE_DSN_BIN"),
            ("RABBITMQ_ADDRESS", "RABBITMQ_ADDRESS"),
            ("REDIS_ADDRESS", "REDIS_ADDRESS"),
            ("REDIS_PASSWORD", "REDIS_PASSWORD"),
            ("REDIS_DATABASE", "REDIS_DATABASE"),
            ("CLICKHOUSE_ADDRESS", "CLICKHOUSE_ADDRESS"),
        ],
        "literal_env": [
            ("PROMETHEUS_ENDPOINT", "/metrics"),
            ("PROMETHEUS_LISTEN_ADDRESS", ":2112"),
        ],
    },
    "ai-manager": {
        "ports": [(2112, "metrics")],
        "secret_env": [
            ("DATABASE_DSN", "DATABASE_DSN_BIN"),
            ("RABBITMQ_ADDRESS", "RABBITMQ_ADDRESS"),
            ("REDIS_ADDRESS", "REDIS_ADDRESS"),
            ("REDIS_PASSWORD", "REDIS_PASSWORD"),
            ("REDIS_DATABASE", "REDIS_DATABASE"),
            ("ENGINE_KEY_CHATGPT", "OPENAI_API_KEY"),
            ("CLICKHOUSE_ADDRESS", "CLICKHOUSE_ADDRESS"),
        ],
        "literal_env": [
            ("PROMETHEUS_ENDPOINT", "/metrics"),
            ("PROMETHEUS_LISTEN_ADDRESS", ":2112"),
        ],
    },
    "api-manager": {
        "ports": [(2112, "metrics"), (443, "service"), (9000, "audiosocket")],
        "secret_env": [
            ("DATABASE_DSN", "DATABASE_DSN_BIN"),
            ("RABBITMQ_ADDRESS", "RABBITMQ_ADDRESS"),
            ("REDIS_ADDRESS", "REDIS_ADDRESS"),
            ("REDIS_PASSWORD", "REDIS_PASSWORD"),
            ("REDIS_DATABASE", "REDIS_DATABASE"),
            ("SSL_PRIVKEY_BASE64", "SSL_PRIVKEY_API_BASE64"),
            ("SSL_CERT_BASE64", "SSL_CERT_API_BASE64"),
            ("GCP_PROJECT_ID", "GCP_PROJECT_ID"),
            ("GCP_BUCKET_NAME", "GCP_BUCKET_NAME_TMP"),
            ("JWT_KEY", "JWT_KEY"),
        ],
        "field_env": [
            ("POD_NAME", "metadata.name"),
            ("POD_NAMESPACE", "metadata.namespace"),
            ("POD_IP", "status.podIP"),
        ],
        "literal_env": [
            ("PROMETHEUS_ENDPOINT", "/metrics"),
            ("PROMETHEUS_LISTEN_ADDRESS", ":2112"),
        ],
    },
    "billing-manager": {
        "ports": [(2112, "metrics")],
        "secret_env": [
            ("DATABASE_DSN", "DATABASE_DSN_BIN"),
            ("RABBITMQ_ADDRESS", "RABBITMQ_ADDRESS"),
            ("REDIS_ADDRESS", "REDIS_ADDRESS"),
            ("REDIS_PASSWORD", "REDIS_PASSWORD"),
            ("REDIS_DATABASE", "REDIS_DATABASE"),
            ("CLICKHOUSE_ADDRESS", "CLICKHOUSE_ADDRESS"),
            ("PADDLE_API_KEY", "PADDLE_API_KEY"),
            ("PADDLE_PRICE_ID_BASIC", "PADDLE_PRICE_ID_BASIC"),
            ("PADDLE_PRICE_ID_PROFESSIONAL", "PADDLE_PRICE_ID_PROFESSIONAL"),
        ],
        "literal_env": [
            ("PROMETHEUS_ENDPOINT", "/metrics"),
            ("PROMETHEUS_LISTEN_ADDRESS", ":2112"),
        ],
    },
    "call-manager": {
        "ports": [(2112, "metrics")],
        "secret_env": [
            ("DATABASE_DSN", "DATABASE_DSN_BIN"),
            ("RABBITMQ_ADDRESS", "RABBITMQ_ADDRESS"),
            ("REDIS_ADDRESS", "REDIS_ADDRESS"),
            ("REDIS_PASSWORD", "REDIS_PASSWORD"),
            ("REDIS_DATABASE", "REDIS_DATABASE"),
            ("HOMER_API_ADDRESS", "HOMER_API_ADDRESS"),
            ("HOMER_AUTH_TOKEN", "HOMER_AUTH_TOKEN"),
            ("HOMER_WHITELIST", "HOMER_WHITELIST"),
            ("PROJECT_BUCKET_NAME", "GCP_BUCKET_NAME_MEDIA"),
            ("CLICKHOUSE_ADDRESS", "CLICKHOUSE_ADDRESS"),
        ],
        "field_env": [
            ("NODE_IP", "status.hostIP"),
            ("POD_IP", "status.podIP"),
        ],
        "literal_env": [
            ("PROMETHEUS_ENDPOINT", "/metrics"),
            ("PROMETHEUS_LISTEN_ADDRESS", ":2112"),
            # NOTE: production literal is "voipbin.net" — install repo
            # MUST template to PLACEHOLDER_DOMAIN per design §4.3 callout 8.
            ("PROJECT_BASE_DOMAIN", "PLACEHOLDER_DOMAIN"),
        ],
    },
    "campaign-manager": {
        "ports": [(2112, "metrics")],
        "secret_env": [
            ("DATABASE_DSN", "DATABASE_DSN_BIN"),
            ("RABBITMQ_ADDRESS", "RABBITMQ_ADDRESS"),
            ("REDIS_ADDRESS", "REDIS_ADDRESS"),
            ("REDIS_PASSWORD", "REDIS_PASSWORD"),
            ("REDIS_DATABASE", "REDIS_DATABASE"),
            ("CLICKHOUSE_ADDRESS", "CLICKHOUSE_ADDRESS"),
        ],
        "literal_env": [
            ("PROMETHEUS_ENDPOINT", "/metrics"),
            ("PROMETHEUS_LISTEN_ADDRESS", ":2112"),
        ],
    },
    "conference-manager": {
        "ports": [(2112, "metrics")],
        "secret_env": [
            ("DATABASE_DSN", "DATABASE_DSN_BIN"),
            ("RABBITMQ_ADDRESS", "RABBITMQ_ADDRESS"),
            ("REDIS_ADDRESS", "REDIS_ADDRESS"),
            ("REDIS_PASSWORD", "REDIS_PASSWORD"),
            ("REDIS_DATABASE", "REDIS_DATABASE"),
            ("CLICKHOUSE_ADDRESS", "CLICKHOUSE_ADDRESS"),
        ],
        "literal_env": [
            ("PROMETHEUS_ENDPOINT", "/metrics"),
            ("PROMETHEUS_LISTEN_ADDRESS", ":2112"),
        ],
    },
    "contact-manager": {
        "ports": [(2112, "metrics")],
        "secret_env": [
            ("DATABASE_DSN", "DATABASE_DSN_BIN"),
            ("RABBITMQ_ADDRESS", "RABBITMQ_ADDRESS"),
            ("REDIS_ADDRESS", "REDIS_ADDRESS"),
            ("REDIS_PASSWORD", "REDIS_PASSWORD"),
            ("REDIS_DATABASE", "REDIS_DATABASE"),
            ("CLICKHOUSE_ADDRESS", "CLICKHOUSE_ADDRESS"),
        ],
        "literal_env": [
            ("PROMETHEUS_ENDPOINT", "/metrics"),
            ("PROMETHEUS_LISTEN_ADDRESS", ":2112"),
        ],
    },
    "conversation-manager": {
        "ports": [(2112, "metrics")],
        "secret_env": [
            ("DATABASE_DSN", "DATABASE_DSN_BIN"),
            ("RABBITMQ_ADDRESS", "RABBITMQ_ADDRESS"),
            ("REDIS_ADDRESS", "REDIS_ADDRESS"),
            ("REDIS_PASSWORD", "REDIS_PASSWORD"),
            ("REDIS_DATABASE", "REDIS_DATABASE"),
            ("CLICKHOUSE_ADDRESS", "CLICKHOUSE_ADDRESS"),
        ],
        "literal_env": [
            ("PROMETHEUS_ENDPOINT", "/metrics"),
            ("PROMETHEUS_LISTEN_ADDRESS", ":2112"),
        ],
    },
    "customer-manager": {
        "ports": [(2112, "metrics")],
        "secret_env": [
            ("DATABASE_DSN", "DATABASE_DSN_BIN"),
            ("RABBITMQ_ADDRESS", "RABBITMQ_ADDRESS"),
            ("REDIS_ADDRESS", "REDIS_ADDRESS"),
            ("REDIS_PASSWORD", "REDIS_PASSWORD"),
            ("REDIS_DATABASE", "REDIS_DATABASE"),
            ("CLICKHOUSE_ADDRESS", "CLICKHOUSE_ADDRESS"),
        ],
        "literal_env": [
            ("PROMETHEUS_ENDPOINT", "/metrics"),
            ("PROMETHEUS_LISTEN_ADDRESS", ":2112"),
        ],
    },
    "direct-manager": {
        "ports": [(2112, "metrics"), (80, "grpc")],
        "secret_env": [
            ("DATABASE_DSN", "DATABASE_DSN_BIN"),
            ("RABBITMQ_ADDRESS", "RABBITMQ_ADDRESS"),
            ("REDIS_ADDRESS", "REDIS_ADDRESS"),
            ("REDIS_PASSWORD", "REDIS_PASSWORD"),
            ("REDIS_DATABASE", "REDIS_DATABASE"),
        ],
        "field_env": [
            ("NODE_IP", "status.hostIP"),
            ("POD_IP", "status.podIP"),
        ],
        "literal_env": [
            ("PROMETHEUS_ENDPOINT", "/metrics"),
            ("PROMETHEUS_LISTEN_ADDRESS", ":2112"),
        ],
    },
    "email-manager": {
        "ports": [(2112, "metrics"), (80, "grpc")],
        "secret_env": [
            ("DATABASE_DSN", "DATABASE_DSN_BIN"),
            ("RABBITMQ_ADDRESS", "RABBITMQ_ADDRESS"),
            ("REDIS_ADDRESS", "REDIS_ADDRESS"),
            ("REDIS_PASSWORD", "REDIS_PASSWORD"),
            ("REDIS_DATABASE", "REDIS_DATABASE"),
            ("SENDGRID_API_KEY", "SENDGRID_API_KEY"),
            ("MAILGUN_API_KEY", "MAILGUN_API_KEY"),
            ("CLICKHOUSE_ADDRESS", "CLICKHOUSE_ADDRESS"),
        ],
        "field_env": [
            ("NODE_IP", "status.hostIP"),
            ("POD_IP", "status.podIP"),
        ],
        "literal_env": [
            ("PROMETHEUS_ENDPOINT", "/metrics"),
            ("PROMETHEUS_LISTEN_ADDRESS", ":2112"),
        ],
    },
    "flow-manager": {
        "ports": [(2112, "metrics"), (80, "grpc")],
        "secret_env": [
            ("DATABASE_DSN", "DATABASE_DSN_BIN"),
            ("RABBITMQ_ADDRESS", "RABBITMQ_ADDRESS"),
            ("REDIS_ADDRESS", "REDIS_ADDRESS"),
            ("REDIS_PASSWORD", "REDIS_PASSWORD"),
            ("REDIS_DATABASE", "REDIS_DATABASE"),
            ("CLICKHOUSE_ADDRESS", "CLICKHOUSE_ADDRESS"),
        ],
        "field_env": [
            ("NODE_IP", "status.hostIP"),
            ("POD_IP", "status.podIP"),
        ],
        "literal_env": [
            ("PROMETHEUS_ENDPOINT", "/metrics"),
            ("PROMETHEUS_LISTEN_ADDRESS", ":2112"),
        ],
    },
    "hook-manager": {
        "ports": [(2112, "metrics"), (443, "service-https"), (80, "service-http")],
        "secret_env": [
            ("DATABASE_DSN", "DATABASE_DSN_BIN"),
            ("RABBITMQ_ADDRESS", "RABBITMQ_ADDRESS"),
            ("REDIS_ADDRESS", "REDIS_ADDRESS"),
            ("REDIS_PASSWORD", "REDIS_PASSWORD"),
            ("REDIS_DATABASE", "REDIS_DATABASE"),
            ("SSL_PRIVKEY_BASE64", "SSL_PRIVKEY_HOOK_BASE64"),
            ("SSL_CERT_BASE64", "SSL_CERT_HOOK_BASE64"),
            ("PADDLE_WEBHOOK_SECRET_KEY", "PADDLE_WEBHOOK_SECRET_KEY"),
        ],
        "literal_env": [
            ("PROMETHEUS_ENDPOINT", "/metrics"),
            ("PROMETHEUS_LISTEN_ADDRESS", ":2112"),
        ],
    },
    "message-manager": {
        "ports": [(2112, "metrics")],
        "secret_env": [
            ("DATABASE_DSN", "DATABASE_DSN_BIN"),
            ("RABBITMQ_ADDRESS", "RABBITMQ_ADDRESS"),
            ("REDIS_ADDRESS", "REDIS_ADDRESS"),
            ("REDIS_PASSWORD", "REDIS_PASSWORD"),
            ("REDIS_DATABASE", "REDIS_DATABASE"),
            ("AUTHTOKEN_MESSAGEBIRD", "AUTHTOKEN_MESSAGEBIRD"),
            ("AUTHTOKEN_TELNYX", "TELNYX_TOKEN"),
            ("CLICKHOUSE_ADDRESS", "CLICKHOUSE_ADDRESS"),
        ],
        "literal_env": [
            ("PROMETHEUS_ENDPOINT", "/metrics"),
            ("PROMETHEUS_LISTEN_ADDRESS", ":2112"),
        ],
    },
    "number-manager": {
        "ports": [(2112, "metrics")],
        "secret_env": [
            ("DATABASE_DSN", "DATABASE_DSN_BIN"),
            ("RABBITMQ_ADDRESS", "RABBITMQ_ADDRESS"),
            ("REDIS_ADDRESS", "REDIS_ADDRESS"),
            ("REDIS_PASSWORD", "REDIS_PASSWORD"),
            ("REDIS_DATABASE", "REDIS_DATABASE"),
            ("TWILIO_SID", "TWILIO_SID"),
            ("TWILIO_TOKEN", "TWILIO_TOKEN"),
            ("TELNYX_CONNECTION_ID", "TELNYX_CONNECTION_ID"),
            ("TELNYX_PROFILE_ID", "TELNYX_PROFILE_ID"),
            ("TELNYX_TOKEN", "TELNYX_TOKEN"),
            ("CLICKHOUSE_ADDRESS", "CLICKHOUSE_ADDRESS"),
        ],
        "literal_env": [
            ("PROMETHEUS_ENDPOINT", "/metrics"),
            ("PROMETHEUS_LISTEN_ADDRESS", ":2112"),
        ],
    },
    "outdial-manager": {
        "ports": [(2112, "metrics")],
        "secret_env": [
            ("DATABASE_DSN", "DATABASE_DSN_BIN"),
            ("RABBITMQ_ADDRESS", "RABBITMQ_ADDRESS"),
            ("REDIS_ADDRESS", "REDIS_ADDRESS"),
            ("REDIS_PASSWORD", "REDIS_PASSWORD"),
            ("REDIS_DATABASE", "REDIS_DATABASE"),
            ("CLICKHOUSE_ADDRESS", "CLICKHOUSE_ADDRESS"),
        ],
        "literal_env": [
            ("PROMETHEUS_ENDPOINT", "/metrics"),
            ("PROMETHEUS_LISTEN_ADDRESS", ":2112"),
        ],
    },
    "pipecat-manager": {
        "ports": [(2112, "metrics"), (8080, "audiosocket")],
        "secret_env": [
            ("DATABASE_DSN", "DATABASE_DSN_BIN"),
            ("RABBITMQ_ADDRESS", "RABBITMQ_ADDRESS"),
            ("REDIS_ADDRESS", "REDIS_ADDRESS"),
            ("REDIS_PASSWORD", "REDIS_PASSWORD"),
            ("REDIS_DATABASE", "REDIS_DATABASE"),
            ("CARTESIA_API_KEY", "CARTESIA_API_KEY"),
            ("ELEVENLABS_API_KEY", "ELEVENLABS_API_KEY"),
            ("OPENAI_API_KEY", "OPENAI_API_KEY"),
            ("DEEPGRAM_API_KEY", "DEEPGRAM_API_KEY"),
            ("XAI_API_KEY", "XAI_API_KEY"),
            ("GOOGLE_API_KEY", "GOOGLE_API_KEY"),
            ("CLICKHOUSE_ADDRESS", "CLICKHOUSE_ADDRESS"),
        ],
        "field_env": [
            ("NODE_IP", "status.hostIP"),
            ("POD_IP", "status.podIP"),
        ],
        "literal_env": [
            ("PROMETHEUS_ENDPOINT", "/metrics"),
            ("PROMETHEUS_LISTEN_ADDRESS", ":2112"),
        ],
    },
    "queue-manager": {
        "ports": [(2112, "metrics")],
        "secret_env": [
            ("DATABASE_DSN", "DATABASE_DSN_BIN"),
            ("RABBITMQ_ADDRESS", "RABBITMQ_ADDRESS"),
            ("REDIS_ADDRESS", "REDIS_ADDRESS"),
            ("REDIS_PASSWORD", "REDIS_PASSWORD"),
            ("REDIS_DATABASE", "REDIS_DATABASE"),
            ("CLICKHOUSE_ADDRESS", "CLICKHOUSE_ADDRESS"),
        ],
        "literal_env": [
            ("PROMETHEUS_ENDPOINT", "/metrics"),
            ("PROMETHEUS_LISTEN_ADDRESS", ":2112"),
        ],
    },
    "rag-manager": {
        "ports": [(2112, "metrics")],
        "secret_env": [
            ("RABBITMQ_ADDRESS", "RABBITMQ_ADDRESS"),
            ("GCP_PROJECT_ID", "GCP_PROJECT_ID"),
            ("GCP_REGION", "GCP_REGION"),
            ("POSTGRESQL_DSN", "DATABASE_DSN_POSTGRES"),
        ],
        "literal_env": [
            ("GOOGLE_EMBEDDING_MODEL", "text-embedding-004"),
            ("RAG_TOP_K", "5"),
            ("PROMETHEUS_ENDPOINT", "/metrics"),
            ("PROMETHEUS_LISTEN_ADDRESS", ":2112"),
        ],
    },
    "registrar-manager": {
        "ports": [(2112, "metrics")],
        # NOTE: registrar-manager is the UNIQUE exception that does NOT
        # rename DATABASE_DSN_BIN. See design §4.3 callout 1.
        "secret_env": [
            ("DATABASE_DSN_BIN", "DATABASE_DSN_BIN"),
            ("DATABASE_DSN_ASTERISK", "DATABASE_DSN_ASTERISK"),
            ("RABBITMQ_ADDRESS", "RABBITMQ_ADDRESS"),
            ("REDIS_ADDRESS", "REDIS_ADDRESS"),
            ("REDIS_PASSWORD", "REDIS_PASSWORD"),
            ("REDIS_DATABASE", "REDIS_DATABASE"),
            ("DOMAIN_NAME_EXTENSION", "DOMAIN_NAME_EXTENSION"),
            ("DOMAIN_NAME_TRUNK", "DOMAIN_NAME_TRUNK"),
            ("CLICKHOUSE_ADDRESS", "CLICKHOUSE_ADDRESS"),
        ],
        "literal_env": [
            ("PROMETHEUS_ENDPOINT", "/metrics"),
            ("PROMETHEUS_LISTEN_ADDRESS", ":2112"),
        ],
    },
    "route-manager": {
        "ports": [(2112, "metrics")],
        "secret_env": [
            ("DATABASE_DSN", "DATABASE_DSN_BIN"),
            ("RABBITMQ_ADDRESS", "RABBITMQ_ADDRESS"),
            ("REDIS_ADDRESS", "REDIS_ADDRESS"),
            ("REDIS_PASSWORD", "REDIS_PASSWORD"),
            ("REDIS_DATABASE", "REDIS_DATABASE"),
            ("CLICKHOUSE_ADDRESS", "CLICKHOUSE_ADDRESS"),
            ("EXTERNAL_SIP_GATEWAY_ADDRESSES", "EXTERNAL_SIP_GATEWAY_ADDRESSES"),
        ],
        "literal_env": [
            ("PROMETHEUS_ENDPOINT", "/metrics"),
            ("PROMETHEUS_LISTEN_ADDRESS", ":2112"),
        ],
    },
    "sentinel-manager": {
        "ports": [(2112, "metrics"), (80, "grpc")],
        "secret_env": [
            ("RABBITMQ_ADDRESS", "RABBITMQ_ADDRESS"),
            ("CLICKHOUSE_ADDRESS", "CLICKHOUSE_ADDRESS"),
        ],
        "literal_env": [
            ("PROMETHEUS_ENDPOINT", "/metrics"),
            ("PROMETHEUS_LISTEN_ADDRESS", ":2112"),
        ],
    },
    "storage-manager": {
        "ports": [(2112, "metrics")],
        "secret_env": [
            ("DATABASE_DSN", "DATABASE_DSN_BIN"),
            ("RABBITMQ_ADDRESS", "RABBITMQ_ADDRESS"),
            ("REDIS_ADDRESS", "REDIS_ADDRESS"),
            ("REDIS_PASSWORD", "REDIS_PASSWORD"),
            ("REDIS_DATABASE", "REDIS_DATABASE"),
            ("GCP_PROJECT_ID", "GCP_PROJECT_ID"),
            ("GCP_BUCKET_NAME_TMP", "GCP_BUCKET_NAME_TMP"),
            ("GCP_BUCKET_NAME_MEDIA", "GCP_BUCKET_NAME_MEDIA"),
            ("JWT_KEY", "JWT_KEY"),
            ("CLICKHOUSE_ADDRESS", "CLICKHOUSE_ADDRESS"),
        ],
        "literal_env": [
            ("PROMETHEUS_ENDPOINT", "/metrics"),
            ("PROMETHEUS_LISTEN_ADDRESS", ":2112"),
        ],
    },
    "tag-manager": {
        "ports": [(2112, "metrics")],
        "secret_env": [
            ("DATABASE_DSN", "DATABASE_DSN_BIN"),
            ("RABBITMQ_ADDRESS", "RABBITMQ_ADDRESS"),
            ("REDIS_ADDRESS", "REDIS_ADDRESS"),
            ("REDIS_PASSWORD", "REDIS_PASSWORD"),
            ("REDIS_DATABASE", "REDIS_DATABASE"),
            ("CLICKHOUSE_ADDRESS", "CLICKHOUSE_ADDRESS"),
        ],
        "literal_env": [
            ("PROMETHEUS_ENDPOINT", "/metrics"),
            ("PROMETHEUS_LISTEN_ADDRESS", ":2112"),
        ],
    },
    "talk-manager": {
        "ports": [(2112, "metrics")],
        "secret_env": [
            ("DATABASE_DSN", "DATABASE_DSN_BIN"),
            ("RABBITMQ_ADDRESS", "RABBITMQ_ADDRESS"),
            ("REDIS_ADDRESS", "REDIS_ADDRESS"),
            ("REDIS_PASSWORD", "REDIS_PASSWORD"),
            ("REDIS_DATABASE", "REDIS_DATABASE"),
            ("CLICKHOUSE_ADDRESS", "CLICKHOUSE_ADDRESS"),
        ],
        "literal_env": [
            ("PROMETHEUS_ENDPOINT", "/metrics"),
            ("PROMETHEUS_LISTEN_ADDRESS", ":2112"),
        ],
    },
    "timeline-manager": {
        "ports": [(2112, "metrics")],
        "secret_env": [
            ("CLICKHOUSE_ADDRESS", "CLICKHOUSE_ADDRESS"),
            ("CLICKHOUSE_DATABASE", "CLICKHOUSE_DATABASE"),
            ("RABBITMQ_ADDRESS", "RABBITMQ_ADDRESS"),
            ("HOMER_API_ADDRESS", "HOMER_API_ADDRESS"),
            ("HOMER_AUTH_TOKEN", "HOMER_AUTH_TOKEN"),
            ("GCS_BUCKET_NAME", "GCP_BUCKET_NAME_MEDIA"),
        ],
        "field_env": [
            ("NODE_IP", "status.hostIP"),
            ("POD_IP", "status.podIP"),
        ],
        "literal_env": [
            ("PROMETHEUS_ENDPOINT", "/metrics"),
            ("PROMETHEUS_LISTEN_ADDRESS", ":2112"),
        ],
    },
    "transcribe-manager": {
        "ports": [(2112, "metrics"), (8080, "audiosocket")],
        "secret_env": [
            ("DATABASE_DSN", "DATABASE_DSN_BIN"),
            ("RABBITMQ_ADDRESS", "RABBITMQ_ADDRESS"),
            ("REDIS_ADDRESS", "REDIS_ADDRESS"),
            ("REDIS_PASSWORD", "REDIS_PASSWORD"),
            ("REDIS_DATABASE", "REDIS_DATABASE"),
            ("AWS_ACCESS_KEY", "AWS_ACCESS_KEY"),
            ("AWS_SECRET_KEY", "AWS_SECRET_KEY"),
            ("CLICKHOUSE_ADDRESS", "CLICKHOUSE_ADDRESS"),
        ],
        "field_env": [
            ("NODE_IP", "status.hostIP"),
            ("POD_IP", "status.podIP"),
        ],
        "literal_env": [
            ("PROMETHEUS_ENDPOINT", "/metrics"),
            ("PROMETHEUS_LISTEN_ADDRESS", ":2112"),
            ("STREAMING_LISTEN_PORT", "8080"),
            ("STT_PROVIDER_PRIORITY", "GCP,AWS"),
        ],
    },
    "transfer-manager": {
        "ports": [(2112, "metrics")],
        "secret_env": [
            ("DATABASE_DSN", "DATABASE_DSN_BIN"),
            ("RABBITMQ_ADDRESS", "RABBITMQ_ADDRESS"),
            ("REDIS_ADDRESS", "REDIS_ADDRESS"),
            ("REDIS_PASSWORD", "REDIS_PASSWORD"),
            ("REDIS_DATABASE", "REDIS_DATABASE"),
            ("CLICKHOUSE_ADDRESS", "CLICKHOUSE_ADDRESS"),
        ],
        "field_env": [
            ("NODE_IP", "status.hostIP"),
            ("POD_IP", "status.podIP"),
        ],
        "literal_env": [
            ("PROMETHEUS_ENDPOINT", "/metrics"),
            ("PROMETHEUS_LISTEN_ADDRESS", ":2112"),
        ],
    },
    "tts-manager": {
        "ports": [(2112, "metrics")],
        "secret_env": [
            ("DATABASE_DSN", "DATABASE_DSN_BIN"),
            ("RABBITMQ_ADDRESS", "RABBITMQ_ADDRESS"),
            ("REDIS_ADDRESS", "REDIS_ADDRESS"),
            ("REDIS_PASSWORD", "REDIS_PASSWORD"),
            ("REDIS_DATABASE", "REDIS_DATABASE"),
            ("AWS_ACCESS_KEY", "AWS_ACCESS_KEY"),
            ("AWS_SECRET_KEY", "AWS_SECRET_KEY"),
            ("ELEVENLABS_API_KEY", "ELEVENLABS_API_KEY"),
            ("CLICKHOUSE_ADDRESS", "CLICKHOUSE_ADDRESS"),
        ],
        "field_env": [
            ("POD_NAME", "metadata.name"),
            ("POD_NAMESPACE", "metadata.namespace"),
            ("POD_IP", "status.podIP"),
        ],
        "literal_env": [
            ("PROMETHEUS_ENDPOINT", "/metrics"),
            ("PROMETHEUS_LISTEN_ADDRESS", ":2112"),
        ],
    },
    "webhook-manager": {
        "ports": [(2112, "metrics")],
        "secret_env": [
            ("DATABASE_DSN", "DATABASE_DSN_BIN"),
            ("RABBITMQ_ADDRESS", "RABBITMQ_ADDRESS"),
            ("REDIS_ADDRESS", "REDIS_ADDRESS"),
            ("REDIS_PASSWORD", "REDIS_PASSWORD"),
            ("REDIS_DATABASE", "REDIS_DATABASE"),
            ("CLICKHOUSE_ADDRESS", "CLICKHOUSE_ADDRESS"),
        ],
        "literal_env": [
            ("PROMETHEUS_ENDPOINT", "/metrics"),
            ("PROMETHEUS_LISTEN_ADDRESS", ":2112"),
        ],
    },
}

assert len(BIN_SERVICE_WIRING) == 31, (
    f"BIN_SERVICE_WIRING must have 31 services (got {len(BIN_SERVICE_WIRING)})"
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def sops_editable_keys() -> set[str]:
    """Return the set of keys the operator may edit via sops `secrets.yaml`.

    Per design §5.3:
      - 22 bin-ns secret-class keys
      - 3 bin-ns dsn-class keys
      - 1 voip-ns DATABASE_ASTERISK_PASSWORD
    = 26 operator-editable keys. (The 5 init-generated keys —
    JWT_KEY + 4 SSL_*_BASE64 — also end up in secrets.yaml but are
    managed by tls_bootstrap.py; not in this set.)
    """
    bin_secret = {
        k for k, m in BIN_SECRET_KEYS.items()
        if m["class"] == "secret" and k != "JWT_KEY"
        and not k.startswith("SSL_")
    }
    bin_dsn = {k for k, m in BIN_SECRET_KEYS.items() if m["class"] == "dsn"}
    voip_secret = {"DATABASE_ASTERISK_PASSWORD"}
    return bin_secret | bin_dsn | voip_secret


def init_generated_keys() -> set[str]:
    """Five keys generated by tls_bootstrap.py and persisted into secrets.yaml."""
    return {
        "JWT_KEY",
        "SSL_CERT_API_BASE64",
        "SSL_PRIVKEY_API_BASE64",
        "SSL_CERT_HOOK_BASE64",
        "SSL_PRIVKEY_HOOK_BASE64",
    }


# ---------------------------------------------------------------------------
# PR-Z Phase B: Kamailio TLS keys
# ---------------------------------------------------------------------------
# Six keys managed by ``scripts.cert_lifecycle.seed_kamailio_certs``. Two CA
# keys are only present in self_signed mode (omitted in manual mode); the
# four per-SAN leaf keys are present in both modes. All are optional from a
# schema standpoint — the audit lives in cert_lifecycle, not the secret
# validator — but they must be in the allow-list so secretmgr does not
# reject ``secrets.yaml`` for "unknown key" when cert_provision adds them.
KAMAILIO_TLS_KEYS: frozenset[str] = frozenset({
    "KAMAILIO_CA_CERT_BASE64",
    "KAMAILIO_CA_KEY_BASE64",
    "KAMAILIO_CERT_SIP_BASE64",
    "KAMAILIO_PRIVKEY_SIP_BASE64",
    "KAMAILIO_CERT_REGISTRAR_BASE64",
    "KAMAILIO_PRIVKEY_REGISTRAR_BASE64",
})


def kamailio_tls_keys() -> set[str]:
    """Return the PR-Z Kamailio TLS keys allow-listed in secrets.yaml."""
    return set(KAMAILIO_TLS_KEYS)


def all_allowed_secrets_yaml_keys() -> set[str]:
    """Union: operator-editable + init-generated + PR-Z Kamailio TLS keys."""
    return sops_editable_keys() | init_generated_keys() | kamailio_tls_keys()
