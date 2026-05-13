"""PR-Z Phase B tests: secret_schema integration for KAMAILIO_* keys."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.secret_schema import (
    all_allowed_secrets_yaml_keys,
    init_generated_keys,
    kamailio_tls_keys,
    KAMAILIO_TLS_KEYS,
)
from scripts.secretmgr import ALLOWED_SECRET_KEYS


KAMAILIO_KEYS_EXPECTED = {
    "KAMAILIO_CA_CERT_BASE64",
    "KAMAILIO_CA_KEY_BASE64",
    "KAMAILIO_CERT_SIP_BASE64",
    "KAMAILIO_PRIVKEY_SIP_BASE64",
    "KAMAILIO_CERT_REGISTRAR_BASE64",
    "KAMAILIO_PRIVKEY_REGISTRAR_BASE64",
}

SSL_KEYS_EXPECTED = {
    "SSL_CERT_API_BASE64",
    "SSL_PRIVKEY_API_BASE64",
    "SSL_CERT_HOOK_BASE64",
    "SSL_PRIVKEY_HOOK_BASE64",
}


class TestSecretSchemaIntegration:
    def test_all_six_kamailio_keys_present(self):
        all_allowed = all_allowed_secrets_yaml_keys()
        for k in KAMAILIO_KEYS_EXPECTED:
            assert k in all_allowed, f"missing {k} in allowed set"
            assert k in ALLOWED_SECRET_KEYS
            assert k in KAMAILIO_TLS_KEYS
            assert k in kamailio_tls_keys()

    def test_ssl_keys_not_regressed(self):
        gen = init_generated_keys()
        for k in SSL_KEYS_EXPECTED:
            assert k in gen
        # And remain in the global allow-list.
        for k in SSL_KEYS_EXPECTED:
            assert k in ALLOWED_SECRET_KEYS

    def test_total_count_is_35(self):
        # 24 operator-editable + 5 init-generated + 6 kamailio TLS = 35.
        assert len(ALLOWED_SECRET_KEYS) == 35
        assert len(all_allowed_secrets_yaml_keys()) == 35
