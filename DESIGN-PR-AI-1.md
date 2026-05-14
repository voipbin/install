# DESIGN-PR-AI-1: Robust terraform destroy for wipeout+reapply

## Problem

`voipbin-install destroy --auto-approve` followed by a fresh apply fails
repeatedly due to two distinct terraform state management bugs. Both were
observed in dogfood (2026-05-14, 2nd wipeout run).

---

### Bug 1: State bucket destroyed mid-run → errored.tfstate / state lock failure

`terraform destroy` processes resources in dependency order. The GCS state
bucket (`google_storage_bucket.terraform_state`) is destroyed before all
resources that reference it are cleaned up. Once the bucket is gone, terraform
cannot write the updated state back, producing:

```
Error: Failed to save state
Error saving state: Failed to upload state to
  gs://voipbin-install-dev-voipbin-tf-state/...: 404 Not Found
```

And then fails to release the state lock, leaving `errored.tfstate` on disk
and making all subsequent `terraform` invocations fail until manually resolved.

**Root cause:** `google_storage_bucket.terraform_state` has `force_destroy =
true` but no mechanism prevents terraform from destroying it before finishing
the state-write of the overall destroy operation.

**Fix:** Before running `terraform destroy`, detach the state bucket and the
KMS keyring/key from the terraform state (`terraform state rm`). These resources
are GCP-level infrastructure that survive the destroy (KMS keyring cannot be
deleted within 24h anyway; the state bucket can be cleaned up separately or
left for the next apply to adopt). Detaching them from state means terraform
will not try to destroy them, and the GCS backend remains available throughout
the entire destroy run.

This mirrors the manual workaround that has been applied in every dogfood run.

---

### Bug 2: GKE residual IG manager blocks subsequent apply

After destroy partially completes (or after the state bucket bug above forces
an aborted destroy), GKE node pool instance group managers may linger in GCP
with a stale hash ID (e.g. `4805a830`). On the next `terraform apply`,
terraform re-imports the GKE cluster but finds the old IG manager ID in the
imported node pool resource, then tries to update it in-place — hitting a GCP
404 because the IG manager no longer exists:

```
Error: googleapi: Error 404: The resource
  '.../instanceGroupManagers/gke-...-4805a830-grp' was not found
```

**Root cause:** terraform imports the GKE cluster and node pool from GCP, but
the node pool's internal IG manager reference is stale. terraform then tries
to reconcile by mutating the node pool in-place, which references the dead IG.

**Fix:** In `destroy_pipeline`, before calling `terraform_destroy`, also detach
`google_container_node_pool.voipbin` and `google_compute_instance_group.kamailio`
from state. This is safe because:
- The GKE cluster itself stays in state (terraform destroys it cleanly).
- The node pool is a child of the cluster; GCP destroys it automatically when
  the cluster is deleted.
- The Kamailio instance group is destroyed by the VM destroy dependency chain.
- Without these in state, terraform will not try to update them in-place during
  the destroy pass, avoiding the stale IG manager reference.

On the subsequent apply, `reconcile_imports` detects their absence and
re-imports them fresh with current GCP IDs.

---

## Changes

### `scripts/terraform.py`

Add a new function `terraform_state_rm(resources: list[str]) -> bool` that
runs `terraform state rm` for the given resource addresses. Returns True on
success or if a resource is not in state (idempotent).

### `scripts/pipeline.py` — `destroy_pipeline()`

Before calling `terraform_destroy`, call `terraform_state_rm` for:

```python
DESTROY_STATE_DETACH = [
    "google_kms_crypto_key.voipbin_sops_key",
    "google_kms_key_ring.voipbin_sops",
    "google_storage_bucket.terraform_state",
    "google_container_node_pool.voipbin",
    "google_compute_instance_group.kamailio",
]
```

Log which resources were detached. If `terraform_state_rm` fails for any
resource (other than "not in state"), log a warning and continue — the destroy
should still proceed; the user can retry.

The list is defined as a module-level constant in `pipeline.py` so tests can
inspect and override it.

### `scripts/pipeline.py` — `destroy_pipeline()` errored.tfstate cleanup

After `terraform_destroy` returns (success or failure), check for
`terraform/errored.tfstate` and remove it if present. On failure this prevents
the file from blocking subsequent runs. On success it is a no-op.

---

## Tests

Add to `tests/test_pipeline.py`:

- **T-AI-1:** `destroy_pipeline()` calls `terraform_state_rm` with
  `DESTROY_STATE_DETACH` before calling `terraform_destroy`.
- **T-AI-2:** `destroy_pipeline()` proceeds with `terraform_destroy` even when
  `terraform_state_rm` returns False for some resources (warning, not abort).
- **T-AI-3:** `destroy_pipeline()` removes `errored.tfstate` after destroy
  completes (both success and failure paths).

Add to `tests/test_terraform.py` (or equivalent):

- **T-AI-4:** `terraform_state_rm` returns True when `terraform state rm`
  succeeds.
- **T-AI-5:** `terraform_state_rm` returns True (idempotent) when resource is
  not in state (exit code 1 + "not in state" in output).
- **T-AI-6:** `terraform_state_rm` returns False on unexpected error.

---

## Risk

Low. The state detach is additive — it only removes entries from local state
before the destroy run. GCP resources are unaffected. If a resource is already
absent from state (e.g. from a previous partial run), the `state rm` is a no-op.
The errored.tfstate cleanup is purely file-system housekeeping.

The one edge case: if an operator has customised the KMS keyring or state bucket
names, the hardcoded resource addresses must match. Both are currently fixed
names in the terraform config (`voipbin_sops`, `terraform_state`), so this is
not a concern for the current codebase.

## Alternatives Rejected

- **`lifecycle { prevent_destroy = false }` on the state bucket:** Requires
  modifying terraform resource definitions; does not solve the ordering problem
  since terraform still processes the bucket in dependency order.
- **Two-pass destroy (resources first, then bucket):** Complex orchestration,
  fragile, not idiomatic terraform.
- **`terraform destroy -target` to exclude the bucket:** Does not compose well
  with the automated pipeline.
