# PR-C — Terraform MySQL private-IP flip + dead SA removal

**Date:** 2026-05-12
**Author:** Hermes (CPO)
**Status:** Design v1
**Repo:** `voipbin/install`
**Branch:** `NOJIRA-PR-C-mysql-private-ip-flip`
**Parent:** main `1bbca45` (PR-A, PR-H, PR-B, PR-F, PR-G all merged)
**Roadmap slot:** PR-C (Phase 3 critical path; depends on PR-A + PR-B)
**LOC estimate:** ~420 (per roadmap v3 §6)
**Gaps closed:** GAP-01 (MySQL public IP), GAP-08 (`cloudsql_private_ip` Terraform-managed), GAP-11 (`cloudsql_ip` consumer cleanup), GAP-19 (dead `sa_cloudsql_proxy`), A-1 (service_accounts.tf comment), A-4 (cloudsql_ip output stale), A-5 (ansible_runner.py dead outputs).

## 1. Context

PR-A landed `reconcile_imports` + `reconcile_outputs` stages. PR-B provisioned the VPC peering scaffold (`google_compute_global_address.cloudsql_peering` + `google_service_networking_connection.voipbin`) but Cloud SQL itself is still `ipv4_enabled = true` (`terraform/cloudsql.tf:22`).

PR-C completes the cloudsql-proxy decommission started in PR #5a (manifest layer) by flipping the MySQL instance to private-IP-only, deleting the dead `sa_cloudsql_proxy` service account, and wiring `cloudsql_mysql_private_ip` into `reconcile_outputs` FIELD_MAP so PR #5a's `check_cloudsql_private_ip` preflight passes on fresh installs.

Production reference (verified during PR-B): MySQL is private-IP-only via peering on the same VPC pattern PR-B introduced. `ssl_mode = ENCRYPTED_ONLY` already matches.

## 2. Scope

### 2.1 `terraform/cloudsql.tf`

```hcl
resource "google_sql_database_instance" "voipbin" {
  # ... unchanged: name, database_version, region, deletion_protection ...

  settings {
    # ... unchanged: tier, disk_*, availability_type ...

    ip_configuration {
      ipv4_enabled       = false
      private_network    = google_compute_network.voipbin.id
      ssl_mode           = "ENCRYPTED_ONLY"
      allocated_ip_range = google_compute_global_address.cloudsql_peering.name
    }

    # ... unchanged: backup_configuration, maintenance_window ...
  }

  depends_on = [
    time_sleep.api_propagation,
    google_service_networking_connection.voipbin,
  ]
}

# DELETED: google_service_account.sa_cloudsql_proxy
# DELETED: google_project_iam_member.sa_cloudsql_proxy_client
```

`allocated_ip_range` pins the instance to PR-B's named peering range. `depends_on` ensures the peering exists before instance creation.

### 2.2 `terraform/outputs.tf`

- **DELETE** `cloudsql_ip` output (`outputs.tf:59-62`). Public IP no longer exists; the output would return `null` and break consumers.
- **ADD** `cloudsql_mysql_private_ip` output:
  ```hcl
  output "cloudsql_mysql_private_ip" {
    description = "Cloud SQL MySQL private IP (consumed by reconcile_outputs FIELD_MAP)."
    value       = google_sql_database_instance.voipbin.private_ip_address
  }
  ```
- **KEEP** `cloudsql_connection_name` output (still useful for operator tooling; rename to `cloudsql_mysql_connection_name` deferred to PR-D1 when Postgres adds its own connection_name).

### 2.3 `scripts/terraform_reconcile.py` — FIELD_MAP additions

Append to `FIELD_MAP` (PR-G is current sole consumer; PR-C adds two more — IP and CIDR):

```python
FIELD_MAP: list[TfOutputFieldMapping] = [
    # ... existing 2 PR-G entries (recordings_bucket_name, tmp_bucket_name) ...
    TfOutputFieldMapping(
        tf_key="cloudsql_mysql_private_ip",
        cfg_key="cloudsql_private_ip",
        validator=_is_valid_ipv4_address,
    ),
    TfOutputFieldMapping(
        tf_key="cloudsql_peering_range_cidr",
        cfg_key="cloudsql_private_ip_cidr",
        validator=_is_valid_ipv4_cidr,
    ),
]
```

New validator helpers near `_is_valid_bucket_name`. Restricted to IPv4 (Cloud SQL Private IP on Google VPC peering is always IPv4 — early rejection of misconfigurations):

```python
import ipaddress

def _is_valid_ipv4_address(v: Any) -> bool:
    if not isinstance(v, str) or not v:
        return False
    try:
        ipaddress.IPv4Address(v)
        return True
    except (ValueError, ipaddress.AddressValueError):
        return False

def _is_valid_ipv4_cidr(v: Any) -> bool:
    if not isinstance(v, str) or not v:
        return False
    try:
        ipaddress.IPv4Network(v, strict=False)
        return True
    except (ValueError, ipaddress.AddressValueError, ipaddress.NetmaskValueError):
        return False
```

