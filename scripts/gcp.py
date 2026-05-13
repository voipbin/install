"""GCP operations: quota checks, API enablement, service account creation."""

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Literal, Optional

import yaml

from scripts.display import (
    console,
    create_progress,
    print_error,
    print_header,
    print_success,
    print_warning,
)
from scripts.utils import INSTALLER_DIR, _validate_cmd_arg, run_cmd, run_cmd_with_retry


@dataclass
class QuotaResult:
    metric: str
    available: float
    required: float
    ok: bool
    description: str


def _load_yaml_data(filename: str) -> dict:
    path = INSTALLER_DIR / "config" / filename
    with open(path) as f:
        return yaml.safe_load(f)


def get_project_id() -> Optional[str]:
    """Auto-detect GCP project from gcloud config."""
    result = run_cmd(
        ["gcloud", "config", "get-value", "project"],
        timeout=10,
    )
    val = result.stdout.strip()
    if result.returncode == 0 and val and val != "(unset)":
        return val
    return None


@dataclass
class ProjectListing:
    """A GCP project surfaced for picker selection (PR-V).

    billing_enabled is tri-state:
      True   — confirmed billing-enabled (safe to deploy)
      False  — confirmed billing-disabled (terraform_apply will fail)
      None   — could not determine (permission denied, no billing accounts
               visible, gcloud beta component missing, etc.)
    """
    project_id: str
    name: str
    billing_enabled: Optional[bool]


def _fetch_billing_map() -> dict[str, bool]:
    """Build {projectId: billingEnabled} by iterating billing accounts.

    Returns an empty dict on any failure (no billing accounts visible,
    permission denied, gcloud beta component missing, JSON parse fail).
    Callers treat absence in the map as `billing_enabled=None`.
    """
    accounts_result = run_cmd(
        ["gcloud", "billing", "accounts", "list",
         "--format=json", "--filter=open=true"],
        timeout=15,
    )
    if accounts_result.returncode != 0:
        return {}
    try:
        accounts = json.loads(accounts_result.stdout or "[]")
    except ValueError:
        return {}
    if not isinstance(accounts, list):
        return {}

    billing_map: dict[str, bool] = {}
    for account in accounts:
        # account["name"] shape: "billingAccounts/<id>". The --billing-account
        # flag wants just the <id> portion.
        full_name = str(account.get("name", ""))
        if not full_name.startswith("billingAccounts/"):
            continue
        account_id = full_name[len("billingAccounts/"):]
        if not account_id:
            continue

        projects_result = run_cmd(
            ["gcloud", "beta", "billing", "projects", "list",
             f"--billing-account={account_id}", "--format=json"],
            timeout=20,
        )
        if projects_result.returncode != 0:
            continue  # Skip this account; other accounts may still succeed
        try:
            entries = json.loads(projects_result.stdout or "[]")
        except ValueError:
            continue
        if not isinstance(entries, list):
            continue
        for entry in entries:
            # entry shape: {"name": "projects/<id>/billingInfo",
            #               "projectId": "<id>",
            #               "billingAccountName": "billingAccounts/<id>",
            #               "billingEnabled": true}
            pid = str(entry.get("projectId", ""))
            if pid:
                billing_map[pid] = bool(entry.get("billingEnabled", False))
    return billing_map


def _resolve_project_display_name(p: dict) -> str:
    """Pick the human display name for a project entry, schema-tolerant.

    gcloud projects list v1 (current default in gcloud 472+): the `name`
    field holds the human display name (e.g., "VoIPBin Dev").
    Cloud Resource Manager v3: `name` holds the resource path
    ("projects/<number>") and `displayName` holds the human label.

    Skip `name` when it has the v3 resource-path shape; otherwise use it.
    Fall back to `displayName` if neither yields a usable string.
    """
    nm = p.get("name") or ""
    dn = p.get("displayName") or ""
    if nm.startswith("projects/"):
        return dn or ""
    return nm or dn or ""


def list_active_projects() -> list[ProjectListing]:
    """Return the ACTIVE GCP projects the operator can see, with billing.

    Implementation:
      1. ``gcloud projects list --filter=lifecycleState:ACTIVE --format=json``
         enumerates ACTIVE projects. Empty list on any failure.
      2. :func:`_fetch_billing_map` builds a {projectId: billingEnabled} map
         by iterating visible OPEN billing accounts. Each unmapped project
         gets ``billing_enabled=None`` (rendered as "unknown" in the picker).
      3. Sort case-insensitively by project_id.

    Never raises. Returns [] on any gcloud failure so the caller can fall
    back to the text-prompt flow gracefully.
    """
    list_result = run_cmd(
        ["gcloud", "projects", "list",
         "--filter=lifecycleState:ACTIVE",
         "--format=json"],
        timeout=20,
    )
    if list_result.returncode != 0:
        return []
    try:
        raw = json.loads(list_result.stdout or "[]")
    except ValueError:
        return []
    if not isinstance(raw, list):
        return []

    listings = [
        ProjectListing(
            project_id=str(p.get("projectId", "")),
            name=_resolve_project_display_name(p),
            billing_enabled=None,
        )
        for p in raw
        if p.get("projectId")  # Filter empty projectIds defensively
    ]

    billing_map = _fetch_billing_map()
    for listing in listings:
        if listing.project_id in billing_map:
            listing.billing_enabled = billing_map[listing.project_id]

    listings.sort(key=lambda lp: lp.project_id.lower())
    return listings


