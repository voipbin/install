"""PR-D2a: Cloud SQL Terraform application resources tests.

42 cases across 18 classes mirroring design §4.7.
"""

from __future__ import annotations

import re
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from scripts import k8s as k8s_mod
from scripts.terraform_reconcile import FIELD_MAP, build_registry

REPO_ROOT = Path(__file__).resolve().parent.parent
CLOUDSQL_TF = REPO_ROOT / "terraform" / "cloudsql.tf"
OUTPUTS_TF = REPO_ROOT / "terraform" / "outputs.tf"
RECONCILE_PY = REPO_ROOT / "scripts" / "terraform_reconcile.py"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


def _extract_resource(text: str, kind: str, name: str) -> str:
    """Return the body (between outermost braces) of an HCL resource block."""
    pattern = rf'resource\s+"{re.escape(kind)}"\s+"{re.escape(name)}"\s*{{'
    m = re.search(pattern, text)
    if not m:
        raise AssertionError(f"resource {kind}.{name} not found in tf")
    start = m.end()
    depth = 1
    i = start
    while i < len(text) and depth > 0:
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
        i += 1
    return text[start:i - 1]


def _extract_output(text: str, name: str) -> str:
    pattern = rf'output\s+"{re.escape(name)}"\s*{{'
    m = re.search(pattern, text)
    if not m:
        raise AssertionError(f"output {name} not found in outputs.tf")
    start = m.end()
    depth = 1
    i = start
    while i < len(text) and depth > 0:
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
        i += 1
    return text[start:i - 1]


def _make_config(project: str = "proj-abc"):
    cfg = MagicMock()
    cfg.get.side_effect = lambda k, d="": {
        "gcp_project_id": project,
        "region": "us-central1",
        "zone": "us-central1-a",
        "env": "voipbin",
        "kamailio_count": 1,
        "rtpengine_count": 1,
        "cloudsql_private_ip": "10.42.0.5",
        "cloudsql_postgres_private_ip": "10.42.0.6",
    }.get(k, d)
    return cfg


# ---------------------------------------------------------------------------
# §4.7 test classes
# ---------------------------------------------------------------------------


class TestMySQLLegacyResourcesRemoved:
    """3 cases. Old voipbin db/user/password absent."""

    def test_legacy_voipbin_db_resource_absent(self):
        text = _read(CLOUDSQL_TF)
        assert not re.search(
            r'resource\s+"google_sql_database"\s+"voipbin"\s*{', text
        ), "legacy google_sql_database.voipbin must be deleted by PR-D2a"

    def test_legacy_voipbin_user_resource_absent(self):
        text = _read(CLOUDSQL_TF)
        assert not re.search(
            r'resource\s+"google_sql_user"\s+"voipbin"\s*{', text
        ), "legacy google_sql_user.voipbin must be deleted by PR-D2a"

    def test_legacy_cloudsql_password_random_password_absent(self):
        text = _read(CLOUDSQL_TF)
        assert not re.search(
            r'resource\s+"random_password"\s+"cloudsql_password"\s*{', text
        ), (
            "legacy random_password.cloudsql_password (MySQL admin) must be "
            "deleted; PR-D2a stops managing MySQL admin via terraform"
        )


class TestMySQLApplicationDatabases:
    """4 cases. 2 dbs (bin_manager, asterisk) × charset + collation."""

    @pytest.mark.parametrize(
        "resource_name,db_name",
        [
            ("voipbin_mysql_bin_manager", "bin_manager"),
            ("voipbin_mysql_asterisk", "asterisk"),
        ],
    )
    def test_charset_utf8mb3(self, resource_name, db_name):
        text = _read(CLOUDSQL_TF)
        body = _extract_resource(text, "google_sql_database", resource_name)
        assert re.search(rf'name\s*=\s*"{db_name}"', body)
        assert re.search(r'charset\s*=\s*"utf8mb3"', body), (
            f"{resource_name}: charset must be utf8mb3 (production parity, "
            "utf8mb4 migration tracked in docs/follow-ups.md)"
        )

    @pytest.mark.parametrize(
        "resource_name",
        ["voipbin_mysql_bin_manager", "voipbin_mysql_asterisk"],
    )
    def test_collation_utf8mb3_general_ci(self, resource_name):
        text = _read(CLOUDSQL_TF)
        body = _extract_resource(text, "google_sql_database", resource_name)
        assert re.search(r'collation\s*=\s*"utf8mb3_general_ci"', body)


