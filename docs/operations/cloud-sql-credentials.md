# Cloud SQL credentials operations runbook

Credentials for install repo's Cloud SQL MySQL and Postgres instances.

## Application users (Terraform-managed)

Created by `terraform/cloudsql.tf` with random passwords stored in Terraform
state. Values are exposed as `sensitive = true` outputs and consumed by
`scripts/k8s.py` (PR-D2b) to render DSN strings into `k8s/backend/secret.yaml`.

| User | Instance | Host | Purpose |
|---|---|---|---|
| `bin-manager` | `<env>-mysql` | `%` | Read/write `bin_manager` database |
| `asterisk` | `<env>-mysql` | `%` | Read/write `asterisk` database |
| `call-manager` | `<env>-mysql` | `%` | Read/write `bin_manager` (legacy production user) |
| `kamailioro` | `<env>-mysql` | `10.0.0.0/255.0.0.0` | Kamailio realtime read-only |
| `bin-manager` | `<env>-postgres` | (n/a) | Read/write `bin_manager` (RAG tables + pgvector) |

### Rotation

```bash
terraform taint random_password.mysql_bin_manager
terraform apply -auto-approve
```

After apply, re-run `voipbin-install apply --stage k8s_apply` so the
substituted DSN secret reaches running Pods. Pods will restart on Secret
rollout.

## Built-in admin users (NOT Terraform-managed)

The MySQL instance retains the Cloud SQL built-in `root@%` admin user.
Cloud SQL does not permit deletion. Rotate via:

```bash
gcloud sql users set-password root \
  --instance=<env>-mysql \
  --project=<project>
```

The Postgres `postgres` admin user IS Terraform-managed via
`google_sql_user.voipbin_postgres`; rotate the same way as application users.

## Reading sensitive outputs

`terraform output -json` prints the entire output JSON including sensitive
values. Do NOT run interactively (shell history captures the plaintext).
Pipe to a script or to the `scripts/k8s.py` substitution layer.

## PR-D2 destroy-safety gate

If `voipbin-install apply` on a project that has the legacy PR-D1 `voipbin`
database returns:

> The legacy `voipbin` MySQL database still exists on this project. PR-D2
> destroys it and replaces with per-app databases (`bin_manager`,
> `asterisk`). [...] re-run with
> `voipbin-install apply --force-destroy-legacy-voipbin` to opt in.

Confirm the legacy database is empty:

```bash
gcloud sql databases describe voipbin --instance=<env>-mysql --project=<project>
```

If empty (default state on a fresh dev cluster), re-run with the force flag.
If the database carries unexpected data, halt and contact the team — the
destroy is data-losing.

## Storing & encrypting credentials

All sensitive Terraform outputs are encrypted at rest under the project's
`google_kms_crypto_key` SOPS key (see `terraform/kms.tf`). Operators must
never copy decrypted credentials into config.yaml, secrets.yaml, or any
artifact committed to git.
