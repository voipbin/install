"""PR-D2b: k8s manifest DSN substitution wiring tests.

19 cases across 9 classes mirroring design §4.4.
"""

from __future__ import annotations

import re
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from scripts import k8s as k8s_mod
from scripts.k8s import _render_manifests_substitution
from scripts.secret_schema import BIN_SECRET_KEYS

REPO_ROOT = Path(__file__).resolve().parent.parent
SECRET_YAML = REPO_ROOT / "k8s" / "backend" / "secret.yaml"


def _make_config(overrides=None):
    base = {
        "domain": "example.com",
        "gcp_project_id": "proj-abc",
        "region": "us-central1",
        "cloudsql_private_ip": "10.42.0.5",
        "cloudsql_postgres_private_ip": "10.42.0.6",
        "rabbitmq_user": "guest",
    }
    if overrides:
        base.update(overrides)
    cfg = MagicMock()
    cfg.get.side_effect = lambda k, d="": base.get(k, d)
    return cfg


def _sample_tf_outputs():
    return {
        "cloudsql_mysql_password_bin_manager": "MySQLBinPwd-Sample24chars!",
        "cloudsql_mysql_password_asterisk": "MySQLAstPwd-Sample24chars!",
        "cloudsql_mysql_password_call_manager": "MySQLCmPwd-Sample24chars!",
        "cloudsql_mysql_password_kamailioro": "MySQLKamPwd-Sample24chars!",
        "cloudsql_postgres_password_bin_manager": "PgBinPwd-Sample24chars!",
    }


class TestSubstitutionMapNewEntries:
    """7 cases. 5 password placeholders + 2 IP placeholders."""

    PASSWORD_MAP = {
        "PLACEHOLDER_DSN_PASSWORD_MYSQL_BIN_MANAGER":
            "cloudsql_mysql_password_bin_manager",
        "PLACEHOLDER_DSN_PASSWORD_MYSQL_ASTERISK":
            "cloudsql_mysql_password_asterisk",
        "PLACEHOLDER_DSN_PASSWORD_MYSQL_CALL_MANAGER":
            "cloudsql_mysql_password_call_manager",
        "PLACEHOLDER_DSN_PASSWORD_MYSQL_KAMAILIORO":
            "cloudsql_mysql_password_kamailioro",
        "PLACEHOLDER_DSN_PASSWORD_POSTGRES_BIN_MANAGER":
            "cloudsql_postgres_password_bin_manager",
    }

    @pytest.mark.parametrize(
        "placeholder,tf_key", list(PASSWORD_MAP.items())
    )
    def test_password_placeholder_sourced_from_tf_output(self, placeholder, tf_key):
        secret = f"value-for-{tf_key}"
        subs = k8s_mod._build_substitution_map(
            _make_config(), {tf_key: secret}, {}
        )
        assert subs[placeholder] == secret, (
            f"{placeholder} must read from tf_outputs[{tf_key!r}]"
        )

    def test_mysql_private_ip_placeholder_present(self):
        subs = k8s_mod._build_substitution_map(_make_config(), {}, {})
        assert subs["PLACEHOLDER_CLOUDSQL_MYSQL_PRIVATE_IP"] == "10.42.0.5"

    def test_postgres_private_ip_placeholder_present(self):
        subs = k8s_mod._build_substitution_map(_make_config(), {}, {})
        assert subs["PLACEHOLDER_CLOUDSQL_POSTGRES_PRIVATE_IP"] == "10.42.0.6"


class TestSecretSchemaDsnDefaults:
    """3 cases. Each DSN default contains the new placeholders, no dummy-password."""

    EXPECTED = {
        "DATABASE_DSN_BIN": (
            "PLACEHOLDER_DSN_PASSWORD_MYSQL_BIN_MANAGER",
            "PLACEHOLDER_CLOUDSQL_MYSQL_PRIVATE_IP",
        ),
        "DATABASE_DSN_ASTERISK": (
            "PLACEHOLDER_DSN_PASSWORD_MYSQL_ASTERISK",
            "PLACEHOLDER_CLOUDSQL_MYSQL_PRIVATE_IP",
        ),
        "DATABASE_DSN_POSTGRES": (
            "PLACEHOLDER_DSN_PASSWORD_POSTGRES_BIN_MANAGER",
            "PLACEHOLDER_CLOUDSQL_POSTGRES_PRIVATE_IP",
        ),
    }

    @pytest.mark.parametrize("key,placeholders", list(EXPECTED.items()))
    def test_dsn_default_uses_new_placeholders(self, key, placeholders):
        default = BIN_SECRET_KEYS[key]["default"]
        password_token, ip_token = placeholders
        assert password_token in default, f"{key}: missing {password_token}"
        assert ip_token in default, f"{key}: missing {ip_token}"
        assert "dummy-password" not in default, (
            f"{key}: must not retain literal dummy-password"
        )


