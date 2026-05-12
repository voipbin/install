# PR-D1 — Cloud SQL Postgres Provisioning

**Branch**: `NOJIRA-PR-D1-cloudsql-postgres`
**Status**: DESIGN — iteration D2 (revised after D1+D2 reviews)
**Author**: Hermes (CPO)
**Reviewer**: pchero (CEO + CTO)
**Phase**: v5
**Predecessor**: v4 close
**Successor**: PR-D2 (per-app users + DSN secrets)

---

## 1. Goal

Provision a Cloud SQL **Postgres** instance alongside the existing MySQL instance so that v5 PR-D2 can create per-app Postgres users (rag-manager today, future Postgres-backed services). Wire the instance into `reconcile_outputs` so its private IP lands in `config.yaml`. Match the existing MySQL provisioning shape exactly to minimise reviewer cognitive load.

## 2. Scope

In scope:

- One new `google_sql_database_instance.voipbin_postgres` (private IP, db-f1-micro, POSTGRES_17).
- One new `random_password.cloudsql_postgres_password` for the built-in admin user.
- One new `google_sql_user.voipbin_postgres` for the admin user (`postgres`).
- One new terraform output `cloudsql_postgres_private_ip`.
- One new `FIELD_MAP` entry mapping the output to `config.cloudsql_postgres_private_ip`.
- One new `reconcile_imports` registry entry for the instance plus parent_check pattern.
- Regression tests pinning every contract above.
- Synthetic-injection gate (v5 §2): each contract test fails when the production change is reverted.

Out of scope (deferred to PR-D2):

- Per-app users (`asterisk`, `voipbin`, `rag`).
- Per-app databases.
- DSN secret generation in Secret Manager.
- Database initialisation / schema migration.

Out of scope (deferred to PR-S or later):

- Postgres backup tuning beyond Terraform defaults.
- Multi-zone HA.
- Read replicas.
- Postgres-specific monitoring dashboards.

## 3. Non-goals

- Replacing MySQL with Postgres. Both engines coexist.
- Reducing dev-tier cost below the current MySQL baseline.

## 4. Background

GAP-44 from dogfood 11 surfaced the Kamailio entrypoint requirement for `KAMAILIO_AUTH_DB_URL`, `REDIS_CACHE_ADDRESS`, etc. Of those, the DB-DSN side requires a per-app Postgres user for rag-manager (currently CrashLooping on missing Postgres). PR-D1 lays the instance; PR-D2 adds the user + DSN secret; PR-R wires the DSN into Kamailio's env.

The existing MySQL provisioning at `terraform/cloudsql.tf` is the template:

```
random_password.cloudsql_password
google_sql_database_instance.voipbin (MYSQL_8_0, db-f1-micro, private IP, peering range)
google_sql_database.voipbin
google_sql_user.voipbin
```

And reconcile wiring at `scripts/terraform_reconcile.py`:

- Lines 504-533: instance + database + user import entries with `parent_check`.
- Line 705: FIELD_MAP entry for `cloudsql_mysql_private_ip → config.cloudsql_private_ip`.
- Output `cloudsql_mysql_private_ip` in `terraform/outputs.tf:59`.

PR-D1 mirrors this shape for Postgres, **omitting the per-app `google_sql_database` and `google_sql_user` resources** (those belong to PR-D2). The Postgres engine ships with a built-in `postgres` admin user, which `google_sql_user` can manage via a `password` attribute; we provision it now so PR-D2's per-app users have an admin to inherit privileges from.

## 5. Design

### 5.1 Terraform resources

Add to `terraform/cloudsql.tf`:

