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
from scripts.display import print_error, print_step, print_success
from scripts.utils import run_cmd


DEFAULT_ENV = "voipbin"


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
    bucket exists and we return True without touching it.

    On create, mirrors ``google_storage_bucket.terraform_state`` in
    ``terraform/storage.tf``:

    - ``--uniform-bucket-level-access``
    - ``--public-access-prevention=enforced``
    - ``--location=<region>``
    - versioning enabled via a follow-up ``buckets update --versioning``

    The ``lifecycle_rule`` (delete after 5 newer versions) is intentionally
    not applied here; ``gcloud storage`` has no first-class flag for it and
    the first ``terraform apply`` reconciles that drift.

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
    if describe.returncode == 0:
        print_success(f"State bucket already exists: {bucket_uri}")
        return True

    print_step(f"Creating Terraform state bucket: {bucket_uri}")
    create = run_cmd(
        ["gcloud", "storage", "buckets", "create", bucket_uri,
         f"--project={project_id}",
         f"--location={region}",
         "--uniform-bucket-level-access",
         "--public-access-prevention=enforced"],
        capture=True,
        timeout=120,
    )
    if create.returncode != 0:
        print_error(
            f"Failed to create state bucket {bucket_uri}:\n{create.stderr}"
        )
        return False

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
        return False

    print_success(f"State bucket ready: {bucket_uri}")
    return True