def get_account_email() -> Optional[str]:
    """Get active gcloud account email."""
    result = run_cmd(
        ["gcloud", "auth", "list", "--filter=status:ACTIVE", "--format=value(account)"],
        timeout=10,
    )
    if result.returncode == 0 and result.stdout.strip():
        return result.stdout.strip().splitlines()[0]
    return None


def check_quotas(project_id: str, region: str) -> list[QuotaResult]:
    """Check GCP quota availability for the selected region."""
    _validate_cmd_arg(project_id, "project_id")
    _validate_cmd_arg(region, "region")
    quota_defs = _load_yaml_data("gcp_quotas.yaml")["quotas"]

    result = run_cmd(
        ["gcloud", "compute", "regions", "describe", region,
         "--project", project_id, "--format", "json"],
        timeout=30,
    )
    if result.returncode != 0:
        return [
            QuotaResult(
                metric=q["metric"],
                available=0,
                required=q["minimum"],
                ok=False,
                description=q["description"],
            )
            for q in quota_defs
        ]

    try:
        region_data = json.loads(result.stdout)
    except json.JSONDecodeError:
        return []

    quota_map: dict[str, float] = {}
    for q in region_data.get("quotas", []):
        metric = q.get("metric", "")
        limit = q.get("limit", 0)
        usage = q.get("usage", 0)
        quota_map[metric] = limit - usage

    results: list[QuotaResult] = []
    for q in quota_defs:
        available = quota_map.get(q["metric"], 0)
        results.append(QuotaResult(
            metric=q["metric"],
            available=available,
            required=q["minimum"],
            ok=available >= q["minimum"],
            description=q["description"],
        ))
    return results


def display_quota_results(results: list[QuotaResult], project_id: str) -> bool:
    """Display quota check results. Returns True if all sufficient."""
    print_header("Validating GCP quotas...")
    all_ok = True
    for q in results:
        if q.ok:
            print_success(f"{q.metric}: {q.available:.0f} available ({q.required:.0f} required)")
        else:
            all_ok = False
            print_warning(
                f"{q.metric}: {q.available:.0f} available ({q.required:.0f} required) — INSUFFICIENT"
            )
            print_warning(
                f"  Request increase: https://console.cloud.google.com/iam-admin/quotas?project={project_id}"
            )
    return all_ok


def enable_apis(
    project_id: str,
    progress_callback: Optional[Callable[[str], None]] = None,
) -> tuple[list[str], list[str]]:
    """Enable required GCP APIs. Returns (succeeded, failed) lists."""
    _validate_cmd_arg(project_id, "project_id")
    api_data = _load_yaml_data("gcp_apis.yaml")
    apis = api_data["apis"]
    succeeded: list[str] = []
    failed: list[str] = []

    for api in apis:
        if progress_callback:
            progress_callback(api)
        result = run_cmd_with_retry(
            ["gcloud", "services", "enable", api, "--project", project_id],
            retries=3,
            delay=10.0,
            timeout=120,
        )
        if result.returncode == 0:
            succeeded.append(api)
        else:
            failed.append(api)

    return succeeded, failed


def create_service_account(
    project_id: str,
    sa_name: str = "voipbin-installer",
    display_name: str = "VoIPBin Installer Service Account",
) -> Optional[str]:
    """Create installer service account and bind IAM roles. Returns SA email."""
    _validate_cmd_arg(project_id, "project_id")
    _validate_cmd_arg(sa_name, "sa_name")
    sa_email = f"{sa_name}@{project_id}.iam.gserviceaccount.com"

    # Create SA (idempotent — ignore already-exists error, surface real errors)
    sa_result = run_cmd(
        ["gcloud", "iam", "service-accounts", "create", sa_name,
         f"--display-name={display_name}",
         f"--project={project_id}"],
        timeout=30,
    )
    if sa_result.returncode != 0:
        stderr_lower = sa_result.stderr.lower()
        if "already exists" not in stderr_lower:
            print_warning(f"Service account creation error: {sa_result.stderr.strip()}")
            print_warning("Continuing with role binding using pre-computed SA email.")

    # Bind roles
    roles_data = _load_yaml_data("gcp_iam_roles.yaml")
    for role in roles_data["roles"]:
        run_cmd_with_retry(
            ["gcloud", "projects", "add-iam-policy-binding", project_id,
             f"--member=serviceAccount:{sa_email}",
             f"--role={role}",
             "--condition=None",
             "--quiet"],
            retries=2,
            delay=5.0,
            timeout=30,
        )

    return sa_email


