# PR-D2a — Cloud SQL Terraform application resources (no manifest wiring)

Status: Design v1 (draft)
Author: Hermes (CPO)
Date: 2026-05-12
Branch: NOJIRA-PR-D2a-cloudsql-terraform-resources
Builds on: PR-D1 (`ecd2072`)
Parent roadmap: `~/agent-hermes/notes/2026-05-12-pr-d2-split-roadmap.md` (v3, APPROVED 2026-05-12)
Supersedes the terraform/reconcile/preflight portion of the aborted PR-D2 v1.

## 1. Goal

Provision the application-level Cloud SQL resources (databases, users, passwords) that the production cluster runs today, with production-parity attributes. Add the terraform outputs the downstream PR-D2b will consume. Fix the `check_legacy_voipbin_destroy_safety` preflight and wire `--force-destroy-legacy-voipbin` end-to-end. Update legacy ssl_mode test expectations.

**Explicitly out of scope (deferred to PR-D2b / PR-D2c):**
- Any `k8s/**/*.yaml` change.
- Any `scripts/secret_schema.py` change.
- Any `scripts/k8s.py` substitution-map change.
- Any ansible change.
- DSN rendering — the new sensitive outputs sit in terraform state, accessible via `reconcile_outputs` but not yet plumbed into Pods.

## 2. Production reality (verified 2026-05-12)

Source: live `gcloud sql` inspection of the production project plus an in-memory `sops --decrypt`. Identifiers below are abstracted (`<prod-project>`, `<prod-mysql>`, `<prod-postgres>`); shapes only.

### MySQL instance
- Version pin track: MYSQL_8_0 (matches PR-D1).
- Tier: db-f1-micro, ZONAL, ipv4 disabled, sslMode `ALLOW_UNENCRYPTED_AND_ENCRYPTED`.
- Databases (charset utf8mb3 / collation utf8mb3_general_ci): `bin_manager`, `asterisk`.
- Application users (BUILT_IN):
  - `bin-manager@%` — read/write `bin_manager`
  - `asterisk@%` — read/write `asterisk`
  - `call-manager@%` — production has it
  - `kamailioro@10.0.0.0/255.0.0.0` — Kamailio realtime read-only

### Postgres instance
- Version pin: POSTGRES_17.
- Tier: db-f1-micro, ZONAL, ipv4 disabled, sslMode `ALLOW_UNENCRYPTED_AND_ENCRYPTED`.
- Databases (UTF8 / en_US.UTF8): `bin_manager` (RAG tables + pgvector).
- Application user: `bin-manager` (host pinning not supported on Postgres).

## 3. Pre-flight audits (mandatory before design APPROVED)

### 3.1 Legacy `voipbin` MySQL consumer audit

**install-repo:**

```bash
cd ~/gitvoipbin/install-pr-d2
grep -rn 'voipbin@\|"voipbin":' k8s/ ansible/ 2>&1 | \
    grep -vE 'voipbin-(mysql|postgres|gke|node|recordings|tmp|media)'
```

Result: zero hits.

**monorepo (read-only):**

```bash
grep -rn '"voipbin"' ~/gitvoipbin/monorepo/bin-* ~/gitvoipbin/monorepo/voip-*-proxy/ \
    --include='*.go' --include='*.yaml' --include='*.yml' --include='*.conf'
```

Result: 148 hits across monorepo. Triaged by category:

- **Asterisk Stasis app name (~145 hits)** — production constant `defaultAstStasisApp = "voipbin"` in `bin-common-handler/pkg/requesthandler/main.go:132` and its vendored copies in every consuming service (`bin-agent-manager`, `bin-ai-manager`, `bin-billing-manager`, `bin-call-manager`, etc.). Every other hit is either a test fixture mirroring this constant in ARI event JSON, or the constant's vendored copy. NOT a MySQL username — it is the Asterisk `application` field set when registering with the Stasis app dialplan.
- **Sphinx project name (`bin-api-manager/docsdev/source/conf.py:10,123,131`)** — Sphinx documentation project metadata.
- **OpenAPI redoc artifact** — `bin-api-manager/gens/openapi_redoc/api.html` references `voipbin` branding strings. Not captured by the design's grep `--include` flags above (missing `*.html`), so the 148 count above does not include it; mentioned here for completeness.