class TestMySQLApplicationUsers:
    """4 cases. 4 users with correct hyphenated names."""

    @pytest.mark.parametrize(
        "resource_name,user_name",
        [
            ("voipbin_mysql_bin_manager", "bin-manager"),
            ("voipbin_mysql_asterisk", "asterisk"),
            ("voipbin_mysql_call_manager", "call-manager"),
            ("voipbin_mysql_kamailioro", "kamailioro"),
        ],
    )
    def test_user_name_literal(self, resource_name, user_name):
        text = _read(CLOUDSQL_TF)
        body = _extract_resource(text, "google_sql_user", resource_name)
        assert re.search(rf'name\s*=\s*"{user_name}"', body), (
            f"user resource {resource_name} must have name={user_name!r}; "
            "guard against accidental underscore typos"
        )


class TestMySQLSslMode:
    """1 case."""

    def test_ssl_mode_allow_unencrypted_and_encrypted(self):
        text = _read(CLOUDSQL_TF)
        body = _extract_resource(text, "google_sql_database_instance", "voipbin")
        assert re.search(
            r'ssl_mode\s*=\s*"ALLOW_UNENCRYPTED_AND_ENCRYPTED"', body
        ), (
            "PR-D2a: MySQL ssl_mode loosened to ALLOW_UNENCRYPTED_AND_ENCRYPTED "
            "for production parity (see docs/security/cis-deviations.md)"
        )


class TestMySQLDeletionProtection:
    """2 cases — PR-AJ: deletion_protection=false + lifecycle ignore."""

    def test_deletion_protection_false(self):
        text = _read(CLOUDSQL_TF)
        body = _extract_resource(text, "google_sql_database_instance", "voipbin")
        assert re.search(r'deletion_protection\s*=\s*false', body), (
            "MySQL instance must have deletion_protection=false so that "
            "voipbin-install destroy can remove it without manual intervention."
        )

    def test_lifecycle_ignore_deletion_protection(self):
        text = _read(CLOUDSQL_TF)
        body = _extract_resource(text, "google_sql_database_instance", "voipbin")
        assert re.search(
            r'lifecycle\s*\{[^}]*ignore_changes\s*=\s*\[deletion_protection\]',
            body,
            re.DOTALL,
        ), (
            "MySQL instance must have lifecycle { ignore_changes = [deletion_protection] } "
            "so production operators can set true via GCP Console without terraform reverting it."
        )


class TestKamailioroHostPin:
    """2 cases."""

    def test_kamailioro_user_resource_exists(self):
        text = _read(CLOUDSQL_TF)
        _ = _extract_resource(text, "google_sql_user", "voipbin_mysql_kamailioro")

    def test_kamailioro_host_pin_literal(self):
        text = _read(CLOUDSQL_TF)
        body = _extract_resource(text, "google_sql_user", "voipbin_mysql_kamailioro")
        assert re.search(r'host\s*=\s*"10\.0\.0\.0/255\.0\.0\.0"', body), (
            "kamailioro host pinning must match production exactly"
        )


class TestPostgresApplicationDb:
    """2 cases."""

    def test_postgres_bin_manager_db_with_utf8(self):
        text = _read(CLOUDSQL_TF)
        body = _extract_resource(
            text, "google_sql_database", "voipbin_postgres_bin_manager"
        )
        assert re.search(r'name\s*=\s*"bin_manager"', body)
        assert re.search(r'charset\s*=\s*"UTF8"', body)
        assert re.search(r'collation\s*=\s*"en_US\.UTF8"', body)

    def test_postgres_bin_manager_user_with_hyphen(self):
        text = _read(CLOUDSQL_TF)
        body = _extract_resource(
            text, "google_sql_user", "voipbin_postgres_bin_manager"
        )
        assert re.search(r'name\s*=\s*"bin-manager"', body)


class TestPostgresAdminPreserved:
    """1 case. PR-D1 admin user untouched."""

    def test_postgres_admin_preserved(self):
        text = _read(CLOUDSQL_TF)
        body = _extract_resource(text, "google_sql_user", "voipbin_postgres")
        assert re.search(r'name\s*=\s*"postgres"', body)


class TestPostgresSslMode:
    """1 case."""

    def test_ssl_mode_allow_unencrypted_and_encrypted(self):
        text = _read(CLOUDSQL_TF)
        body = _extract_resource(
            text, "google_sql_database_instance", "voipbin_postgres"
        )
        assert re.search(
            r'ssl_mode\s*=\s*"ALLOW_UNENCRYPTED_AND_ENCRYPTED"', body
        )


class TestRandomPasswordsAlphabet:
    """5 cases. 5 password resources, length 24, special=true, exact alphabet."""

    EXPECTED = [
        "mysql_bin_manager",
        "mysql_asterisk",
        "mysql_call_manager",
        "mysql_kamailioro",
        "postgres_bin_manager",
    ]

    @pytest.mark.parametrize("name", EXPECTED)
    def test_random_password_shape(self, name):
        text = _read(CLOUDSQL_TF)
        body = _extract_resource(text, "random_password", name)
        assert re.search(r"length\s*=\s*24", body), f"{name}: length must be 24"
        assert re.search(r"special\s*=\s*true", body), f"{name}: special must be true"
        m = re.search(r'override_special\s*=\s*"([^"]*)"', body)
        assert m, f"{name}: override_special must be set"
        # Exact RFC 3986 userinfo-safe subset (locked by roadmap v3 §66).
        assert m.group(1) == "!*+-._~", (
            f"{name}: override_special must be exactly '!*+-._~' "
            f"(RFC 3986 unreserved + URI-safe sub-delims subset); got {m.group(1)!r}"
        )


