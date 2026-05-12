"""State bucket bootstrap helper.

Owns the naming convention for the Terraform remote-state GCS bucket and the
idempotent create-or-skip logic invoked at the top of ``terraform_init``.

This module exists to break a chicken-and-egg: the state bucket is declared
as a Terraform resource (``google_storage_bucket.terraform_state`` in
``terraform/storage.tf``), but ``terraform init`` needs the bucket to already
exist in order to configure the remote-state backend.

The bucket is created with flags that mirror the Terraform resource as
closely as ``gcloud storage`` allows so the subsequent reconcile import +
first ``terraform apply`` are drift-minimal.
"""

from scripts.config import InstallerConfig
from scripts.display import (
    print_error,
    print_step,
    print_success,
    print_warning,
)
from scripts.utils import run_cmd


DEFAULT_ENV = "voipbin"

_RACE_MARKERS = ("409", "alreadyownedbyyou", "already exists")
_IAM_MARKERS = ("403", "does not have", "permission_denied")
_IAM_HINT = (
    "Grant 'roles/storage.admin' to the active ADC principal: gcloud auth list"
)


def _has(stderr: str, markers) -> bool:
    s = (stderr or "").lower()
    return any(m in s for m in markers)


def state_bucket_name(config: InstallerConfig) -> str:
    """Return the canonical state-bucket name for *config*.

    Format: ``{project_id}-{env}-tf-state``. ``env`` falls back to
    ``DEFAULT_ENV`` ("voipbin") when absent, matching the default of
    ``terraform/variables.tf::env``.
    """
    project_id = config.get("gcp_project_id", "") or ""
    env = config.get("env", DEFAULT_ENV) or DEFAULT_ENV
    return f"{project_id}-{env}-tf-state"


def ensure_state_bucket(config: InstallerConfig) -> bool:
    """Create the Terraform state bucket if it does not yet exist.

    Idempotent: ``gcloud storage buckets describe`` exit code 0 means the
    bucket exists and we skip the create. Versioning is then applied
    unconditionally (the update is a no-op when already enabled) so a
    partial first run cannot leave the bucket without versioning.

    Race-safe: if a parallel invocation wins the create, the loser will see
    HTTP 409 / ``AlreadyOwnedByYou`` / ``already exists``; we re-describe
    and continue on confirmation.

    On IAM permission failures (403 / PERMISSION_DENIED), prints a hint
    pointing the operator at ``roles/storage.admin``.

    Returns True on success or when the bucket already exists, False
    otherwise.
    """
    project_id = config.get("gcp_project_id", "") or ""
    region = config.get("region", "") or ""
    bucket = state_bucket_name(config)
    bucket_uri = f"gs://{bucket}"

    describe = run_cmd(
        ["gcloud", "storage", "buckets", "describe", bucket_uri,
         f"--project={project_id}"],
        capture=True,
        timeout=60,
    )
    bucket_exists = describe.returncode == 0
    if bucket_exists:
        print_success(f"State bucket already exists: {bucket_uri}")
    else:
        if _has(describe.stderr, _IAM_MARKERS) and not _has(
            describe.stderr, ("not found", "404")
        ):
            # Surface IAM errors observed during describe early.
            print_warning(_IAM_HINT)

        print_step(f"Creating Terraform state bucket: {bucket_uri}")
        create = run_cmd(
            ["gcloud", "storage", "buckets", "create", bucket_uri,
             f"--project={project_id}",
             f"--location={region}",
             "--uniform-bucket-level-access",
             "--public-access-prevention"],
            capture=True,
            timeout=120,
        )
        if create.returncode != 0:
            if _has(create.stderr, _RACE_MARKERS):
                # TOCTOU: another apply created it between describe and create.
                recheck = run_cmd(
                    ["gcloud", "storage", "buckets", "describe", bucket_uri,
                     f"--project={project_id}"],
                    capture=True,
                    timeout=60,
                )
                if recheck.returncode == 0:
                    print_warning(
                        f"State bucket create raced with another apply; "
                        f"existing bucket confirmed: {bucket_uri}"
                    )
                else:
                    print_error(
                        f"Failed to create state bucket {bucket_uri}:\n"
                        f"{create.stderr}"
                    )
                    return False
            else:
                print_error(
                    f"Failed to create state bucket {bucket_uri}:\n"
                    f"{create.stderr}"
                )
                if _has(create.stderr, _IAM_MARKERS):
                    print_error(_IAM_HINT)
                return False

    # Unconditional, idempotent versioning enable — guarantees the
    # contract regardless of whether we just created or found the bucket.
    versioning = run_cmd(
        ["gcloud", "storage", "buckets", "update", bucket_uri,
         f"--project={project_id}",
         "--versioning"],
        capture=True,
        timeout=60,
    )
    if versioning.returncode != 0:
        print_error(
            f"Failed to enable versioning on {bucket_uri}:\n{versioning.stderr}"
        )
        if _has(versioning.stderr, _IAM_MARKERS):
            print_error(_IAM_HINT)
        return False

    print_success(f"State bucket ready: {bucket_uri}")
    return True
