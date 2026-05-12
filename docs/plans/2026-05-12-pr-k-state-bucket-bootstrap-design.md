# PR-K: state bucket bootstrap (GAP-32 hot-fix) — design

Date: 2026-05-12
Branch: NOJIRA-PR-K-state-bucket-bootstrap
Status: Design

## Problem

A fresh `voipbin-install apply` blocks at the `terraform_init` stage with an
interactive prompt:

```
Initializing the backend...
bucket
  The name of the Google Cloud Storage bucket
  Enter a value:
```

Three issues combine to produce this hang on a clean project:

1. `scripts/terraform.py::terraform_init` only passes
   `-backend-config=prefix=...` and omits the bucket entirely. Because
   `terraform/backend.tf` declares `backend "gcs" { prefix = "..." }` with no
   bucket value, this is a **partial backend config** and Terraform must
   receive the bucket via `-backend-config=bucket=...` at init time. Without
   it, Terraform falls back to a stdin prompt.

2. The state bucket itself does not yet exist on a first run. It is declared
   as a Terraform resource (`google_storage_bucket.terraform_state` in
   `terraform/storage.tf`) which can only be created **after** `init` + `plan`
   + `apply` — classic chicken-and-egg.

3. The naming convention for the state bucket is duplicated in three places
   with two different literal forms:
   - `terraform/storage.tf`: `${var.project_id}-${var.env}-tf-state`
   - `scripts/terraform_reconcile.py:348-352`: hardcoded
     `{project}-voipbin-tf-state`
   - `scripts/diagnosis.py:202`: hardcoded `{project_id}-voipbin-tf-state`

   These all resolve identically today because `var.env` defaults to
   `voipbin`, but the divergence is fragile: any change to env (or any future
   per-env install) silently desynchronises.

## Goals

- Make `voipbin-install apply` succeed end-to-end on a fresh, empty GCP
  project, without interactive prompts.
- Centralise state-bucket naming behind a single helper so the
  Terraform resource, the reconcile import registry, and the diagnosis stage
  hint all agree by construction.
- Stay drift-free: the bucket created by the bootstrap helper must match the
  `google_storage_bucket.terraform_state` resource closely enough that the
  subsequent reconcile import + first `terraform apply` are no-ops (or close
  to it) — small, recoverable drift (e.g. lifecycle rule) is acceptable.
- Idempotent re-runs: if the bucket already exists, the helper is a no-op.

## Non-goals

- Switching backend type. We keep GCS remote state.
- Removing the `google_storage_bucket.terraform_state` resource from
  `storage.tf`. We continue to import it post-bootstrap so Terraform manages
  it long-term.
- Splitting `APPLY_STAGES`. We deliberately keep bootstrap **inside**
  `terraform_init` to avoid roadmap churn.

## Design

### 1. New helper module `scripts/state_bucket.py`

Single source of truth for state-bucket naming and creation.

```python
# scripts/state_bucket.py
"""State bucket bootstrap helper.

Owns the naming convention for the Terraform remote-state GCS bucket and the
idempotent create-or-skip logic invoked at the top of `terraform_init`.
"""

from scripts.config import InstallerConfig
from scripts.display import print_error, print_step, print_success
from scripts.utils import run_cmd


DEFAULT_ENV = "voipbin"


def state_bucket_name(config: InstallerConfig) -> str:
    """Return the canonical state-bucket name for *config*.

    Format: ``{project_id}-{env}-tf-state``. ``env`` falls back to
    ``DEFAULT_ENV`` ("voipbin") when absent, matching
    ``terraform/variables.tf::env``.
    """
    project_id = config.get("gcp_project_id", "")
    env = config.get("env", DEFAULT_ENV) or DEFAULT_ENV
    return f"{project_id}-{env}-tf-state"


def ensure_state_bucket(config: InstallerConfig) -> bool:
    """Create the Terraform state bucket if it does not yet exist.

    Idempotent: ``gcloud storage buckets describe`` exit code 0 means the
    bucket exists and we return True without touching it.

    On create, mirrors the flags of ``google_storage_bucket.terraform_state``
    in ``terraform/storage.tf`` so the subsequent reconcile import is
    drift-minimal:

    - ``--uniform-bucket-level-access``
    - ``--public-access-prevention=enforced``
    - ``--location=<region>``
    - versioning enabled via a follow-up ``buckets update --versioning``

    The ``lifecycle_rule`` (delete after 5 newer versions) is intentionally
    **not** applied here; ``gcloud storage`` has no first-class flag for it
    and the first ``terraform apply`` will reconcile this small drift.

    Returns True on success or when bucket already exists, False otherwise.
    """
```

The helper is **not** named `_ensure_state_bucket` (no leading underscore) so
it can be imported and unit-tested directly.

### 2. `scripts/terraform.py::terraform_init` changes

```python
def terraform_init(config: InstallerConfig) -> bool:
    write_tfvars(config)
    if not ensure_state_bucket(config):
        return False
    bucket = state_bucket_name(config)
    cmd = [
        "terraform", f"-chdir={TERRAFORM_DIR}", "init",
        f"-backend-config=bucket={bucket}",
        f"-backend-config=prefix=voipbin/{config.get('gcp_project_id', '')}",
    ]
    ...
```

Two changes:
- Call `ensure_state_bucket(config)` first; return False if bootstrap failed.
- Add `-backend-config=bucket=...` alongside the existing prefix.