```hcl
# Postgres admin password (separate from MySQL random_password.cloudsql_password)
resource "random_password" "cloudsql_postgres_password" {
  length  = 24
  special = true
}

# Cloud SQL Postgres instance
resource "google_sql_database_instance" "voipbin_postgres" {
  name                = "${var.env}-postgres"
  database_version    = "POSTGRES_17"
  region              = var.region
  deletion_protection = true

  settings {
    tier              = "db-f1-micro"
    disk_size         = 10
    disk_type         = "PD_SSD"
    availability_type = var.gke_type == "regional" ? "REGIONAL" : "ZONAL"
    disk_autoresize   = true

    ip_configuration {
      ipv4_enabled       = false
      private_network    = google_compute_network.voipbin.id
      ssl_mode           = "ENCRYPTED_ONLY"
      allocated_ip_range = google_compute_global_address.cloudsql_peering.name
    }

    backup_configuration {
      enabled    = true
      start_time = "03:30"  # UTC. Offset from MySQL's 03:00 to avoid IO overlap.
      # Postgres does not support binary_log_enabled (MySQL-only field).
      # point_in_time_recovery_enabled is intentionally left at default false:
      # dev tier does not need WAL archive cost and the daily backups suffice.
      backup_retention_settings {
        retained_backups = 3  # Dev tier. Halves backup billing vs default 7.
        retention_unit   = "COUNT"
      }
    }

    maintenance_window {
      day  = 7
      hour = 5  # UTC. Offset from MySQL's 04 to avoid concurrent maintenance.
    }
  }

  depends_on = [
    time_sleep.api_propagation,
    google_service_networking_connection.voipbin,
  ]
}

# Built-in postgres admin user. PR-D2 will add per-app users alongside.
resource "google_sql_user" "voipbin_postgres" {
  name     = "postgres"
  instance = google_sql_database_instance.voipbin_postgres.name
  password = random_password.cloudsql_postgres_password.result
}
```

### 5.2 Terraform outputs

Append to `terraform/outputs.tf` near the existing Cloud SQL block (line 54-62):

```hcl
output "cloudsql_postgres_connection_name" {
  description = "Cloud SQL Postgres instance connection name (for Cloud SQL Proxy)"
  value       = google_sql_database_instance.voipbin_postgres.connection_name
}

output "cloudsql_postgres_private_ip" {
  description = "Cloud SQL Postgres private IP (consumed by reconcile_outputs FIELD_MAP)."
  value       = google_sql_database_instance.voipbin_postgres.private_ip_address
}
```

### 5.3 Reconcile imports

Append to `scripts/terraform_reconcile.py` Cloud SQL block (after line 533, before GKE):

```python
# Postgres instance (PR-D1). No database/user import here — admin user
# `postgres` is managed by google_sql_user.voipbin_postgres directly and
# does not require import. PR-D2 will add per-app database and user
# entries with parent_check pointing to the Postgres instance.
entries.append({
    "tf_address":   "google_sql_database_instance.voipbin_postgres",
    "description":  "Cloud SQL Postgres Instance",
    "gcloud_check": ["gcloud", "sql", "instances", "describe",
                     f"{config.get('env') or DEFAULT_ENV}-postgres",
                     f"--project={project}"],
    "import_id":    f"projects/{project}/instances/{config.get('env') or DEFAULT_ENV}-postgres",
})
entries.append({
    "tf_address":   "google_sql_user.voipbin_postgres",
    "description":  "Cloud SQL Postgres Admin User",
    "gcloud_check": ["gcloud", "sql", "users", "list",
                     f"--instance={config.get('env') or DEFAULT_ENV}-postgres",
                     "--filter=name=postgres", f"--project={project}"],
    "import_id":    f"{project}/{config.get('env') or DEFAULT_ENV}-postgres/postgres",
    # Same parent-check pattern as MySQL voipbin user.
    "parent_check": ["gcloud", "sql", "instances", "describe",
                     f"{config.get('env') or DEFAULT_ENV}-postgres",
                     f"--project={project}"],
})
```

### 5.4 Reconcile outputs FIELD_MAP

Append to `FIELD_MAP` (after line 713):

```python
TfOutputFieldMapping(
    tf_key="cloudsql_postgres_private_ip",
    cfg_key="cloudsql_postgres_private_ip",
    validator=_is_valid_ipv4_address,
),
```

### 5.5 Config schema

`config/schema.py` must allow `cloudsql_postgres_private_ip` as a top-level string (or whatever pattern the schema uses for `cloudsql_private_ip` today). PR-D1 design D2 must verify the schema location and add the property in the same shape.

