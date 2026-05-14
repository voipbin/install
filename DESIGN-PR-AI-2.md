# DESIGN-PR-AI-2: Robust terraform destroy for wipeout+reapply

## Problem

`voipbin-install destroy --auto-approve` followed by a fresh apply fails
repeatedly due to two distinct terraform state management bugs. Both were
observed in dogfood (2026-05-14, 2nd wipeout run).

---

### Bug 1: State bucket destroyed mid-run → errored.tfstate / state lock failure

`terraform destroy` processes resources in dependency order. The GCS state
bucket (`google_storage_bucket.terraform_state`) is destroyed during the run.
Once the bucket is gone, terraform cannot write the updated state back:

```
Error: Failed to save state
Error saving state: Failed to upload state to
  gs://voipbin-install-dev-voipbin-tf-state/...: 404 Not Found
```

This triggers two compounding failures:
1. Terraform cannot write updated state → `errored.tfstate` is written locally
2. Terraform cannot release the GCS state lock → all subsequent terraform
   invocations fail with a "locked" error until manually unlocked

Note: `google_storage_bucket.terraform_state` has `force_destroy = false` in
terraform config, so even if the order were different, terraform destroy would
fail on a non-empty bucket. The state-write failure is the primary symptom.

**Fix:** Before running `terraform destroy`, detach `google_storage_bucket.terraform_state`
and the KMS keyring/key from terraform state via `terraform state rm`. These
resources are preserved in GCP; the GCS backend remains functional throughout
the entire destroy run. KMS resources must also be detached because they have
`lifecycle { prevent_destroy = true }` which would cause terraform destroy to
hard-error on them.

---

### Bug 2: GKE residual IG manager blocks subsequent apply

After destroy, GKE node pool instance group managers may linger in GCP with
a stale hash ID (e.g. `4805a830`). On the next `terraform apply`, terraform
re-imports the GKE cluster and node pool, then tries to update the node pool
in-place — hitting a GCP 404 because the IG manager no longer exists:

```
Error: googleapi: Error 404: The resource
  '.../instanceGroupManagers/gke-...-4805a830-grp' was not found
```

**Fix:** In `destroy_pipeline`, before calling `terraform_destroy`, also detach
`google_container_node_pool.voipbin` and `google_compute_instance_group.kamailio`
from state. This is safe because:
- The GKE cluster stays in state; terraform destroys it cleanly; GCP cascades
  the node pool deletion automatically.
- `google_compute_instance_group.kamailio` is covered by `reconcile_imports`
  (line 453 of `terraform_reconcile.py`): on the next apply, the import step
  detects it is absent from state and re-imports the existing GCP resource,
  avoiding a 409 conflict.
- Without these in state, terraform will not try to update them in-place during
  the destroy pass, avoiding the stale IG manager reference.

---

## Changes

### `scripts/terraform.py`

Add a new function `terraform_state_rm(resources: list[str]) -> bool`.

Implementation details:
- Runs `terraform -chdir=TERRAFORM_DIR state rm` **per-resource** (one call
  per address), not in bulk, so a missing resource is isolated from others.
- For each resource: if exit code is 0 → success. If exit code is non-zero
  and stderr/stdout contains `"No matching objects found"` (exact terraform
  output for missing address, verified against terraform 1.x in voipbin-install-dev
  dogfood 2026-05-14) → treat as success (idempotent). Any other
  non-zero exit → return False for that resource.
- Must use `run_cmd(..., capture=True, ...)` — streaming (`capture=False`)
  suppresses stdout/stderr, making the idempotency check unreachable.
- Returns True if all resources were successfully detached or were absent.
  Returns False if any resource produced an unexpected error.
- Requires `terraform init` to have been run (backend must be configured).
  `destroy_pipeline` always runs after `terraform_init` stage, so this
  precondition is satisfied.

```python
def terraform_state_rm(resources: list[str]) -> bool:
    """Detach resources from terraform state before destroy.

    Runs per-resource so a missing address does not block others.
    Returns True if all resources were detached or were not in state.
    Returns False if any resource produced an unexpected error.
    """
    ...
```

### `scripts/pipeline.py`

Add module-level constant:

```python
# Resources detached from TF state before destroy to prevent:
# - GCS backend loss mid-destroy (state bucket, KMS)
# - Stale GKE IG manager ID blocking subsequent apply (node pool, kamailio IG)
# All entries must be bare resource addresses (no module prefix) matching
# the actual terraform state list output.
DESTROY_STATE_DETACH = [
    "google_kms_crypto_key.voipbin_sops_key",
    "google_kms_key_ring.voipbin_sops",
    "google_storage_bucket.terraform_state",
    "google_container_node_pool.voipbin",
    "google_compute_instance_group.kamailio",
]
```