**Conclusion:** Across all monorepo hits, zero authenticate as MySQL user `voipbin`. The production code constant `defaultAstStasisApp` is the Asterisk Stasis application name (a string passed to Asterisk via the dialplan when entering Stasis), unrelated to Cloud SQL credentials. PR-D2a's terraform destroy of `google_sql_user.voipbin` and `google_sql_database.voipbin` is safe.

**Live cluster verification (2026-05-12):**

```bash
gcloud sql databases list --instance=voipbin-mysql --project=voipbin-install-dev
```

Returned the legacy `voipbin` database (charset utf8mb3). **The common dev-path operator MUST pass `--force-destroy-legacy-voipbin` on the next `voipbin-install apply`** because the database is present. This is intentional (PR-D1 created it). The post-PR-D2a state will be: `bin_manager` and `asterisk` databases exist; `voipbin` is destroyed.

### 3.2 Reconcile-outputs FIELD_MAP scope

The 5 new sensitive outputs are NOT added to `FIELD_MAP` in PR-D2a. Rationale: FIELD_MAP entries write into `config.yaml`, which is persisted to disk. Persisting raw passwords to `config.yaml` defeats the `sensitive=true` posture. Instead, the outputs are consumed by PR-D2b's substitution map directly from `terraform output -json` (programmatic, never echoed to a shell). PR-D2a adds NO new FIELD_MAP entries; PR-D2b wires the consumption path.

## 4. Change surface

### 4.1 `terraform/cloudsql.tf`

#### MySQL section — rewrite

- Delete: `random_password.cloudsql_password` (MySQL admin), `google_sql_database.voipbin`, `google_sql_user.voipbin`.
- Mutate: `google_sql_database_instance.voipbin.settings.ip_configuration.ssl_mode = "ALLOW_UNENCRYPTED_AND_ENCRYPTED"`.
- Add: `random_password.mysql_{bin_manager,asterisk,call_manager,kamailioro}` (4 resources), each:

  ```hcl
  resource "random_password" "mysql_bin_manager" {
    length           = 24
    special          = true
    override_special = "!*+-._~"
  }
  ```

  **Alphabet rationale (locked by roadmap):** `!*+-._~` is a strict subset of RFC 3986 §3.2.1 userinfo (`!` `*` `+` are sub-delims, `-` `.` `_` `~` are unreserved). All 7 chars are also safe inside MySQL DSN userinfo (per go-sql-driver/mysql parser) and shell. 69-char alphabet × 24 chars ≈ 146 bits entropy. This makes PR-D2c's URL-safety work purely a regression test.

- Add: `google_sql_database.voipbin_mysql_{bin_manager,asterisk}` — name `bin_manager` and `asterisk`, `charset = "utf8mb3"`, `collation = "utf8mb3_general_ci"`.
- Add: `google_sql_user.voipbin_mysql_{bin_manager,asterisk,call_manager,kamailioro}` — names `bin-manager`, `asterisk`, `call-manager`, `kamailioro`. The kamailioro user has `host = "10.0.0.0/255.0.0.0"`; others omit host (defaults to `%`).

#### Postgres section — add per-app

- Mutate: `google_sql_database_instance.voipbin_postgres.settings.ip_configuration.ssl_mode = "ALLOW_UNENCRYPTED_AND_ENCRYPTED"`.
- Add: `random_password.postgres_bin_manager` (same shape as MySQL passwords).
- Add: `google_sql_database.voipbin_postgres_bin_manager` — name `bin_manager`, `charset = "UTF8"`, `collation = "en_US.UTF8"`.
- Add: `google_sql_user.voipbin_postgres_bin_manager` — name `bin-manager`, no host field.

### 4.2 `terraform/outputs.tf`