### 5.6 Operator-facing changes

PR-D1 introduces TWO new terraform output keys:
- `cloudsql_postgres_connection_name`
- `cloudsql_postgres_private_ip`

These flow into `_write_extra_vars` (`ansible_runner.py`) as part of the `terraform_outputs` dict passed to Ansible. PR-D1 design D2 verified that no Ansible playbook iterates `terraform_outputs.keys()` as a closed set, and no CI script or runbook pins the output JSON shape. The wizard does not prompt for either field; reconcile_outputs auto-populates `cloudsql_postgres_private_ip` into `config.yaml`.

Schema gains one top-level string property `cloudsql_postgres_private_ip` (see §5.5 and D2 verification step).

PR-D2 will surface DSN secrets via a banner.

## 6. Trade-offs

| Decision | Alternatives considered | Why this one |
|---|---|---|
| POSTGRES_17 | POSTGRES_15 (older LTS-ish), POSTGRES_16 | 17 is GA, supported until 2029. Newer than necessary, but no migration cost in fresh install. |
| db-f1-micro | db-g1-small, db-custom-1-3840 | Matches MySQL. ~$10/mo. Dev profile. PR-D2/D3 can introduce a tfvar for tier. |
| Separate instance (not Postgres database on MySQL) | n/a — different engines | Cloud SQL one engine per instance. |
| Admin user `postgres` provisioned in PR-D1 | Defer to PR-D2 | PR-D2 will reuse the random_password and may grant privileges from postgres. Provisioning the admin in PR-D1 keeps PR-D2 focused on per-app users. |
| `binary_log_enabled` omitted | Set false explicitly | Postgres rejects the field. Omitting it is the documented path. |
| Backup window 03:30 / maintenance 05:00 | Same as MySQL (03:00 / 04:00) | Avoid IO contention on dev tier. |
| `parent_check` only on user, not on instance | parent_check on instance too | Instance has no parent within VoIPBin terraform (depends on network / peering, but those are siblings, not parents). |

## 7. Test surface

| Test class | What it pins |
|---|---|
| `TestPostgresInstanceResource` | resource exists with name pattern `${env}-postgres`, database_version POSTGRES_17, db-f1-micro, private IP, peering range, deletion_protection=true, `ssl_mode = "ENCRYPTED_ONLY"` (literal string assertion), `retained_backups = 3` |
| `TestPostgresAdminUser` | `google_sql_user.voipbin_postgres` exists, name=postgres, password ref to random_password.cloudsql_postgres_password |
| `TestPostgresOutputs` | both new outputs exist, reference instance attributes |
| `TestReconcileImports` | both registry entries exist, instance has NO `parent_check` key (positive assertion: key must be absent), user has `parent_check` pointing at instance |
| `TestReconcileFieldMap` | FIELD_MAP has `cloudsql_postgres_private_ip → config.cloudsql_postgres_private_ip` with `_is_valid_ipv4_address` validator |
| `TestSchemaAcceptsField` | config schema accepts the new property without `additionalProperties: false` rejection |
| `TestSyntheticInjection` | reverting any single PR-D1 file makes at least one test fail (verified before merge) |

## 8. v5 §2 abort criteria specific to PR-D1

- ≤ 4 review iterations.
- Design must not be re-opened twice (one design change is OK; two means PR-D1 scope is wrong, abort and re-plan).
- pytest + sensitive-audit clean before push.

## 9. Risks