# Deliberate subset of gcp_apis.yaml: only the 3 APIs whose absence will
# cause Terraform to fail immediately. Full API enablement happens during init.
REQUIRED_APIS = [
    "compute.googleapis.com",
    "container.googleapis.com",
    "sqladmin.googleapis.com",
]


def check_billing_tristate(project_id: str) -> Literal["enabled", "disabled", "unknown"]:
    """Three-way billing check distinguishing disabled from probe failure.

    Returns "unknown" when the gcloud command fails (auth error, network, etc.)
    so callers skip the billing hint rather than showing a false positive.
    """
    result = run_cmd([
        "gcloud", "billing", "projects", "describe", project_id,
        "--format=value(billingEnabled)",
    ])
    if result.returncode != 0:
        return "unknown"
    value = result.stdout.strip().lower()
    if value == "true":
        return "enabled"
    if value == "false":
        return "disabled"
    return "unknown"


def check_required_apis(project_id: str) -> list[str]:
    """Return list of REQUIRED_APIS not yet enabled in the project.

    Returns an empty list if all required APIs are enabled or if the probe
    fails — an empty result on failure avoids false 'enable API' hints.
    """
    result = run_cmd([
        "gcloud", "services", "list",
        "--enabled",
        "--project", project_id,
        "--format=value(config.name)",
    ])
    if result.returncode != 0:
        return []
    enabled = set(result.stdout.splitlines())
    return [api for api in REQUIRED_APIS if api not in enabled]


def create_kms_keyring(
    project_id: str,
    keyring_name: str = "voipbin-sops",
    key_name: str = "voipbin-sops-key",
    location: str = "global",
) -> Optional[str]:
    """Create KMS key ring and crypto key. Returns key resource ID."""
    _validate_cmd_arg(project_id, "project_id")
    _validate_cmd_arg(keyring_name, "keyring_name")
    _validate_cmd_arg(key_name, "key_name")
    _validate_cmd_arg(location, "location")

    # Create keyring (idempotent)
    run_cmd(
        ["gcloud", "kms", "keyrings", "create", keyring_name,
         f"--location={location}", f"--project={project_id}"],
        timeout=30,
    )

    # Create crypto key with 90-day rotation (idempotent)
    run_cmd(
        ["gcloud", "kms", "keys", "create", key_name,
         f"--keyring={keyring_name}",
         f"--location={location}",
         "--purpose=encryption",
         "--rotation-period=7776000s",
         f"--project={project_id}"],
        timeout=30,
    )

    # Grant the current gcloud user encrypt/decrypt on this key so SOPS
    # (which uses Application Default Credentials) can encrypt secrets.yaml.
    # Scoped to the key, not the project, for least privilege.
    account_result = run_cmd(
        ["gcloud", "config", "get-value", "account"],
        timeout=10,
    )
    account = account_result.stdout.strip() if account_result.returncode == 0 else ""
    # gcloud prints the literal "(unset)" when no account is configured.
    if not account or account == "(unset)":
        print_warning(
            "Could not detect current gcloud account; skipping KMS IAM "
            "grant. SOPS encryption may fail — grant "
            "roles/cloudkms.cryptoKeyEncrypterDecrypter manually."
        )
    else:
        # Reject exotic characters; an unexpected value here would silently
        # produce a malformed --member arg.
        _validate_cmd_arg(account, "gcloud account")
        member_type = (
            "serviceAccount"
            if account.endswith(".iam.gserviceaccount.com")
            else "user"
        )
        binding_result = run_cmd_with_retry(
            ["gcloud", "kms", "keys", "add-iam-policy-binding", key_name,
             f"--keyring={keyring_name}",
             f"--location={location}",
             f"--member={member_type}:{account}",
             "--role=roles/cloudkms.cryptoKeyEncrypterDecrypter",
             f"--project={project_id}",
             "--condition=None",
             "--quiet"],
            retries=2,
            delay=5.0,
            timeout=30,
        )
        if binding_result.returncode != 0:
            print_warning(
                f"Failed to grant cloudkms.cryptoKeyEncrypterDecrypter to "
                f"{account}. SOPS encryption will likely fail. "
                f"Grant manually:\n  gcloud kms keys add-iam-policy-binding "
                f"{key_name} --keyring={keyring_name} --location={location} "
                f"--member={member_type}:{account} "
                f"--role=roles/cloudkms.cryptoKeyEncrypterDecrypter "
                f"--project={project_id}"
            )

    return (
        f"projects/{project_id}/locations/{location}"
        f"/keyRings/{keyring_name}/cryptoKeys/{key_name}"
    )