Add 5 outputs, all `sensitive = true`:

| output | value |
|---|---|
| `cloudsql_mysql_password_bin_manager` | `random_password.mysql_bin_manager.result` |
| `cloudsql_mysql_password_asterisk` | `random_password.mysql_asterisk.result` |
| `cloudsql_mysql_password_call_manager` | `random_password.mysql_call_manager.result` |
| `cloudsql_mysql_password_kamailioro` | `random_password.mysql_kamailioro.result` |
| `cloudsql_postgres_password_bin_manager` | `random_password.postgres_bin_manager.result` |

Each carries a description block noting: "Programmatic use only — do NOT run `terraform output -json` interactively; pipe to a script or sink. PR-D2b consumes via reconcile_outputs."

### 4.3 `scripts/terraform_reconcile.py`

Append 7 entries; remove 2 legacy entries. Final delta:

| tf_address | import_id | parent_check |
|---|---|---|
| `google_sql_database.voipbin_mysql_bin_manager` | `projects/{project}/instances/voipbin-mysql/databases/bin_manager` | yes (mysql instance) |
| `google_sql_database.voipbin_mysql_asterisk` | `projects/{project}/instances/voipbin-mysql/databases/asterisk` | yes |
| `google_sql_user.voipbin_mysql_bin_manager` | `{project}/voipbin-mysql/bin-manager` | yes |
| `google_sql_user.voipbin_mysql_asterisk` | `{project}/voipbin-mysql/asterisk` | yes |
| `google_sql_user.voipbin_mysql_call_manager` | `{project}/voipbin-mysql/call-manager` | yes |
| `google_sql_database.voipbin_postgres_bin_manager` | `projects/{project}/instances/{env}-postgres/databases/bin_manager` | yes (postgres instance) |
| `google_sql_user.voipbin_postgres_bin_manager` | `{project}/{env}-postgres/bin-manager` | yes |

Removed: `google_sql_database.voipbin`, `google_sql_user.voipbin`.

**kamailioro user intentionally absent.** Reason: provider import id format is `{project}/{instance}/{host}/{name}` parsed by split-by-slash. Host `10.0.0.0/255.0.0.0` contains slashes → 5-tuple instead of 4-tuple → import fails. `terraform apply` creates the user on first run; state persists.

### 4.4 `scripts/preflight.py`

