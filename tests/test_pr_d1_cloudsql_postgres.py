"""PR-D1: Cloud SQL Postgres provisioning.

Adds a Postgres instance alongside the existing MySQL instance so PR-D2
can provision per-app users (rag, asterisk, voipbin) and DSN secrets,
and PR-R can wire those DSNs into Kamailio's runtime env.

Scope (strict):
- 1 google_sql_database_instance (POSTGRES_17, db-f1-micro, private IP)
- 1 random_password for admin
- 1 google_sql_user (built-in `postgres` admin)
- 2 terraform outputs (connection_name + private_ip)
- 1 reconcile_imports entry for the instance
- 1 reconcile_imports entry for the admin user (with parent_check)
- 1 FIELD_MAP entry (private IP -> config.cloudsql_postgres_private_ip)
- 1 config schema property

Out of scope (deferred to PR-D2): per-app databases, per-app users,
DSN secret generation. Tests below assert these are NOT introduced
by PR-D1.

Synthetic injection: each contract test fails when its corresponding
production change is reverted (verified before merge).
"""

import re
from pathlib import Path
from unittest.mock import MagicMock


REPO = Path(__file__).resolve().parent.parent
CLOUDSQL_TF = REPO / "terraform" / "cloudsql.tf"
OUTPUTS_TF = REPO / "terraform" / "outputs.tf"


# ---------------------------------------------------------------------------
# Terraform: Postgres instance + admin user
# ---------------------------------------------------------------------------

def _postgres_instance_block() -> str:
    content = CLOUDSQL_TF.read_text()
    m = re.search(
        r'resource\s+"google_sql_database_instance"\s+"voipbin_postgres"\s*\{',
        content,
    )
    assert m, "google_sql_database_instance.voipbin_postgres not declared"
    tail = content[m.end():]
    # Walk until the matching closing brace of the resource. The block
    # contains nested blocks; count depth.
    depth = 1
    i = 0
    while i < len(tail) and depth > 0:
        if tail[i] == "{":
            depth += 1
        elif tail[i] == "}":
            depth -= 1
        i += 1
    return tail[: i - 1]


class TestPostgresInstanceResource:
    def test_resource_declared(self):
        # _postgres_instance_block raises if absent.
        _postgres_instance_block()

    def test_name_uses_env_var(self):
        block = _postgres_instance_block()
        assert re.search(r'name\s*=\s*"\$\{var\.env\}-postgres"', block), (
            "Instance name must template on var.env (e.g. voipbin-postgres on "
            "the dev env) so multi-env installs do not collide."
        )

    def test_database_version_postgres_17(self):
        block = _postgres_instance_block()
        assert re.search(r'database_version\s*=\s*"POSTGRES_17"', block)

    def test_tier_f1_micro(self):
        block = _postgres_instance_block()
        assert re.search(r'tier\s*=\s*"db-f1-micro"', block)

    def test_private_ip_only(self):
        block = _postgres_instance_block()
        assert re.search(r'ipv4_enabled\s*=\s*false', block), (
            "Postgres instance must be private-IP-only; no public IPv4."
        )

    def test_uses_peering_range(self):
        block = _postgres_instance_block()
        assert "google_compute_global_address.cloudsql_peering.name" in block, (
            "Postgres instance must consume the existing VPC peering range "
            "so it shares the allocation with MySQL."
        )

    def test_ssl_mode_allows_unencrypted_for_production_parity(self):
        block = _postgres_instance_block()
        assert re.search(r'ssl_mode\s*=\s*"ALLOW_UNENCRYPTED_AND_ENCRYPTED"', block), (
            "PR-D2a loosened Postgres ssl_mode to ALLOW_UNENCRYPTED_AND_ENCRYPTED "
            "for production parity (see docs/security/cis-deviations.md). "
            "Postgres has no legacy require_ssl field; the test must assert the "
            "explicit string."
        )

    def test_deletion_protection_false(self):
        block = _postgres_instance_block()
        assert re.search(r'deletion_protection\s*=\s*false', block), (
            "deletion_protection must be false in install repo "
            "(destroy workflow enabled; lifecycle ignore_changes prevents drift)."
        )

    def test_lifecycle_ignore_deletion_protection(self):
        block = _postgres_instance_block()
        assert re.search(r'lifecycle\s*\{[^}]*ignore_changes\s*=\s*\[deletion_protection\]', block, re.DOTALL), (
            "Postgres instance must have lifecycle { ignore_changes = [deletion_protection] } "
            "so production operators can set true via GCP Console without terraform reverting it."
        )

    def test_retained_backups_three(self):
        block = _postgres_instance_block()
        assert re.search(r'retained_backups\s*=\s*3', block), (
            "Backup retention must be 3 on dev tier (PR-D1 D2 decision: "
            "halves backup billing vs default 7)."
        )

    def test_depends_on_api_and_peering(self):
        block = _postgres_instance_block()
        assert "time_sleep.api_propagation" in block
        assert "google_service_networking_connection.voipbin" in block