The validators reject any non-IPv4/CIDR string (including PR #5a sentinel `cloudsql-private.invalid` and PR-A scaffolding empty). `reconcile_outputs` only writes valid values, so PR #5a's `check_cloudsql_private_ip` preflight remains the ultimate guard for invalid pipeline state.

### 2.4 `scripts/ansible_runner.py` — dead output cleanup (A-5, GAP-11)

`scripts/ansible_runner.py:31-34` flattens `cloudsql_connection_name` and `cloudsql_ip` into `ansible_vars`. Grep across `ansible/` confirms no role/template reads either variable (anomaly A-5). PR-C removes both lines.

**Also**: `tests/test_ansible_runner.py:38, 46-72` exercise these flattened keys. Update those tests in lockstep — drop the assertions for `cloudsql_connection_name`/`cloudsql_ip` in the ansible_vars dict.

If grep surfaces unexpected consumers, scope expands to update them or revert this cleanup. To verify at implementation time: `grep -rn 'cloudsql_ip\|cloudsql_connection_name' ansible/ scripts/ tests/ | grep -v '.md'`.

### 2.5 `terraform/service_accounts.tf` (A-1 cleanup)

File is currently a comment-only stub referencing `sa-cloudsql-proxy` as a current resource. PR-C: delete the file entirely (Terraform parses any `.tf` file in the dir; removing one with no resources is safe).

### 2.6 `scripts/terraform_reconcile.py::build_registry` — MySQL entry update

The existing MySQL instance import is at `terraform_reconcile.py` around the cloudsql block. After PR-C, the instance still exists with the same name — no registry change required, but verify the `gcloud_check` path still works (it should: `gcloud sql instances describe` returns the instance regardless of IP mode).

### 2.7 Tests — `tests/test_pr_c_mysql_private_ip_flip.py` (~11 tests)

1. **`test_cloudsql_mysql_private_only`** — parse `terraform/cloudsql.tf`, assert `ipv4_enabled = false`, `private_network = google_compute_network.voipbin.id`, `allocated_ip_range = google_compute_global_address.cloudsql_peering.name`, `ssl_mode = "ENCRYPTED_ONLY"`.
2. **`test_cloudsql_depends_on_peering`** — instance `depends_on` list includes `google_service_networking_connection.voipbin`. Uses hcl2 parser pattern from `tests/test_pr_b_vpc_peering_scaffold.py`.
3. **`test_no_cloudsql_proxy_sa`** — `terraform/cloudsql.tf` has no `sa_cloudsql_proxy` resource and no `sa_cloudsql_proxy_client` IAM binding.
4. **`test_service_accounts_tf_removed`** — `terraform/service_accounts.tf` does not exist (pattern from `tests/test_pr5a_cloudsql_removal.py`).
5. **`test_cloudsql_ip_output_deleted`** — `terraform/outputs.tf` has no `output "cloudsql_ip"` (regression guard for A-4).
6. **`test_cloudsql_mysql_private_ip_output_present`** — `terraform/outputs.tf` declares `cloudsql_mysql_private_ip`.
7. **`test_field_map_includes_mysql_private_ip`** — `terraform_reconcile.FIELD_MAP` contains an entry mapping `cloudsql_mysql_private_ip` → `cloudsql_private_ip`.
8. **`test_field_map_includes_peering_cidr`** — `terraform_reconcile.FIELD_MAP` contains an entry mapping `cloudsql_peering_range_cidr` → `cloudsql_private_ip_cidr`.
9. **`test_ipv4_validator_rejects_sentinel_and_garbage`** — `_is_valid_ipv4_address` returns False for `"cloudsql-private.invalid"`, `""`, `None`, IPv6 `"::1"`, non-string; True for `"10.1.2.3"`.
10. **`test_ipv4_cidr_validator`** — `_is_valid_ipv4_cidr` accepts `"10.0.0.0/20"`, rejects `"10.0.0.0"` (no prefix), `"not-cidr"`, IPv6, None.
11. **`test_ansible_runner_no_dead_outputs`** — `scripts/ansible_runner.py` source does not reference `cloudsql_ip` or `cloudsql_connection_name` in the flatten block.
12. **`test_reconcile_outputs_populates_cloudsql_private_ip_and_cidr`** — mock TF outputs `{cloudsql_mysql_private_ip: "10.0.0.5", cloudsql_peering_range_cidr: "10.0.0.0/20"}`, run `outputs(config, tf)`, assert config gets both `cloudsql_private_ip = "10.0.0.5"` and `cloudsql_private_ip_cidr = "10.0.0.0/20"`.

## 3. Out of scope

- Postgres instance (PR-D1).
- Per-app SQL users (PR-D2).
- Operator-supplied `cloudsql_private_ip` config field removal (PR-E).
- `cloudsql_connection_name` rename to `cloudsql_mysql_connection_name` (PR-D1 when Postgres adds parallel output).
- Full smoke dogfood execution (post-merge, separate step).

## 4. Migration

Operators with existing PR-A→PR-B-deployed clusters have:
- MySQL instance running on `ipv4_enabled=true` (public IP).
- PR-B's peering scaffold present but unused by Cloud SQL.

PR-C's `terraform apply` attempts to flip `ipv4_enabled` to `false`. Google's `google_sql_database_instance` provider DOES NOT support in-place change of this field on an instance with `deletion_protection=true`. Terraform plan will fail with a clear error.

Operator migration path (documented in PR-C description):
1. **Prerequisite**: PR-B must already be applied (`google_service_networking_connection.voipbin` peering active on `${env}-vpc`). PR-C cannot succeed without it.
2. `gcloud sql instances patch <env>-mysql --no-assign-ip --network=projects/<project>/global/networks/<env>-vpc --project=<project>` — triggers instance restart (~minutes downtime) and disables public IP. Existing authorized networks are dropped.
3. **DO NOT** run `voipbin-install apply --stage k8s_apply` between steps 2 and 4 — config still has sentinel `cloudsql_private_ip`, preflight will fail loudly (recoverable by completing step 4).
4. `voipbin-install apply` — `terraform plan` now reconciles cleanly because the instance already matches PR-C's desired state.

For fresh installs (no existing instance): `terraform apply` provisions the instance with private-IP-only from the start. No migration step needed.

For the dogfood project `voipbin-install-dev`: no Cloud SQL instance exists yet (smoke dogfood was structurally blocked by sentinel). Fresh apply will work.

**Dogfood teardown**: `deletion_protection=true` blocks `terraform destroy`. Operator path:
- Flip `deletion_protection = false` in `terraform/cloudsql.tf` (or `gcloud sql instances patch <env>-mysql --deletion-protection=false`).
- `voipbin-install apply` to push the deletion_protection flip.
- `voipbin-install destroy --auto-approve`.

## 5. Risks

- **In-place private-IP flip rejected by Cloud SQL** for existing public-IP instances: documented migration path above. PR description and `docs/operations/cloudsql-private-ip.md` updated.
- **Dead-output cleanup may have hidden consumer**: mitigated by grep verification at implementation time.
- **FIELD_MAP validator semantics**: `_is_valid_ip_address` rejects empty and sentinel. If Terraform output is genuinely empty (apply incomplete), reconcile silently skips — operator sees the existing PR #5a preflight error on next `apply k8s`, which guides them to re-run the pipeline. Acceptable.
- **`cloudsql_ip` output deletion is a breaking change** for any external script reading `terraform output cloudsql_ip`. Grep across repo confirms no such reader; document in PR description.

## 6. Smoke dogfood (post-merge)

Per roadmap v3 §7 + §8, **full smoke dogfood** runs after PR-C merges on `voipbin-install-dev`:

1. `voipbin-install init` already has config.yaml from prior attempt; re-init or reuse.
2. `voipbin-install apply --auto-approve` — should now reach `terraform_apply` successfully:
   - `terraform_init` ✓
   - `reconcile_imports` ✓ (no orphan resources)
   - `terraform_apply` provisions VPC + peering + MySQL private-IP + GKE + VMs + GCS buckets (~15-25 min, ~$0.24/hour)
   - `reconcile_outputs` populates `cloudsql_private_ip`, `cloudsql_private_ip_cidr` (from PR-B's `cloudsql_peering_range_cidr`), `recordings_bucket`, `tmp_bucket` (from PR-G)
   - `ansible_run` configures Kamailio + RTPEngine
   - `k8s_apply` deploys backend + voip + frontend — **`rag-manager` will CrashLoop** (Postgres instance not yet provisioned; PR-D1 territory). Expected and not a blocker for PR-C verification.

Expected P0 issues: 0. rag-manager CrashLoop is **expected and documented**, not a P0 since `voipbin-install apply` itself completes.

If unexpected P0 surfaces, Gap Addendum Protocol §4 classifies. If ≥2 P0, roadmap §5 abort triggers fire.

## 7. Checklist

- [x] Scope grounded in file:line (cloudsql.tf:1-64, outputs.tf:59-62, ansible_runner.py:31-34, service_accounts.tf, terraform_reconcile.py FIELD_MAP)
- [x] PR-B's peering resources consumed (allocated_ip_range, depends_on)
- [x] FIELD_MAP validator pattern reused (PR-G precedent)
- [x] Dead output cleanup grep-verified at impl time
- [x] Migration path explicit for existing operators (gcloud patch + apply)
- [x] Public IP output deletion noted as breaking change
- [x] Smoke dogfood gate identified (post-merge); rag-manager CrashLoop expected, not P0
- [ ] Design review iter 1
- [ ] Design review iter 2