| Risk | Mitigation |
|---|---|
| POSTGRES_17 unavailable in `us-central1` | Verify in R1 via `gcloud sql tiers list --project=voipbin-install-dev` or provider docs. Fallback POSTGRES_16. |
| Peering range exhaustion | The peering range is **`/20`** (`variables.tf` default `cloudsql_peering_prefix_length = 20`), 4096 addresses. Cloud SQL Service Networking carves `/24` per instance pair → ceiling ~16 instances. PR-D1 takes 1→2, no exhaustion risk. |
| `availability_type = "REGIONAL"` on `db-f1-micro` may not be supported | Shared-core tiers (`db-f1-micro`, `db-g1-small`) historically reject HA. MySQL inherits the same latent issue. R1 must `terraform plan` against a regional `gke_type` setting to confirm. Fallback: hardcode `ZONAL` for shared-core tiers and gate HA on a future tier knob. |
| `gke_type` flip post-create doubles cost | Changing `gke_type` from zonal to regional triggers Cloud SQL HA upgrade on both engines and roughly doubles cost. Documented; no PR-D1 mitigation. |
| Cost surprise | +$10/mo instance + ~$1.70 PD-SSD + reduced-retention backups. `retained_backups = 3` cuts backup billing >50% vs default 7. |
| Dogfood teardown blocked by `deletion_protection = true` | PR-D1 inherits MySQL's existing papercut. NOT widened by PR-D1 — same operator workflow (hand-flip the bool, apply, destroy). Carry-forward to PR-J for proper teardown UX (single `var.cloudsql_deletion_protection` covering both engines). Called out in PR commit message. |
| Config schema may use `additionalProperties: false` | R1 must verify `config/schema.py` and add the property if needed. If schema does not enforce strictness today, no change required. |
| `ssl_mode = "ENCRYPTED_ONLY"` Postgres acceptance | Confirmed valid for both engines in `hashicorp/google` provider v5.x and v6.x. Test must assert the literal string, NOT a non-existent `require_ssl` field (Postgres has none). |

## 10. Carry-forward for PR-D2 (do NOT do here)

- Per-app Postgres user `rag` (rag-manager consumer).
- Per-app database `rag`.
- DSN secret `rag_db_dsn` in Secret Manager. DSN format must include `sslmode=require` because Postgres `ssl_mode=ENCRYPTED_ONLY` forces TLS.
- Equivalent MySQL per-app users (`asterisk`, `voipbin` if not already).
- DSN format study from monorepo consumers.
- `parent_check` on each new per-app DB and user pointing at `${env}-postgres`.

## 10a. Carry-forward beyond PR-D2

- **`voipbin-install pause` verb** (PR-J or later): set `settings.activation_policy = "NEVER"` on both Cloud SQL instances. The dogfood-loop cost lever. Currently the only way to stop billing is destroy, blocked by `deletion_protection`.
- **Unified `var.cloudsql_deletion_protection`** (PR-J): replaces the two hardcoded `deletion_protection = true` so dev teardown stops requiring hand-edits.
- **Templatize MySQL's hardcoded `voipbin-mysql` reconcile entries** to `${env}-mysql` for consistency with PR-D1's Postgres entries. Trivial follow-up; PR-D1 intentionally does not retrofit MySQL.

## 11. Open questions resolved in D2

1. **Schema strictness**: R1 must run `grep -n additionalProperties config/schema.py` and add `cloudsql_postgres_private_ip` if `false`. If not strict, no change. Marked as a risk above.
2. **`random_password` exposure**: kept internal. Only `google_sql_user.voipbin_postgres` consumes `.result`. PR-D2 will reference via `random_password.cloudsql_postgres_password.result` directly. No Secret Manager entry in PR-D1.
3. **Teardown UX**: NOT widened by PR-D1 (MySQL already has the same papercut). Carry-forward to PR-J (`var.cloudsql_deletion_protection`). Called out in commit message.
4. **`cloudsql_postgres_connection_name` in FIELD_MAP**: kept OUT. Output exists but is not mapped to config until PR-D2 decides whether the DSN format needs it (private-IP DSN vs Cloud SQL Proxy connection name).

---

## Acceptance criteria

1. `terraform plan` on a clean state shows Postgres instance + user as ADDs only (no destroys).
2. `pytest` green; new PR-D1 tests included.
3. `check-plan-sensitive.sh` clean.
4. `reconcile_imports` registry contains the two new entries.
5. `reconcile_outputs` FIELD_MAP contains the new mapping.
6. Synthetic-injection verified for at least 3 of the new tests (revert ⇒ fail).
7. R1 + R2 + R3 review iterations all return APPROVED or follow-up only.
