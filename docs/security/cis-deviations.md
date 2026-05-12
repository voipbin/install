# CIS / security baseline deviations

Each entry lists the deviation, the production-parity reason, and the
suggested hardening path.

## Cloud SQL `ssl_mode = ALLOW_UNENCRYPTED_AND_ENCRYPTED`

**CIS reference:** Cloud SQL benchmark, "Require SSL/TLS for all Cloud SQL
database instances".

**Deviation source:** PR-D2a (2026-05-12).

**Reason.** Install repo's MySQL and Postgres instances ship with `ssl_mode
= "ALLOW_UNENCRYPTED_AND_ENCRYPTED"` to match production. Production runs
application clients (`bin-manager`, `asterisk`, `kamailioro`) that connect
without SSL because their DSN strings do not set `sslmode=require` (Postgres)
/ `tls=true` (MySQL). Flipping the instance to `ENCRYPTED_ONLY` without
first updating every DSN client would surface as connection-refused errors
in every consuming Pod.

**Hardening path.** A future PR (working title PR-S, "tighten Cloud SQL
ssl_mode") should:
1. Update every DSN default in `scripts/secret_schema.py` to set `tls=true`
   (MySQL) / `sslmode=require` (Postgres).
2. Verify every consuming service (`bin-manager`, `asterisk`, `kamailioro`,
   `rag-manager`) accepts the new DSN string.
3. Roll the configuration change through the production cluster first, then
   flip install repo's `ssl_mode` to `ENCRYPTED_ONLY` once consumers are
   demonstrably healthy.
4. Add a regression test asserting install-repo's `ssl_mode` matches the
   production cluster's mode at any given time.

## Cloud SQL built-in `root@%` admin user not Terraform-managed

**CIS reference:** Cloud SQL benchmark, "Ensure no default Cloud SQL user
exists with an unrestricted host".

**Deviation source:** PR-D2a (2026-05-12). The MySQL instance retains the
built-in `root@%` admin user. Cloud SQL does not allow this user to be
deleted, only its password rotated. Operators MUST NOT attempt to manage
`root` via Terraform. Rotation:

```
gcloud sql users set-password root --instance=<env>-mysql --project=<project>
```

See `docs/operations/cloud-sql-credentials.md` for the full runbook.

**Hardening path.** Pinning `root@%` to a private network range (same
strategy as `kamailioro`) is possible but Cloud SQL applies the change with
an instance restart. Coordinate with production maintenance windows.

## Cloud SQL `password_validation_policy` not set

**CIS reference:** Cloud SQL benchmark, "Ensure that the
'cloudsql.enable_password_validation' instance flag is set to 'on'".

**Deviation source:** PR-D2a (2026-05-12). Production does not set the flag;
PR-D2a inherits the same posture for parity. All `random_password` resources
in `terraform/cloudsql.tf` produce 24-character secrets from a 69-char
alphabet (≈146 bits entropy), which far exceeds any policy minimum.
Application users are never created with operator-chosen passwords.

**Hardening path.** A future PR may enable
`cloudsql.enable_password_validation` once production also enables it,
preserving parity.