class TestSensitiveOutputs:
    """6 cases. 6 outputs declared sensitive=true with correct value reference."""

    EXPECTED = {
        "cloudsql_mysql_password_bin_manager":
            "random_password.mysql_bin_manager.result",
        "cloudsql_mysql_password_asterisk":
            "random_password.mysql_asterisk.result",
        "cloudsql_mysql_password_call_manager":
            "random_password.mysql_call_manager.result",
        "cloudsql_mysql_password_kamailioro":
            "random_password.mysql_kamailioro.result",
        "cloudsql_postgres_password_bin_manager":
            "random_password.postgres_bin_manager.result",
        "cloudsql_postgres_password_homer":
            "random_password.postgres_homer.result",
    }

    @pytest.mark.parametrize("output_name,expected_ref", list(EXPECTED.items()))
    def test_output_sensitive_and_correct_value(self, output_name, expected_ref):
        text = _read(OUTPUTS_TF)
        body = _extract_output(text, output_name)
        assert re.search(r"sensitive\s*=\s*true", body), (
            f"{output_name}: must be declared sensitive=true"
        )
        assert expected_ref in body, (
            f"{output_name}: value must reference {expected_ref}"
        )


class TestReconcileRegistryEntries:
    """7 cases. Exactly 7 new entries with expected import_id."""

    PROJECT = "proj-abc"

    EXPECTED = [
        ("google_sql_database.voipbin_mysql_bin_manager",
         f"projects/{PROJECT}/instances/voipbin-mysql/databases/bin_manager"),
        ("google_sql_database.voipbin_mysql_asterisk",
         f"projects/{PROJECT}/instances/voipbin-mysql/databases/asterisk"),
        ("google_sql_user.voipbin_mysql_bin_manager",
         f"{PROJECT}/voipbin-mysql/bin-manager"),
        ("google_sql_user.voipbin_mysql_asterisk",
         f"{PROJECT}/voipbin-mysql/asterisk"),
        ("google_sql_user.voipbin_mysql_call_manager",
         f"{PROJECT}/voipbin-mysql/call-manager"),
        ("google_sql_database.voipbin_postgres_bin_manager",
         f"projects/{PROJECT}/instances/voipbin-postgres/databases/bin_manager"),
        ("google_sql_user.voipbin_postgres_bin_manager",
         f"{PROJECT}/voipbin-postgres/bin-manager"),
    ]

    @pytest.mark.parametrize("tf_address,import_id", EXPECTED)
    def test_entry_present_with_correct_import_id(self, tf_address, import_id):
        entries = build_registry(_make_config(self.PROJECT))
        by_addr = {e["tf_address"]: e for e in entries}
        assert tf_address in by_addr, (
            f"reconcile registry must include {tf_address}"
        )
        assert by_addr[tf_address]["import_id"] == import_id


class TestKamailioroNotInRegistry:
    """1 case. Slash-in-host collides with provider import id parser."""

    def test_kamailioro_not_in_registry(self):
        entries = build_registry(_make_config())
        addrs = {e["tf_address"] for e in entries}
        assert "google_sql_user.voipbin_mysql_kamailioro" not in addrs, (
            "kamailioro user must NOT be in reconcile registry — its host "
            "`10.0.0.0/255.0.0.0` contains slashes that collide with the "
            "provider's import id parser"
        )


class TestNoFieldMapPasswordEntries:
    """1 case. Passwords must NEVER be persisted to config.yaml via FIELD_MAP."""

    def test_field_map_has_no_password_entries(self):
        # FIELD_MAP entries write into config.yaml on disk. Writing passwords
        # to config.yaml defeats the sensitive=true posture. PR-D2b consumes
        # the password tf_outputs directly via the substitution map.
        for mapping in FIELD_MAP:
            assert "password" not in mapping.cfg_key.lower(), (
                f"FIELD_MAP must not contain a password entry; found "
                f"{mapping.cfg_key!r} (would persist plaintext to config.yaml)"
            )
            assert "password" not in mapping.tf_key.lower(), (
                f"FIELD_MAP must not contain a password tf_key; found "
                f"{mapping.tf_key!r}"
            )


