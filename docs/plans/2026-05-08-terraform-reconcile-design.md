# Terraform Reconcile Stage — Design

> **Superseded by PR-A (2026-05-12)** — the single `terraform_reconcile` stage is now split into `reconcile_imports` (before apply) + `reconcile_outputs` (after apply). See `docs/plans/2026-05-12-pr-a-pipeline-reconcile-split-design.md`.

**Date:** 2026-05-08
**Status:** Approved
**Context:** `voipbin-install apply` fails with Terraform 409 "already exists" errors when
GCP resources exist from a previous partially-failed deployment but are absent from Terraform
state. This design adds a reconciliation stage that imports those resources before apply runs.

---

## Problem

When a deployment fails mid-run, Terraform may have created some GCP resources while the
state backend recorded none (or only some) of them. On the next `apply` run, Terraform tries
to create resources that already exist, producing 409 errors and halting the pipeline.

---

## Solution: `terraform_reconcile` Stage

### Architecture

```
terraform_init → terraform_reconcile → terraform_apply → ansible_run → k8s_apply
```

A new stage sits between `terraform_init` and `terraform_apply`. It:

1. Checks Terraform state for already-managed resources (skip those)
2. Queries GCP for each resource the installer would create
3. Collects any that exist in GCP but not in state
4. Prompts the user to confirm importing them
5. Imports confirmed resources, then proceeds to `terraform_apply`

If no conflicts are found the stage exits immediately — zero overhead for clean deploys.

**New file:** `scripts/terraform_reconcile.py`
**Called from:** `pipeline.py`, between the `terraform_init` and `terraform_apply` stages.

---

## Resource Registry

### Excluded resource types

These four types are excluded from the registry — they either cannot be imported or do not
produce 409 errors in practice:

| Type | Reason |
|---|---|
| `google_project_service` | Enabling an already-enabled API is idempotent — never 409 |
| `google_project_iam_member` | IAM bindings are additive — never 409 |
| `random_password` | Import regenerates the value — would silently break the DB password |
| `time_sleep` | No GCP API backing — not importable |

### Included resources (~35 entries, built dynamically from config)

**Service Accounts (4)**
- `google_service_account.sa_cloudsql_proxy`
- `google_service_account.sa_gke_nodes`
- `google_service_account.sa_kamailio`
- `google_service_account.sa_rtpengine`

**KMS (2, imported in dependency order — key ring before crypto key)**
- `google_kms_key_ring.voipbin_sops`
- `google_kms_crypto_key.voipbin_sops_key`

**Network (8)**
- `google_compute_network.voipbin`
- `google_compute_subnetwork.voipbin_main`
- `google_compute_router.voipbin`
- `google_compute_router_nat.voipbin`
- `google_compute_firewall.fw_allow_internal`
- `google_compute_firewall.fw_gke_internal`
- `google_compute_firewall.fw_healthcheck`
- `google_compute_firewall.fw_iap_ssh`
- `google_compute_firewall.fw_kamailio_sip`
- `google_compute_firewall.fw_rtpengine_control`
- `google_compute_firewall.fw_rtpengine_rtp`
- `google_compute_firewall.fw_vm_to_infra`

**Compute (dynamic — count-based entries expanded from `kamailio_count`/`rtpengine_count`)**
- `google_compute_address.nat_ip`
- `google_compute_address.kamailio_lb_external`
- `google_compute_address.kamailio_lb_internal`
- `google_compute_address.rtpengine[i]` × rtpengine_count
- `google_compute_http_health_check.kamailio_external`
- `google_compute_health_check.kamailio_internal`
- `google_compute_target_pool.kamailio`
- `google_compute_region_backend_service.kamailio_internal`
- `google_compute_forwarding_rule.kamailio_internal`
- `google_compute_forwarding_rule.kamailio_tcp_sip`
- `google_compute_forwarding_rule.kamailio_tcp_wss`
- `google_compute_forwarding_rule.kamailio_udp_sip`
- `google_compute_instance_group.kamailio`
- `google_compute_instance.kamailio[i]` × kamailio_count
- `google_compute_instance.rtpengine[i]` × rtpengine_count

**Storage (2)**
- `google_storage_bucket.media`
- `google_storage_bucket.terraform_state` ← special case (see below)

**Cloud SQL (3, imported in dependency order — instance before database and user)**
- `google_sql_database_instance.voipbin`
- `google_sql_database.voipbin`
- `google_sql_user.voipbin`