class TestPostgresAdminUser:
    def test_user_declared(self):
        content = CLOUDSQL_TF.read_text()
        assert re.search(
            r'resource\s+"google_sql_user"\s+"voipbin_postgres"', content,
        ), "google_sql_user.voipbin_postgres must be declared (the admin)."

    def test_user_name_postgres(self):
        content = CLOUDSQL_TF.read_text()
        m = re.search(
            r'resource\s+"google_sql_user"\s+"voipbin_postgres"\s*\{([^}]+)\}',
            content,
        )
        assert m, "voipbin_postgres user block missing"
        body = m.group(1)
        assert re.search(r'name\s*=\s*"postgres"', body), (
            "Admin user name must be the built-in `postgres`."
        )
        assert "random_password.cloudsql_postgres_password.result" in body, (
            "Password must reference random_password.cloudsql_postgres_password."
        )
        assert "google_sql_database_instance.voipbin_postgres.name" in body


class TestPostgresPasswordResource:
    def test_random_password_declared(self):
        content = CLOUDSQL_TF.read_text()
        assert re.search(
            r'resource\s+"random_password"\s+"cloudsql_postgres_password"', content,
        )

    def test_password_is_24_chars(self):
        content = CLOUDSQL_TF.read_text()
        m = re.search(
            r'resource\s+"random_password"\s+"cloudsql_postgres_password"\s*\{([^}]+)\}',
            content,
        )
        assert m
        assert re.search(r'length\s*=\s*24', m.group(1))


# ---------------------------------------------------------------------------
# Terraform outputs
# ---------------------------------------------------------------------------

class TestPostgresOutputs:
    def test_connection_name_output(self):
        content = OUTPUTS_TF.read_text()
        assert 'output "cloudsql_postgres_connection_name"' in content
        assert "google_sql_database_instance.voipbin_postgres.connection_name" in content

    def test_private_ip_output(self):
        content = OUTPUTS_TF.read_text()
        assert 'output "cloudsql_postgres_private_ip"' in content
        assert "google_sql_database_instance.voipbin_postgres.private_ip_address" in content


# ---------------------------------------------------------------------------
# Reconcile imports
# ---------------------------------------------------------------------------

def _build_registry_entries() -> list[dict]:
    """Helper to call build_registry with a minimal mocked config."""
    from scripts.terraform_reconcile import build_registry
    cfg = MagicMock()
    cfg.get.side_effect = lambda key, default=None: {
        "gcp_project_id": "test-proj",
        "env": "voipbin",
        "zone": "us-central1-a",
        "region": "us-central1",
        "gke_type": "zonal",
    }.get(key, default)
    return build_registry(cfg)


def _find_entry(addr: str, entries: list[dict]) -> dict | None:
    for e in entries:
        if e.get("tf_address") == addr:
            return e
    return None


class TestReconcileImports:
    def test_instance_entry_exists(self):
        entries = _build_registry_entries()
        e = _find_entry("google_sql_database_instance.voipbin_postgres", entries)
        assert e is not None, (
            "build_registry must yield an entry for "
            "google_sql_database_instance.voipbin_postgres."
        )

    def test_instance_entry_has_no_parent_check(self):
        """Positive assertion: instance has NO parent within VoIPBin terraform.
        Adding a parent_check would be incorrect because network/peering are
        siblings (depends_on), not parents."""
        entries = _build_registry_entries()
        e = _find_entry("google_sql_database_instance.voipbin_postgres", entries)
        assert e is not None
        assert "parent_check" not in e, (
            "Postgres instance must NOT carry a parent_check key. The "
            "network and peering it depends_on are siblings, not parents."
        )

    def test_instance_import_id_templated_on_env(self):
        entries = _build_registry_entries()
        e = _find_entry("google_sql_database_instance.voipbin_postgres", entries)
        assert "voipbin-postgres" in e["import_id"]
        assert "projects/test-proj/instances/voipbin-postgres" == e["import_id"]

    def test_user_entry_exists(self):
        entries = _build_registry_entries()
        e = _find_entry("google_sql_user.voipbin_postgres", entries)
        assert e is not None

    def test_user_has_parent_check_on_instance(self):
        entries = _build_registry_entries()
        e = _find_entry("google_sql_user.voipbin_postgres", entries)
        assert "parent_check" in e, (
            "Postgres admin user must declare parent_check pointing at the "
            "Postgres instance so reconcile defers import when the instance "
            "is not yet created (PR-L pattern)."
        )
        # parent_check should be a gcloud sql instances describe of the
        # postgres instance.
        pc = e["parent_check"]
        assert "instances" in pc and "describe" in pc and "voipbin-postgres" in pc