The existing prefix value `voipbin/{project_id}` is preserved unchanged
(deliberately divergent from `backend.tf`'s placeholder `terraform/state` —
the CLI override wins and was the working value pre-fix).

### 3. `scripts/terraform_reconcile.py` change

Replace the hardcoded literal `f"{project}-voipbin-tf-state"` (×2) with
`state_bucket_name(config)`. Same import, same check_cmd shape; only the
literal moves behind the helper.

### 4. `scripts/diagnosis.py` change

Replace `f"gs://{project_id}-voipbin-tf-state"` with
`f"gs://{state_bucket_name(config)}"`.

### 5. No Terraform file changes

`terraform/storage.tf` already produces the same name. `terraform/backend.tf`
already declares a partial backend; no change needed there.

## Behaviour matrix

| Scenario                                | Pre-PR-K              | Post-PR-K                                  |
| --------------------------------------- | --------------------- | ------------------------------------------ |
| Fresh project, no state bucket          | Hangs at stdin prompt | Helper creates bucket, init succeeds       |
| State bucket already exists, no state   | Hangs at stdin prompt | Helper skips create, init succeeds         |
| State bucket exists, state populated    | Hangs at stdin prompt | Helper skips, init re-uses remote state    |
| Custom env (e.g. `env=staging`)         | Reconcile name drifts | Helper returns `<proj>-staging-tf-state`   |
| ADC not configured / no GCS permission  | Bucket create fails   | Helper returns False, init returns False   |

## Test plan (`tests/test_pr_k_state_bucket_bootstrap.py`)

1. `state_bucket_name` returns `{proj}-voipbin-tf-state` when env unset.
2. `state_bucket_name` returns `{proj}-{env}-tf-state` for explicit env.
3. `ensure_state_bucket` idempotent: when describe returns 0, no create
   subprocess call is issued.
4. `ensure_state_bucket` creates bucket with the right flags
   (`--uniform-bucket-level-access`, `--public-access-prevention=enforced`,
   `--location=<region>`, `--project=<project>`) when describe fails.
5. `ensure_state_bucket` follows up with `buckets update --versioning` to
   enable versioning.
6. `ensure_state_bucket` returns False when create fails.
7. `terraform_init` issues both `-backend-config=bucket=...` and
   `-backend-config=prefix=...`.
8. `terraform_init` returns False when `ensure_state_bucket` returns False.

Existing test impact: `tests/test_terraform.py` does not pin the init command
shape (only checks `write_tfvars` and `terraform_state_list`), and
`tests/test_diagnosis.py` only asserts on `billing`/`ansible`/`labels.env`
substrings — neither requires update. The new diagnosis stage hint still
mentions the bucket gs:// URL so any future assertions remain valid.

## Risks & mitigations

- **Risk**: `gcloud storage buckets describe` requires `storage.buckets.get`
  permission; absent perm could be misread as "missing" → spurious create.
  **Mitigation**: rely on returncode==0 for existence; non-zero falls through
  to create, which itself fails loud with the permission error. Net result:
  the user sees a clear gcloud error rather than a silent skip.

- **Risk**: drift between gcloud-created bucket and terraform resource (e.g.
  lifecycle rule, labels).
  **Mitigation**: documented as acceptable; first `terraform apply` after
  import reconciles. Spelled out in helper docstring.

- **Risk**: legacy installs that pre-date PR-K already created the bucket via
  some other path with `voipbin` literal env — these continue to work because
  the default `env` resolution still yields the same name.

## Design review log

Iteration 1 — independent review

Findings:
- Q: should `ensure_state_bucket` use `gcloud storage ls` (as diagnosis.py
  does) or `buckets describe`? Decision: use `buckets describe` — `ls` on a
  non-existent bucket can return rc=0 with empty stdout in some setups when
  the project has soft-deleted versions; `describe` is unambiguous.
  (diagnosis.py is left on `ls` for now; the inconsistency is intentional
  and out-of-scope — it only generates a hint, not a control-flow decision.)
- Q: do we need to pass `--project` to `buckets describe`? Bucket names are
  globally unique so it is not strictly required, but `--project` ensures the
  bucket lives in the user's project and not a name collision elsewhere.
  Decision: pass `--project` everywhere for consistency.
- Q: should the helper live in `scripts/terraform.py` to avoid one more
  module? Decision: separate module — `terraform.py` already imports
  `config`, `display`, `utils`; adding bucket-naming makes circular imports
  more likely once `terraform_reconcile.py` (which already imports
  `terraform`) needs the helper. Clean separation wins.

Iteration 2 — independent review

Findings:
- Edge: empty `gcp_project_id` would produce a bucket name `-voipbin-tf-state`
  which is invalid. Mitigation: `terraform_init` is gated behind the wizard /
  config schema requirement that `gcp_project_id` is non-empty; the helper
  inherits that invariant. No extra guard needed — failing fast with a
  gcloud "invalid bucket name" error is acceptable for this corner.
- Edge: `region` may contain dashes (`us-central1`) which are safe for
  `_validate_cmd_arg`. ✓
- Edge: `state_bucket_name` returning `""` when both fields absent — covered
  by config schema; helper does not need its own validation layer.
- Verified: no other module reaches the literal `tf-state` form. Greps:
  `*.py` files outside the three touched modules → 0 hits.

Both review rounds resolved. Proceed to implementation.
