"""Terraform state reconciliation for VoIPBin installer.

Detects GCP resources that exist outside Terraform state and imports them
before terraform apply runs, making deployments resumable without 409 errors.
"""

from pathlib import Path
from typing import Any

from scripts.config import InstallerConfig
from scripts.terraform import TERRAFORM_DIR
from scripts.utils import _validate_cmd_arg, run_cmd


# Covers "NOT FOUND", "NOT_FOUND" (gcloud style), "notfound", "404 Not Found", etc.
_NOT_FOUND_PHRASES = ("not found", "notfound", "not_found", "does not exist", "404", "no such")


def check_exists_in_gcp(check_cmd: list[str]) -> tuple[bool, bool]:
    """Check whether a GCP resource exists.

    Returns:
        (exists, check_succeeded): exists=True if the resource is present.
        check_succeeded=False means the check could not be completed (e.g.
        permission error) — callers should warn but not block.
    """
    result = run_cmd(check_cmd, capture=True, timeout=30)
    if result.returncode == 0:
        return True, True
    stderr_lower = (result.stderr or "").lower()
    if any(phrase in stderr_lower for phrase in _NOT_FOUND_PHRASES):
        return False, True
    return False, False


def import_resource(
    tf_address: str,
    import_id: str,
    project_id: str,
) -> tuple[bool, str]:
    """Run `terraform import` for a single resource.

    Returns:
        (success, error_message): error_message is empty on success.
    """
    _validate_cmd_arg(project_id, "project_id")
    _validate_cmd_arg(tf_address, "tf_address")
    _validate_cmd_arg(import_id, "import_id")
    cmd = [
        "terraform", "import", "-no-color",
        "-var", f"project_id={project_id}",
        tf_address,
        import_id,
    ]
    result = run_cmd(cmd, capture=True, timeout=120, cwd=TERRAFORM_DIR)
    return result.returncode == 0, (result.stderr or "").strip()


def build_registry(config: InstallerConfig) -> list[dict[str, Any]]:
    """Build the list of GCP resources to check and import if needed.

    Entries are ordered by dependency (key ring before crypto key, etc.).
    Each entry has:
        tf_address   — Terraform resource address
        description  — human-readable name for display
        gcloud_check — gcloud command list; exit 0 = exists
        import_id    — ID string passed to `terraform import`
    """
    project = config.get("gcp_project_id")
    region = config.get("region")
    zone = config.get("zone")
    _kamailio_count = config.get("kamailio_count", 1)
    _rtpengine_count = config.get("rtpengine_count", 1)

    entries: list[dict[str, Any]] = []

    # -- Service accounts ------------------------------------------------
    sa_specs = [
        ("sa_cloudsql_proxy", "sa-voipbin-cloudsql-proxy", "Cloud SQL Proxy SA"),
        ("sa_gke_nodes",      "sa-voipbin-gke-nodes",      "GKE Node Pool SA"),
        ("sa_kamailio",       "sa-voipbin-kamailio",        "Kamailio VM SA"),
        ("sa_rtpengine",      "sa-voipbin-rtpengine",       "RTPEngine VM SA"),
    ]
    for tf_name, sa_id, desc in sa_specs:
        email = f"{sa_id}@{project}.iam.gserviceaccount.com"
        entries.append({
            "tf_address":   f"google_service_account.{tf_name}",
            "description":  desc,
            "gcloud_check": ["gcloud", "iam", "service-accounts", "describe", email, f"--project={project}"],
            "import_id":    f"projects/{project}/serviceAccounts/{email}",
        })

    # -- KMS (key ring must come before crypto key) ----------------------
    entries.append({
        "tf_address":   "google_kms_key_ring.voipbin_sops",
        "description":  "KMS Key Ring",
        "gcloud_check": ["gcloud", "kms", "keyrings", "describe", "voipbin-sops",
                         "--location=global", f"--project={project}"],
        "import_id":    f"projects/{project}/locations/global/keyRings/voipbin-sops",
    })
    entries.append({
        "tf_address":   "google_kms_crypto_key.voipbin_sops_key",
        "description":  "KMS Crypto Key",
        "gcloud_check": ["gcloud", "kms", "keys", "describe", "voipbin-sops-key",
                         "--keyring=voipbin-sops", "--location=global", f"--project={project}"],
        "import_id":    f"projects/{project}/locations/global/keyRings/voipbin-sops/cryptoKeys/voipbin-sops-key",
    })

    return entries
