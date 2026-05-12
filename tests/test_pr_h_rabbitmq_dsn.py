"""PR-H — RABBITMQ_ADDRESS DSN password-substitution tests.

Replaces the redaction artifact ``***`` in the schema-default DSN with the
standalone substitution token ``PLACEHOLDER_RABBITMQ_PASSWORD`` so the
existing longest-first substitution loop at ``scripts/k8s.py:147-148`` injects
the operator-supplied password into the rendered Secret.
"""

from pathlib import Path

import yaml

from scripts.k8s import _build_substitution_map
from scripts.secret_schema import BIN_SECRET_KEYS, VOIP_SECRET_KEYS


REPO_ROOT = Path(__file__).resolve().parent.parent
EXPECTED_DSN = (
    "amqp://guest:PLACEHOLDER_RABBITMQ_PASSWORD"
    "@rabbitmq.infrastructure.svc.cluster.local:5672"
)


class FakeConfig:
    def __init__(self, data: dict):
        self._data = data

    def get(self, key, default=None):
        return self._data.get(key, default)


def test_bin_rabbitmq_default_contains_placeholder():
    default = BIN_SECRET_KEYS["RABBITMQ_ADDRESS"]["default"]
    assert "PLACEHOLDER_RABBITMQ_PASSWORD" in default
    # Redaction artifact must be gone.
    assert "***" not in default
    assert default == EXPECTED_DSN


def test_voip_rabbitmq_default_contains_placeholder():
    default = VOIP_SECRET_KEYS["RABBITMQ_ADDRESS"]["default"]
    assert "PLACEHOLDER_RABBITMQ_PASSWORD" in default
    assert "***" not in default
    assert default == EXPECTED_DSN


def test_bin_and_voip_rabbitmq_defaults_match():
    # Both Secrets must remain byte-for-byte identical post-render
    # (the existing 31-service wiring assumes a single RabbitMQ topology).
    assert (
        BIN_SECRET_KEYS["RABBITMQ_ADDRESS"]["default"]
        == VOIP_SECRET_KEYS["RABBITMQ_ADDRESS"]["default"]
    )


def test_rendered_secret_yaml_matches_schema():
    backend = yaml.safe_load(
        (REPO_ROOT / "k8s" / "backend" / "secret.yaml").read_text()
    )
    voip = yaml.safe_load((REPO_ROOT / "k8s" / "voip" / "secret.yaml").read_text())
    assert (
        backend["stringData"]["RABBITMQ_ADDRESS"]
        == BIN_SECRET_KEYS["RABBITMQ_ADDRESS"]["default"]
    )
    assert (
        voip["stringData"]["RABBITMQ_ADDRESS"]
        == VOIP_SECRET_KEYS["RABBITMQ_ADDRESS"]["default"]
    )


def _substitute(subs: dict, rendered: str) -> str:
    """Mirror the longest-first substitution loop at scripts/k8s.py:147-148."""
    for token in sorted(subs, key=len, reverse=True):
        rendered = rendered.replace(token, subs[token])
    return rendered


def test_substitution_injects_password_into_dsn():
    cfg = FakeConfig({"domain": "voipbin.example.com"})
    subs = _build_substitution_map(cfg, {}, {"rabbitmq_password": "hunter2"})
    template = BIN_SECRET_KEYS["RABBITMQ_ADDRESS"]["default"]
    rendered = _substitute(subs, template)
    assert rendered == (
        "amqp://guest:hunter2@rabbitmq.infrastructure.svc.cluster.local:5672"
    )
    assert "PLACEHOLDER_" not in rendered


def test_substitution_default_when_secrets_missing():
    cfg = FakeConfig({"domain": "voipbin.example.com"})
    subs = _build_substitution_map(cfg, {}, {})
    template = BIN_SECRET_KEYS["RABBITMQ_ADDRESS"]["default"]
    rendered = _substitute(subs, template)
    # secrets.get("rabbitmq_password", "guest") fallback at k8s.py:73.
    assert rendered == (
        "amqp://guest:guest@rabbitmq.infrastructure.svc.cluster.local:5672"
    )
    assert "PLACEHOLDER_" not in rendered


def test_sops_override_full_dsn_still_wins():
    """pchero decision #4: sops may ship a full DSN that overrides the default."""
    cfg = FakeConfig({"domain": "voipbin.example.com"})
    override_dsn = "amqp://prod-user:prod-pass@prod-host:5672"
    subs = _build_substitution_map(
        cfg,
        {},
        {"RABBITMQ_ADDRESS": override_dsn, "rabbitmq_password": "hunter2"},
    )
    # The override branch at scripts/k8s.py:49-53 maps
    # PLACEHOLDER_RABBITMQ_ADDRESS -> the full operator-supplied DSN.
    assert subs["PLACEHOLDER_RABBITMQ_ADDRESS"] == override_dsn
    rendered = _substitute(subs, "PLACEHOLDER_RABBITMQ_ADDRESS")
    assert rendered == override_dsn
    assert "PLACEHOLDER_" not in rendered


def test_no_partial_token_collision():
    """Longest-first ordering preserves correctness for USER vs PASSWORD tokens.

    PLACEHOLDER_RABBITMQ_PASSWORD (29 chars) is processed before
    PLACEHOLDER_RABBITMQ_USER (25 chars). Neither is a prefix of the other,
    but the longest-first invariant matters if/when USER is later added
    to the DSN template.
    """
    user = "PLACEHOLDER_RABBITMQ_USER"
    password = "PLACEHOLDER_RABBITMQ_PASSWORD"
    assert not password.startswith(user)
    assert not user.startswith(password)
    assert len(password) > len(user)

    cfg = FakeConfig({"rabbitmq_user": "bunny"})
    subs = _build_substitution_map(cfg, {}, {"rabbitmq_password": "hunter2"})
    # A hypothetical template containing both tokens substitutes correctly.
    rendered = _substitute(
        subs,
        "amqp://PLACEHOLDER_RABBITMQ_USER:PLACEHOLDER_RABBITMQ_PASSWORD@host:5672",
    )
    assert rendered == "amqp://bunny:hunter2@host:5672"