# ---------------------------------------------------------------------------
# Reconcile outputs FIELD_MAP
# ---------------------------------------------------------------------------

class TestReconcileFieldMap:
    def test_postgres_private_ip_mapping_present(self):
        from scripts.terraform_reconcile import FIELD_MAP
        keys = {m.tf_key: m for m in FIELD_MAP}
        assert "cloudsql_postgres_private_ip" in keys, (
            "FIELD_MAP must map cloudsql_postgres_private_ip (terraform "
            "output) -> config.cloudsql_postgres_private_ip."
        )
        m = keys["cloudsql_postgres_private_ip"]
        assert m.cfg_key == "cloudsql_postgres_private_ip"

    def test_postgres_private_ip_uses_ipv4_validator(self):
        from scripts.terraform_reconcile import (
            FIELD_MAP, _is_valid_ipv4_address,
        )
        m = next(x for x in FIELD_MAP
                 if x.tf_key == "cloudsql_postgres_private_ip")
        assert m.validator is _is_valid_ipv4_address, (
            "Validator must be _is_valid_ipv4_address so non-IPv4 strings "
            "(e.g. the cloudsql-private.invalid sentinel) cannot land in "
            "config.yaml."
        )


# ---------------------------------------------------------------------------
# Config schema
# ---------------------------------------------------------------------------

class TestSchemaAcceptsField:
    def test_property_declared(self):
        from config.schema import CONFIG_SCHEMA
        props = CONFIG_SCHEMA["properties"]
        assert "cloudsql_postgres_private_ip" in props, (
            "schema.CONFIG_SCHEMA.properties must declare "
            "cloudsql_postgres_private_ip; otherwise jsonschema's "
            "additionalProperties=false will reject the reconcile_outputs "
            "write."
        )
        assert props["cloudsql_postgres_private_ip"]["type"] == "string"

    def test_strict_schema_accepts_value(self):
        """Sanity-check the full schema validates a config that contains the
        new property — guards against accidentally adding a sibling property
        with the wrong shape."""
        import jsonschema
        from config.schema import CONFIG_SCHEMA
        sample = {
            "gcp_project_id": "test-proj",
            "region": "us-central1",
            "domain": "example.com",
            "cloudsql_postgres_private_ip": "10.0.0.5",
        }
        # Should not raise.
        jsonschema.validate(sample, CONFIG_SCHEMA)

    def test_strict_schema_rejects_unknown_property(self):
        """Negative case: confirms additionalProperties=false is still
        enforced and that the test_strict_schema_accepts_value above is
        not silently a no-op because the schema went permissive."""
        import jsonschema
        import pytest
        from config.schema import CONFIG_SCHEMA
        bogus = {
            "gcp_project_id": "test-proj",
            "region": "us-central1",
            "domain": "example.com",
            "cloudsql_postgres_private_ip": "10.0.0.5",
            "bogus_key_that_does_not_exist": "x",
        }
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate(bogus, CONFIG_SCHEMA)


# ---------------------------------------------------------------------------
# Scope guard: PR-D1 must NOT introduce PR-D2 territory.
# ---------------------------------------------------------------------------

class TestPRD2ScopeNotEntered:
    def test_no_per_app_postgres_databases(self):
        """PR-D2 will add google_sql_database.rag etc. PR-D1 must not."""
        content = CLOUDSQL_TF.read_text()
        for forbidden in (
            'resource "google_sql_database" "rag"',
            'resource "google_sql_database" "asterisk_postgres"',
            'resource "google_sql_database" "voipbin_postgres_app"',
        ):
            assert forbidden not in content, (
                f"PR-D1 must not declare {forbidden!r}. Per-app databases "
                "belong to PR-D2."
            )

    def test_no_per_app_postgres_users(self):
        content = CLOUDSQL_TF.read_text()
        # google_sql_user.voipbin_postgres is the admin only.
        for forbidden in (
            'resource "google_sql_user" "rag"',
            'resource "google_sql_user" "asterisk_postgres"',
        ):
            assert forbidden not in content, (
                f"PR-D1 must not declare {forbidden!r}. Per-app users "
                "belong to PR-D2."
            )

    def test_no_secret_manager_dsn_resources(self):
        """PR-D2 introduces google_secret_manager_secret.<app>_db_dsn.
        PR-D1 must not."""
        tf_files = list((REPO / "terraform").glob("*.tf"))
        for tf in tf_files:
            content = tf.read_text()
            assert "db_dsn" not in content, (
                f"{tf.name}: db_dsn secret naming belongs to PR-D2."
            )