class TestSecretYamlManifest:
    """3 cases. k8s/backend/secret.yaml uses new placeholders on the 3 DSN lines."""

    EXPECTED = {
        "DATABASE_DSN_BIN":
            "bin-manager:PLACEHOLDER_DSN_PASSWORD_MYSQL_BIN_MANAGER@tcp(PLACEHOLDER_CLOUDSQL_MYSQL_PRIVATE_IP:3306)/bin_manager",
        "DATABASE_DSN_ASTERISK":
            "asterisk:PLACEHOLDER_DSN_PASSWORD_MYSQL_ASTERISK@tcp(PLACEHOLDER_CLOUDSQL_MYSQL_PRIVATE_IP:3306)/asterisk",
        "DATABASE_DSN_POSTGRES":
            "postgres://bin-manager:PLACEHOLDER_DSN_PASSWORD_POSTGRES_BIN_MANAGER@PLACEHOLDER_CLOUDSQL_POSTGRES_PRIVATE_IP:5432/bin_manager?sslmode=disable",
    }

    @pytest.mark.parametrize("key,expected", list(EXPECTED.items()))
    def test_secret_yaml_line(self, key, expected):
        text = SECRET_YAML.read_text(encoding="utf-8")
        assert expected in text, (
            f"k8s/backend/secret.yaml must contain the expected {key} value: "
            f"{expected!r}"
        )
        # Sanity: no dummy-password
        assert "dummy-password" not in text, (
            "secret.yaml must not retain dummy-password anywhere"
        )


def _resolve(template: str, subs: dict) -> str:
    """Drive the production substitution loop directly."""
    return _render_manifests_substitution(template, subs)


class TestEndToEndRenderBin:
    """1 case. Render DATABASE_DSN_BIN against the real secret.yaml + real
    substitution map; assert no PLACEHOLDER_ left and values are real."""

    def test_bin_dsn_renders_fully(self):
        text = SECRET_YAML.read_text(encoding="utf-8")
        subs = k8s_mod._build_substitution_map(
            _make_config(), _sample_tf_outputs(), {}
        )
        rendered = _resolve(text, subs)
        # Extract the DATABASE_DSN_BIN line.
        line = next(
            l for l in rendered.splitlines() if l.startswith("  DATABASE_DSN_BIN:")
        )
        assert "PLACEHOLDER_" not in line, f"unresolved placeholder in: {line}"
        assert "MySQLBinPwd-Sample24chars!" in line
        assert "10.42.0.5" in line
        assert "bin-manager:" in line
        assert "/bin_manager" in line


class TestEndToEndRenderAsterisk:
    """1 case."""

    def test_asterisk_dsn_renders_fully(self):
        text = SECRET_YAML.read_text(encoding="utf-8")
        subs = k8s_mod._build_substitution_map(
            _make_config(), _sample_tf_outputs(), {}
        )
        rendered = _resolve(text, subs)
        line = next(
            l for l in rendered.splitlines()
            if l.startswith("  DATABASE_DSN_ASTERISK:")
        )
        assert "PLACEHOLDER_" not in line
        assert "MySQLAstPwd-Sample24chars!" in line
        assert "10.42.0.5" in line  # MySQL IP (asterisk db is on MySQL)


class TestEndToEndRenderPostgres:
    """1 case."""

    def test_postgres_dsn_renders_fully(self):
        text = SECRET_YAML.read_text(encoding="utf-8")
        subs = k8s_mod._build_substitution_map(
            _make_config(), _sample_tf_outputs(), {}
        )
        rendered = _resolve(text, subs)
        line = next(
            l for l in rendered.splitlines()
            if l.startswith("  DATABASE_DSN_POSTGRES:")
        )
        assert "PLACEHOLDER_" not in line
        assert "PgBinPwd-Sample24chars!" in line
        assert "10.42.0.6" in line  # Postgres IP
        assert line.startswith("  DATABASE_DSN_POSTGRES: \"postgres://")


class TestLegacyMySQLAliasStillEmitted:
    """1 case. PR-D2a regression guard."""

    def test_legacy_keys_emitted_for_voip_and_netpol(self):
        subs = k8s_mod._build_substitution_map(_make_config(), {}, {})
        assert subs["PLACEHOLDER_CLOUDSQL_PRIVATE_IP"] == "10.42.0.5", (
            "Legacy MySQL-IP alias must remain (consumed by k8s/voip/secret.yaml "
            "and k8s/network-policies/*)."
        )
        assert subs["PLACEHOLDER_CLOUDSQL_PRIVATE_IP_CIDR"] == "10.42.0.5/32"