**GKE (2, imported in dependency order — cluster before node pool)**
- `google_container_cluster.voipbin`
- `google_container_node_pool.voipbin`

### Special case: Terraform state bucket

`google_storage_bucket.terraform_state` uses a globally unique GCS name. Before importing,
verify the bucket belongs to the current project:

```
gcloud storage buckets describe gs://{name} --format=json
```

Check that `project_number` in the response matches the current GCP project. If the bucket
belongs to another project, skip import and warn the user to choose a different bucket name
(re-run `voipbin-install init --reconfigure`).

---

## GCP Existence Check

Each registry entry stores a `gcloud` command that exits 0 if the resource exists.

Exit code handling:
- `0` → resource exists in GCP
- `2` → resource not found — skip silently
- `1` (or other) → permission error or API unavailable — skip and warn the user

---

## State-First Check

Before querying GCP, run `terraform state list` to get all resources currently in state.
Any registry entry whose `tf_address` appears in the state list is skipped — no double-import.

---

## User Interaction

When conflicts are detected:

```
  ⚠  12 resources exist in GCP but are missing from Terraform state

   #   Terraform Address                              Description
  ─────────────────────────────────────────────────────────────────────
   1   google_service_account.sa_cloudsql_proxy       Cloud SQL Proxy SA
   2   google_service_account.sa_gke_nodes            GKE Node Pool SA
   3   google_kms_key_ring.voipbin_sops               KMS Key Ring
   4   google_kms_crypto_key.voipbin_sops_key         KMS Crypto Key
   5   google_compute_network.voipbin                 VPC Network
   6   google_compute_address.nat_ip                  NAT Static IP
   7   google_compute_address.kamailio_lb_external    Kamailio LB IP (ext)
   8   google_compute_health_check.kamailio_internal  Health Check (int)
   9   google_sql_database_instance.voipbin           Cloud SQL Instance
  10   google_storage_bucket.terraform_state          TF State Bucket
  11   google_container_cluster.voipbin               GKE Cluster
  12   google_container_node_pool.voipbin             GKE Node Pool

  Import all into Terraform state and continue? [y/n] (y):
```

On confirmation, import each in order with a live progress line:

```
  ↺ Importing google_service_account.sa_cloudsql_proxy...  ✓
  ↺ Importing google_kms_key_ring.voipbin_sops...          ✓
  ↺ Importing google_compute_network.voipbin...            ✗  <error message>

  Summary: 10 imported, 1 failed, 1 skipped (already in state)

  ✗ Import failed for:
      google_compute_network.voipbin
      Run manually: terraform import -var project_id=<id> \
        google_compute_network.voipbin projects/<id>/global/networks/voipbin-vpc

  Pipeline halted. Fix the above and re-run: voipbin-install apply
```

On rejection: `Pipeline halted. Re-run voipbin-install apply after resolving conflicts manually.`

---

## Import Mechanics

```python
def run_state_list(tf_dir: Path) -> set[str]:
    result = subprocess.run(
        ["terraform", "state", "list"], cwd=tf_dir,
        capture_output=True, text=True,
    )
    return set(result.stdout.splitlines()) if result.returncode == 0 else set()


def check_exists_in_gcp(check_cmd: list[str]) -> tuple[bool, bool]:
    """Returns (exists, check_succeeded)."""
    result = subprocess.run(check_cmd, capture_output=True)
    if result.returncode == 0:
        return True, True
    if result.returncode == 2:
        return False, True   # not found
    return False, False      # permission error — couldn't verify


def import_resource(
    tf_dir: Path, address: str, import_id: str, project_id: str
) -> tuple[bool, str]:
    result = subprocess.run(
        ["terraform", "import", "-no-color",
         "-var", f"project_id={project_id}",
         address, import_id],
        cwd=tf_dir, capture_output=True, text=True,
    )
    return result.returncode == 0, result.stderr.strip()
```

The `-var project_id=...` flag suppresses the interactive `var.project_id: Enter a value:`
prompt that appears in the Terraform output without it.

**Partial failure behaviour:** All imports are attempted even if some fail. After completion,
a summary table shows successes, failures, and skips. If any import failed, the pipeline
halts and prints the exact `terraform import` commands the user can run manually.

---

## Files Changed

| File | Change |
|---|---|
| `scripts/terraform_reconcile.py` | New — reconcile logic and resource registry |
| `scripts/pipeline.py` | Insert `terraform_reconcile` stage between init and apply |
| `tests/test_terraform_reconcile.py` | New — unit tests for registry, state check, GCP check |