Resource addresses verified against live `terraform state list` output from
dogfood environment (voipbin-install-dev, 2026-05-14). All five are top-level
resources (no module prefix).

Update `destroy_pipeline()`:

```python
def destroy_pipeline(config, auto_approve=False):
    ...
    # Step 0: Detach protect-and-survive resources from state
    print_header("Stage: Pre-destroy state detach")
    ok = terraform_state_rm(DESTROY_STATE_DETACH)
    if not ok:
        print_warning("Some resources could not be detached from state; proceeding anyway.")

    # Stage 1: K8s delete (existing)
    ...

    # Stage 2: Terraform destroy (existing)
    tf_ok = terraform_destroy(config, auto_approve=auto_approve)

    # Step 3: Clean up errored.tfstate if present (defensive)
    errored = TERRAFORM_DIR / "errored.tfstate"
    if errored.exists():
        errored.unlink()
        print_step("Removed errored.tfstate")

    if not tf_ok:
        ...
```

`TERRAFORM_DIR` is already defined in `terraform.py` as
`INSTALLER_DIR / "terraform"`. Import it in `pipeline.py` (it is already
imported for other usages). Use `pathlib.Path` for the errored.tfstate check
so the path is always resolved relative to `TERRAFORM_DIR`, not the CWD.

---

## Tests

Add to `tests/test_pipeline.py`:

- **T-AI-0:** Assert `DESTROY_STATE_DETACH` contains exactly the five expected
  resource addresses in order (guards against accidental list mutation).
- **T-AI-1:** `destroy_pipeline()` calls `terraform_state_rm(DESTROY_STATE_DETACH)`
  before `terraform_destroy` — verified via call-order assertion on mocks.
- **T-AI-2:** `destroy_pipeline()` calls `terraform_state_rm` then calls
  `terraform_destroy` even when `terraform_state_rm` returns False (warning
  logged, no abort). The overall return value is determined solely by
  `terraform_destroy`'s return value, not by `terraform_state_rm`'s.
- **T-AI-3:** `destroy_pipeline()` removes `TERRAFORM_DIR / "errored.tfstate"`
  after destroy — two sub-cases:
  - File exists → file is removed (both success and failure paths)
  - File does not exist → no FileNotFoundError raised
- **T-AI-3b:** On the failure path, `state["deployment_state"]` is still set
  to `"destroy_failed"` after errored.tfstate cleanup.

Add to `tests/test_terraform.py`:

- **T-AI-4:** `terraform_state_rm(["some.resource"])` returns True when
  `terraform state rm` exits 0.
- **T-AI-5:** `terraform_state_rm(["missing.resource"])` returns True (idempotent)
  when terraform exits non-zero with `"No matching objects found"` in output.
- **T-AI-6:** `terraform_state_rm(["bad.resource"])` returns False when terraform
  exits non-zero with an unrecognised error message.
- **T-AI-7:** `terraform_state_rm(["a.ok", "b.missing", "c.error"])` processes
  each resource independently; returns False only because of c.error.

---

## Risk

Low. The state detach is purely additive — it only removes entries from remote
state before the destroy run. GCP resources are unaffected.

**Stale GCS lock:** If a previous broken destroy (before this fix is deployed)
left a dangling GCS lock, `terraform state rm` will fail with a lock contention
error. In this case, run `terraform -chdir=terraform force-unlock <lock-id>` to
release it, then retry `voipbin-install destroy`. This is a pre-existing
operational concern outside the scope of this fix.

**`errored.tfstate` pre-existing from old runs:** The cleanup in `destroy_pipeline`
removes `errored.tfstate` if present. If an operator has manually edited
`errored.tfstate` to preserve partial state, this cleanup will discard their
edits. This is an unlikely edge case; operators who need to preserve
`errored.tfstate` should rename it before running destroy.

## Alternatives Rejected

- **`lifecycle { prevent_destroy = false }` on state bucket:** Requires
  modifying terraform resource definitions; does not solve the mid-run ordering
  problem.
- **Two-pass destroy:** Complex, fragile, not idiomatic terraform.
- **`terraform destroy -target` to exclude the bucket:** Does not compose with
  the automated pipeline.