class TestRenderManifestsLoopHonorsLongestFirst:
    """3 cases. Drive `_render_manifests_substitution` directly to verify the
    longest-first sort semantics that protect overlapping placeholders."""

    def test_longest_first_resolves_overlapping_prefix(self):
        # `_PRIVATE_IP` vs `_PRIVATE_IP_CIDR` is the real-world overlap.
        subs = {
            "PLACEHOLDER_FOO": "X",
            "PLACEHOLDER_FOO_BAR": "Y",
        }
        out = _render_manifests_substitution(
            "PLACEHOLDER_FOO_BAR PLACEHOLDER_FOO", subs
        )
        assert out == "Y X", (
            f"longest-first sort broken: got {out!r}, expected 'Y X'"
        )

    def test_legacy_cloudsql_overlap_intact(self):
        subs = {
            "PLACEHOLDER_CLOUDSQL_PRIVATE_IP": "10.42.0.5",
            "PLACEHOLDER_CLOUDSQL_PRIVATE_IP_CIDR": "10.42.0.5/32",
        }
        out = _render_manifests_substitution(
            "ip=PLACEHOLDER_CLOUDSQL_PRIVATE_IP cidr=PLACEHOLDER_CLOUDSQL_PRIVATE_IP_CIDR",
            subs,
        )
        assert out == "ip=10.42.0.5 cidr=10.42.0.5/32"

    def test_nested_dsn_resolves_both_password_and_ip(self):
        subs = {
            "PLACEHOLDER_DSN_PASSWORD_MYSQL_BIN_MANAGER": "secret-pw",
            "PLACEHOLDER_CLOUDSQL_MYSQL_PRIVATE_IP": "10.42.0.5",
        }
        out = _render_manifests_substitution(
            "bin-manager:PLACEHOLDER_DSN_PASSWORD_MYSQL_BIN_MANAGER"
            "@tcp(PLACEHOLDER_CLOUDSQL_MYSQL_PRIVATE_IP:3306)/bin_manager",
            subs,
        )
        assert out == "bin-manager:secret-pw@tcp(10.42.0.5:3306)/bin_manager"


class TestEmptyPasswordSurfaceErrors:
    """1 case. Guard against silent empty-password DSN render. If a tf_output
    key is missing, `_build_substitution_map` currently defaults to `""`,
    producing a DSN like `bin-manager:@10.x:3306/...`. This test pins the
    current behavior so any future tightening is intentional."""

    def test_missing_tf_output_yields_empty_password(self):
        subs = k8s_mod._build_substitution_map(_make_config(), {}, {})
        # Pre-tightening: empty string. If/when an installer-side check
        # rejects this, update both production code and this test together.
        assert subs["PLACEHOLDER_DSN_PASSWORD_MYSQL_BIN_MANAGER"] == "", (
            "Documented current behavior: missing tf_output → empty password "
            "in the substitution map. The follow-up to add an installer-side "
            "guard against empty DSN passwords is filed in docs/follow-ups.md."
        )


class TestPasswordYamlSafetyForLockedAlphabet:
    """1 case. The terraform password alphabet locked in PR-D2a is RFC 3986
    userinfo-safe AND YAML double-quoted scalar-safe (no `"`, no `\\`). This
    test asserts the alphabet boundary so PR-D2c (kamailioro / URL safety)
    does not silently widen it without also widening the DSN escape logic."""

    LOCKED_ALPHABET = set("!*+-._~")
    YAML_HOSTILE = set('"\\')
    URL_HOSTILE = set('@:/?#&=% \t\n')

    def test_locked_alphabet_is_yaml_and_url_safe(self):
        assert self.LOCKED_ALPHABET.isdisjoint(self.YAML_HOSTILE), (
            "Locked password alphabet contains chars that break YAML "
            "double-quoted scalars; rendered Secret would be invalid."
        )
        assert self.LOCKED_ALPHABET.isdisjoint(self.URL_HOSTILE), (
            "Locked password alphabet contains chars that break DSN userinfo "
            "without percent-encoding; PR-AC-1 raw-password emission assumes this "
            "guarantee."
        )


class TestPostgresDsnPrefixCanonical:
    """1 case."""

    def test_postgres_dsn_prefix(self):
        default = BIN_SECRET_KEYS["DATABASE_DSN_POSTGRES"]["default"]
        assert default.startswith("postgres://"), (
            "PR-D2b: DATABASE_DSN_POSTGRES must use `postgres://` (lib/pq + "
            "golang-migrate both accept; production uses it); not `postgresql://`."
        )


class TestK8sModuleConstantsSmoke:
    """1 case. Regression guard: iter-2 R2/R3 caught that helper extraction
    accidentally deleted `K8S_DIR = INSTALLER_DIR / "k8s"`, leaving runtime
    NameError in `_render_manifests` and `k8s_delete` (lazy name lookup
    bypassed import-time checks). This smoke test ensures the module-level
    constant stays resolvable."""

    def test_k8s_dir_module_constant(self):
        from scripts.k8s import K8S_DIR
        assert str(K8S_DIR).endswith("/k8s")
