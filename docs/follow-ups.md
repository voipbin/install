# Follow-up work items

Items deliberately deferred from in-flight PRs. Each entry lists the PR that
identified the work, the rationale for deferral, and the suggested scope of
the follow-up PR.

## NOJIRA-Empty-DSN-Password-Installer-Side-Guard

**Source:** PR-D2b R1 review (2026-05-12).

`scripts/k8s.py:_build_substitution_map` calls `terraform_outputs.get(key, "")`
for each `PLACEHOLDER_DSN_PASSWORD_*` token. If reconcile_outputs fails to
populate one of these (e.g. terraform state drift), the rendered Secret
contains a DSN like `bin-manager:@10.x:3306/...` with an empty password.
Pods authenticate-fail silently with no installer-side warning.

Suggested scope:
- In `_build_substitution_map`, after the password block, iterate the 5
  `PLACEHOLDER_DSN_PASSWORD_*` keys; if any resolves to `""`, raise
  `RuntimeError` with the missing tf_output name.
- Or: extend the existing "unresolved placeholder warning" block in
  `_render_manifests` to specifically promote empty DSN-password values to
  an error.
- Update `TestEmptyPasswordSurfaceErrors` in
  `tests/test_pr_d2b_dsn_render.py` to assert the new error path (current
  test pins the silent-empty behavior so this follow-up is intentional).

## NOJIRA-Asterisk-MySQL-Schema-Migration-Job

**Source:** PR-D2b (2026-05-12).

The `bin_manager` MySQL schema is applied at install time via the
`database-migration` Kubernetes Job (`k8s/database/migration-job.yaml`) using
the `voipbin/bin-database` Docker image (built from `bin-dbscheme-manager`'s
`bin-manager/` Alembic stream). The parallel `asterisk` MySQL schema (from
`bin-dbscheme-manager/asterisk_config/`) is NOT applied by any install-repo
Job today. Production must run it out of band (separate Job or CI/CD
pipeline). This means a fresh `voipbin-install apply` provisions the
`asterisk` database (PR-D2a) and wires the DSN (PR-D2b), but the PJSIP
realtime tables are missing. Asterisk tolerates this by falling back to file
config — non-crashing but non-functional realtime.

Suggested scope:
- Add `k8s/database/asterisk-migration-job.yaml` mirroring
  `migration-job.yaml`, using a new image (e.g. `voipbin/asterisk-database`)
  built from `bin-dbscheme-manager/asterisk_config/`.
- Job consumes `DATABASE_DSN_ASTERISK` from the `voipbin` Secret.
- `k8s/database/kustomization.yaml` registers the new Job manifest.
- Smoke check: `kubectl get job -n bin-manager asterisk-migration -o jsonpath='{.status.succeeded}'` returns `1`.

## NOJIRA-Migrate-MySQL-utf8mb3-to-utf8mb4

**Source:** PR-D2a (2026-05-12).

PR-D2a creates the MySQL application databases `bin_manager` and `asterisk`
with `charset = "utf8mb3"` and `collation = "utf8mb3_general_ci"` to match
production. `utf8mb3` is deprecated upstream of MySQL 8.4. Production hit the
same constraint; the migration to `utf8mb4` is a coordinated schema PR that
touches the running schema, bin-manager Go code, dbscheme manifests, and the
live data conversion path.

Suggested scope:
- Update `terraform/cloudsql.tf` to `utf8mb4` / `utf8mb4_unicode_ci`.
- Add a one-time data migration step with downtime estimate.
- Coordinate with bin-manager team for 4-byte UTF-8 surrogate-pair handling
  across affected tables.

## NOJIRA-Operator-Personal-SQL-Users

**Source:** PR-D2a (2026-05-12).

Production has operator-personal MySQL/Postgres users (e.g. `pchero@%`).
Install repo does not create them. Operators who want personal debugging
credentials currently add them out-of-band via `gcloud sql users create`
and rotate via `set-password`.

Suggested scope:
- Optional `operators` list in `config.yaml` (username + optional host pin).
- `random_password` per operator + Terraform wiring.
- Documentation: operator users are NOT for application traffic and must
  never be reused as DSN credentials.
