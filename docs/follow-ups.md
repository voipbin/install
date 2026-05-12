# Follow-up work items

Items deliberately deferred from in-flight PRs. Each entry lists the PR that
identified the work, the rationale for deferral, and the suggested scope of
the follow-up PR.

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