class TestLegacyAliasPreserved:
    """1 case. Option-A regression guard: scripts/k8s.py keeps the legacy
    PLACEHOLDER_CLOUDSQL_PRIVATE_IP[_CIDR] keys so existing k8s manifests
    continue rendering unchanged through the D2a → D2b interim."""

    def test_legacy_placeholder_keys_emitted(self):
        cfg = MagicMock()
        cfg.get.side_effect = lambda k, d="": {
            "domain": "example.com",
            "gcp_project_id": "proj-abc",
            "region": "us-central1",
            "cloudsql_private_ip": "10.42.0.5",
            "cloudsql_postgres_private_ip": "10.42.0.6",
            "rabbitmq_user": "guest",
        }.get(k, d)
        subs = k8s_mod._build_substitution_map(cfg, {}, {})
        assert subs["PLACEHOLDER_CLOUDSQL_PRIVATE_IP"] == "10.42.0.5", (
            "PR-D2a must NOT remove the legacy MySQL-IP alias; existing "
            "manifests (k8s/backend/secret.yaml, k8s/voip/secret.yaml, "
            "k8s/network-policies/*) depend on it. Rename is D2b's scope."
        )
        assert subs["PLACEHOLDER_CLOUDSQL_PRIVATE_IP_CIDR"] == "10.42.0.5/32"


class TestPreflightLegacyVoipbinForceTrue:
    """1 case."""

    def test_force_true_short_circuits(self):
        from scripts.preflight import check_legacy_voipbin_destroy_safety
        cfg = MagicMock()
        cfg.get.side_effect = lambda k, d="": "p" if k == "gcp_project_id" else d
        # No mock for run_cmd: if force=True does NOT short-circuit, the real
        # gcloud will be invoked (or raise). force=True must return None.
        with patch("scripts.preflight.run_cmd") as mock_run:
            check_legacy_voipbin_destroy_safety(cfg, force=True)
            mock_run.assert_not_called()


class TestPreflightLegacyVoipbinForceFalseRaises:
    """1 case."""

    def test_force_false_raises_when_db_present(self):
        from scripts.preflight import PreflightError, check_legacy_voipbin_destroy_safety
        cfg = MagicMock()
        cfg.get.side_effect = lambda k, d="": "p" if k == "gcp_project_id" else d
        proc = MagicMock()
        proc.returncode = 0  # legacy db exists
        with patch("scripts.preflight.run_cmd", return_value=proc) as mock_run:
            with pytest.raises(PreflightError, match="force-destroy-legacy-voipbin"):
                check_legacy_voipbin_destroy_safety(cfg, force=False)
            # Regression guard: run_cmd must be called with `capture=` keyword
            # (NOT `capture_output=`, which would TypeError against
            # scripts/utils.py:run_cmd signature). PR-D2 v1 root-cause #2.
            assert mock_run.called, "run_cmd must be invoked to probe gcloud"
            _, kwargs = mock_run.call_args
            assert "capture" in kwargs and "capture_output" not in kwargs, (
                "run_cmd must be called with `capture=` keyword, matching "
                "scripts/utils.py:run_cmd signature. PR-D2 v1 root-cause #2."
            )


class TestPreflightLegacyVoipbinForceFalseSilent:
    """1 case."""

    def test_force_false_silent_when_db_absent(self):
        from scripts.preflight import check_legacy_voipbin_destroy_safety
        cfg = MagicMock()
        cfg.get.side_effect = lambda k, d="": "p" if k == "gcp_project_id" else d
        proc = MagicMock()
        proc.returncode = 1  # legacy db absent
        with patch("scripts.preflight.run_cmd", return_value=proc):
            check_legacy_voipbin_destroy_safety(cfg, force=False)


class TestCliForceFlagWired:
    """1 case. CliRunner-driven end-to-end: invoke `voipbin-install apply
    --force-destroy-legacy-voipbin --dry-run` and assert the flag reached
    `cmd_apply` (downstream pipeline reads it via `getattr`)."""

    def test_flag_reaches_cmd_apply(self):
        from click.testing import CliRunner
        from scripts import cli as cli_mod

        captured = {}

        def fake_cmd_apply(**kwargs):
            captured.update(kwargs)

        runner = CliRunner()
        with patch.object(cli_mod, "cmd_apply", side_effect=fake_cmd_apply):
            result = runner.invoke(
                cli_mod.cli,
                ["apply", "--force-destroy-legacy-voipbin", "--dry-run"],
            )
        # The Click invocation should succeed (no SystemExit from cmd_apply
        # since we patched it). Click returns exit_code=0 on clean.
        assert result.exit_code == 0, (
            f"Click invocation failed: {result.output}\n{result.exception}"
        )
        assert captured.get("force_destroy_legacy_voipbin") is True, (
            f"--force-destroy-legacy-voipbin did not reach cmd_apply; "
            f"got kwargs={captured}"
        )