- Implement `check_legacy_voipbin_destroy_safety(config, force=False) -> None`. On `ecd2072` base this function does NOT exist; D2a adds it from scratch (the v1 aborted attempt used `run_cmd(..., capture_output=True)` which raised `TypeError` — see retrospective root cause #2).
- Use `run_cmd(..., capture=True)` matching `scripts/utils.py:run_cmd` signature.
- Probe with `gcloud sql databases describe voipbin --instance=voipbin-mysql --project=<project>`. rc != 0 → legacy db is gone (or unreachable) → return silently. rc == 0 → legacy db exists; raise `PreflightError` with operator-facing message stating: "the legacy `voipbin` MySQL database still exists on this project. PR-D2 destroys it and replaces with per-app databases (`bin_manager`, `asterisk`). On the **common dev path** (`voipbin-install-dev` confirmed 2026-05-12 still has the legacy db from PR-D1), re-run `voipbin-install apply --force-destroy-legacy-voipbin` to opt in. See `docs/operations/cloud-sql-credentials.md`."
- `force=True` short-circuit at function entry.
- Add a soft-skip when `gcp_project_id` is empty (fresh `voipbin-install init` state).

### 4.5 `scripts/cli.py` + `scripts/commands/apply.py`

- Add `--force-destroy-legacy-voipbin` Click flag on the `apply` command. Default: `False`. Update the call site `scripts/cli.py:63` `cmd_apply(auto_approve=..., dry_run=..., stage=stage, force_destroy_legacy_voipbin=force_destroy_legacy_voipbin)`.
- Update `cmd_apply` signature in `scripts/commands/apply.py:53` to accept the new keyword.
- Thread it through `cmd_apply` into `config.force_destroy_legacy_voipbin` (attribute set on InstallerConfig instance, NOT persisted to config.yaml). **Ordering requirement**: the assignment `config.force_destroy_legacy_voipbin = force_destroy_legacy_voipbin` happens IMMEDIATELY after `config.load()` and BEFORE `run_pipeline(...)`, so `_run_terraform_apply`'s `getattr(config, 'force_destroy_legacy_voipbin', False)` reads the operator-supplied value.
- `scripts/pipeline.py:_run_terraform_apply` reads `getattr(config, 'force_destroy_legacy_voipbin', False)` and passes to `check_legacy_voipbin_destroy_safety`.

### 4.6 Legacy ssl_mode test updates

Two existing tests assert the legacy `ENCRYPTED_ONLY` value. PR-D2a updates them to the new production-parity literal:

- `tests/test_pr_c_mysql_private_ip_flip.py::test_cloudsql_mysql_private_only` — change ssl_mode assertion to `ALLOW_UNENCRYPTED_AND_ENCRYPTED`.
- `tests/test_pr_d1_cloudsql_postgres.py::TestPostgresInstanceResource::test_ssl_mode_encrypted_only` — rename to `test_ssl_mode_allows_unencrypted_for_production_parity`, update assertion.

Both tests gain a comment citing `docs/security/cis-deviations.md`.

### 4.7 New test file: `tests/test_pr_d2a_cloudsql_resources.py`

42 cases across these classes:

| Class | Cases | Coverage |
|---|---|---|
| `TestMySQLLegacyResourcesRemoved` | 3 | `google_sql_database.voipbin`, `google_sql_user.voipbin`, `random_password.cloudsql_password` absent |
| `TestMySQLApplicationDatabases` | 4 | 2 dbs (bin_manager, asterisk), each with charset utf8mb3 + collation utf8mb3_general_ci |
| `TestMySQLApplicationUsers` | 4 | 4 users with correct hyphenated names |
| `TestMySQLSslMode` | 1 | `ALLOW_UNENCRYPTED_AND_ENCRYPTED` literal |
| `TestKamailioroHostPin` | 2 | host `10.0.0.0/255.0.0.0` literal; resource present |
| `TestPostgresApplicationDb` | 2 | bin_manager db (UTF8/en_US.UTF8), bin-manager user |
| `TestPostgresAdminPreserved` | 1 | `voipbin_postgres` admin user untouched from PR-D1 |
| `TestPostgresSslMode` | 1 | `ALLOW_UNENCRYPTED_AND_ENCRYPTED` literal |
| `TestRandomPasswordsAlphabet` | 5 | 5 random_password resources; length=24; special=true; override_special exactly `!*+-._~` |
| `TestSensitiveOutputs` | 5 | 5 outputs declared sensitive=true with correct value reference |
| `TestReconcileRegistryEntries` | 7 | 7 new entries with expected import_id |
| `TestKamailioroNotInRegistry` | 1 | kamailioro absent from reconcile registry |
| `TestNoFieldMapPasswordEntries` | 1 | FIELD_MAP MUST NOT contain any password entry (passwords flow via TF outputs only, not config.yaml) |
| `TestLegacyAliasPreserved` | 1 | `_build_substitution_map` still emits `PLACEHOLDER_CLOUDSQL_PRIVATE_IP[_CIDR]` keys (manifest compatibility regression guard) |
| `TestPreflightLegacyVoipbinForceTrue` | 1 | `check_legacy_voipbin_destroy_safety(config, force=True)` returns immediately |
| `TestPreflightLegacyVoipbinForceFalseRaises` | 1 | with mocked `run_cmd` returning rc=0 (db exists), function raises `PreflightError` |
| `TestPreflightLegacyVoipbinForceFalseSilent` | 1 | with mocked `run_cmd` returning rc!=0 (db absent), function returns silently |
| `TestCliForceFlagWired` | 1 | `CliRunner`-driven end-to-end: invoke `voipbin-install apply --force-destroy-legacy-voipbin --dry-run` with `check_legacy_voipbin_destroy_safety` patched to capture its `force` kwarg; assert it received `force=True`. (Drives production code, not Click introspection — see retrospective skill update.) |

**Total: 42 cases.**

### 4.8 Synthetic injection mutants (gate ≥ 5 catches)

1. `ssl_mode = "ENCRYPTED_ONLY"` on MySQL → must trip `TestMySQLSslMode`.
2. user name `bin_manager` (underscore typo) → must trip `TestMySQLApplicationUsers`.
3. drop `random_password.mysql_call_manager` → must trip `TestRandomPasswordsAlphabet`.
4. add stale `random_password "cloudsql_password"` → must trip `TestMySQLLegacyResourcesRemoved`.
5. swap `utf8mb4` for `utf8mb3` → must trip `TestMySQLApplicationDatabases`.
6. (extra) drop `force=True` short-circuit in `check_legacy_voipbin_destroy_safety` → must trip `TestPreflightLegacyVoipbinForceTrue`.
7. (extra) revert `capture=True` to `capture_output=True` in preflight → must trip `TestPreflightLegacyVoipbinForceFalseRaises` with `TypeError`.

### 4.9 Docs

- `docs/follow-ups.md` (new): utf8mb4 migration stub, operator-personal-users stub.
- `docs/security/cis-deviations.md` (new): ssl_mode loosening rationale, root@% out-of-terraform note, password_validation_policy deferral.
- `docs/operations/cloud-sql-credentials.md` (new): per-app password rotation runbook, `gcloud sql users set-password` for admin users.

## 5. State migration plan (terraform plan against dev)

```
- google_sql_database.voipbin                       # destroy
- google_sql_user.voipbin                            # destroy
- random_password.cloudsql_password                 # destroy
+ random_password.mysql_bin_manager
+ random_password.mysql_asterisk
+ random_password.mysql_call_manager
+ random_password.mysql_kamailioro
+ random_password.postgres_bin_manager
+ google_sql_database.voipbin_mysql_bin_manager
+ google_sql_database.voipbin_mysql_asterisk
+ google_sql_user.voipbin_mysql_bin_manager
+ google_sql_user.voipbin_mysql_asterisk
+ google_sql_user.voipbin_mysql_call_manager
+ google_sql_user.voipbin_mysql_kamailioro
+ google_sql_database.voipbin_postgres_bin_manager
+ google_sql_user.voipbin_postgres_bin_manager
~ google_sql_database_instance.voipbin              # in-place: ssl_mode change
~ google_sql_database_instance.voipbin_postgres     # in-place: ssl_mode change
```

Net: **3 destroy / 13 add / 2 in-place**. Cloud SQL ssl_mode change does NOT restart the instance per provider docs.

## 6. Verification

- `pytest -q --ignore=tests/test_pr_n_oslogin.py` green (PR-N env mismatch unrelated; documented in retro).
- `terraform fmt -check terraform/` clean.
- `terraform -chdir=terraform validate` clean.
- `bash scripts/dev/check-plan-sensitive.sh docs/plans/2026-05-12-pr-d2a-cloudsql-terraform-resources-design.md` returns OK.
- `git diff --cached` introduces ZERO new sensitive literals (production domain, production private IPs, production project ID — see roadmap §sensitive scan).
- Synthetic injection: ≥ 5 mutants caught (target: 7/7).

## 7. Smoke dogfood (after merge)

**Baseline capture (before `voipbin-install apply`):**

```bash
kubectl get pods -A -o wide > /tmp/baseline-pre-d2a-pods.txt
kubectl get deploy,statefulset -A -o json | \
    jq '.items[] | {ns: .metadata.namespace, name: .metadata.name, ready: .status.readyReplicas, desired: .spec.replicas}' \
    > /tmp/baseline-pre-d2a-controllers.json
gcloud sql users list --instance=voipbin-mysql --format=json > /tmp/baseline-pre-d2a-mysql-users.json
```

**Apply invocation:**

```bash
voipbin-install apply --force-destroy-legacy-voipbin
```

The `--force-destroy-legacy-voipbin` flag is REQUIRED on `voipbin-install-dev` because PR-D1's legacy `voipbin` MySQL database is still present (verified 2026-05-12 in §3.1). The preflight gate would otherwise block.

**Post-apply assertions:**

1. terraform state shows 8 new resources (2 mysql dbs, 4 mysql users, 1 postgres db, 1 postgres user).
2. `gcloud sql users list --instance=voipbin-mysql --format=json` includes `bin-manager`, `asterisk`, `call-manager`, `kamailioro`.
3. The kamailioro row in the same JSON has `host == "10.0.0.0/255.0.0.0"` (catches reconcile-registry slash-in-host omission).
4. Pod inventory diff: `diff` between fresh `kubectl get pods` output and `/tmp/baseline-pre-d2a-pods.txt` shows zero changes for bin-manager / asterisk / rag-manager namespaces. **This is a negative regression guard** — it catches a stray `k8s/` edit. It is NOT a positive functional signal; positive Pod-up signals come from D2b/D2c.
5. `bash scripts/dev/check-plan-sensitive.sh` returns OK on the merged diff.

**Auto-revert trigger:** if any of (1)-(5) fail, file an auto-revert PR (squash-revert of the merge commit) following the same review loop (abbreviated R1 only), then report to pchero.

## 8. Risks & tradeoffs

| Risk | Mitigation |
|---|---|
| `kamailioro` import unavailable; manual recovery if `apply` errors | Documented in §4.3. First apply creates the user; subsequent applies are idempotent. |
| Operator with `force_destroy_legacy_voipbin=True` on an upgraded cluster with data | Operator opt-in only; default False. Pre-apply preflight gives clear error before destroy. |
| ssl_mode loosening flagged as CIS regression | Filed in `docs/security/cis-deviations.md` with hardening path (PR-S). |
| utf8mb3 deprecated upstream | Tracked in `docs/follow-ups.md` (utf8mb4 migration). |
| MySQL root@% retained outside terraform | Documented in `docs/operations/cloud-sql-credentials.md`. |

## 9. Exit criteria

- All 8 application resources present in terraform state on dev.
- `pytest` green (baseline + 42 new).
- 5-iter min synthetic injection ≥ 5 catches; this design targets 7.
- Sensitive scan clean.
- 3-iter min PR review APPROVED (R1+R2+R3 each fresh subagent).
- Smoke dogfood §7 all 5 assertions pass.

## 10. Out of scope (handed to PR-D2b)

- `k8s/backend/secret.yaml` DSN literal rewrite.
- `scripts/secret_schema.py` DSN default rewrite.
- `scripts/k8s.py` substitution map new entries (password placeholders, new IP placeholders).
- bin-manager Pod successfully connecting to new MySQL user.
- rag-manager Pod connecting to new Postgres user.

## 11. Out of scope (handed to PR-D2c)

- ansible kamailio_auth_db_url wiring.
- Postgres password URL-safety regression test (alphabet is locked here in §4.1; D2c verifies, doesn't regenerate).

## Appendix A — pre-design audits

- §3.1 install-repo audit: 0 hits. ✓
- §3.1 monorepo audit: 148 hits, all false-positives (Asterisk Stasis app constant + Sphinx project name + OpenAPI HTML branding). See §3.1 for the full triage. ✓
- §3.2 FIELD_MAP scope decision: passwords never persisted to config.yaml.
- `bin_manager` MySQL schema migration trace (for D2b precondition, not blocking D2a): `bin-manager` Go service runs `golang-migrate` at pod startup (PR-D2b will verify the exact entrypoint).
- `asterisk` MySQL schema provenance trace (for D2b precondition): owned by ansible-provisioned Asterisk realtime; the `asterisk` MySQL user reads CRUD from realtime tables, and the schema lives in `ansible/roles/asterisk/files/` or Asterisk's own migration path. PR-D2b/c will pin down.
