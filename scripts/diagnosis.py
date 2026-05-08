"""Diagnosis and guided recovery functions for VoIPBin installer."""

from __future__ import annotations

import os
import platform
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING

from scripts.display import confirm, print_error, print_fix, print_success, print_warning
from scripts.gcp import check_billing_tristate, check_quotas, check_required_apis
from scripts.utils import check_tool_exists, run_cmd

if TYPE_CHECKING:
    from scripts.config import InstallerConfig


# ---------------------------------------------------------------------------
# 1. ADC Check and Setup
# ---------------------------------------------------------------------------

def check_application_default_credentials() -> tuple[bool, str | None]:
    """Check Application Default Credentials validity.

    Returns (is_valid, account). is_valid=True means the ADC token works.
    account is a best-effort lookup — may be None if not retrievable.
    """
    result = run_cmd(["gcloud", "auth", "application-default", "print-access-token"])
    if result.returncode != 0:
        return False, None

    account_result = run_cmd(["gcloud", "config", "get-value", "account"])
    if account_result.returncode != 0:
        return True, None
    account = account_result.stdout.strip()
    if not account or account == "(unset)":
        return True, None
    return True, account


def _get_adc_file_path() -> Path:
    """Return the ADC credentials file path, respecting CLOUDSDK_CONFIG."""
    cloudsdk_config = os.environ.get("CLOUDSDK_CONFIG")
    if cloudsdk_config:
        return Path(cloudsdk_config) / "application_default_credentials.json"
    return Path.home() / ".config" / "gcloud" / "application_default_credentials.json"


def offer_adc_setup(auto_accept: bool = False) -> bool:
    """Offer to set up or refresh Application Default Credentials.

    Guard (unconditional): if gcloud is not on PATH, print prereq message and
    return False immediately — even auto_accept=True cannot bypass this.
    """
    if shutil.which("gcloud") is None:
        print_error("gcloud CLI is not installed. Run: voipbin-install init")
        return False

    adc_path = _get_adc_file_path()
    if not adc_path.exists():
        print_warning("Application Default Credentials are not yet configured.")
        print_warning("These credentials allow Terraform to access GCP on your behalf.")
        prompt = "Set up credentials now? [Y/n]"
    else:
        print_warning("Your Application Default Credentials are invalid or expired.")
        prompt = "Refresh credentials now? [Y/n]"

    print_warning("A browser window will open for you to sign in to GCP.")

    if not auto_accept:
        if not confirm(prompt, default=True):
            print_fix("How to fix", ["gcloud auth application-default login"])
            return False

    result = run_cmd(
        ["gcloud", "auth", "application-default", "login"],
        capture=False,
        timeout=300,
    )
    if result.returncode != 0:
        print_fix("How to fix", ["gcloud auth application-default login"])
        print_error("Then re-run: voipbin-install <command>")
        return False

    valid, _ = check_application_default_credentials()
    return valid


# ---------------------------------------------------------------------------
# 2. Pre-Apply Health Checks
# ---------------------------------------------------------------------------

def run_pre_apply_checks(
    config: InstallerConfig,
    auto_approve: bool = False,
    only_stage: str | None = None,
) -> bool:
    """Run pre-apply health checks. Returns True if deployment may proceed.

    Checks (in order): ADC, project access, billing, required APIs.
    Checks 2-4 are skipped if state is fresh (<24h), not failed, and
    only_stage is None — to avoid redundant GCP calls on resume.
    """
    # Lazy import: keeps diagnosis.py importable without pipeline.py loaded.
    # Tests must patch "scripts.pipeline.load_state", not "scripts.diagnosis.load_state".
    from scripts.pipeline import load_state

    project_id = config.get("gcp_project_id")

    # Check 1: ADC (always runs)
    valid, _ = check_application_default_credentials()
    if not valid:
        refreshed = offer_adc_setup(auto_accept=auto_approve)
        if not refreshed:
            return False

    # Timestamp-based skip for checks 2-4
    if only_stage is None:
        state = load_state()
        ts_str = state.get("timestamp")
        deploy_state = state.get("deployment_state", "")
        if ts_str and deploy_state != "failed":
            try:
                ts = datetime.fromisoformat(ts_str)
                if datetime.now(timezone.utc) - ts < timedelta(hours=24):
                    return True  # checks 2-4 skipped
            except (ValueError, TypeError):
                pass

    # Check 2: project accessible
    result = run_cmd(["gcloud", "projects", "describe", project_id, "--format=value(projectId)"])
    if result.returncode != 0:
        print_error(f"Cannot access project '{project_id}'. Check project ID and IAM permissions.")
        return False

    # Check 3: billing
    billing = check_billing_tristate(project_id)
    if billing == "disabled":
        print_error(f"Billing is disabled on project '{project_id}'.")
        print_fix(
            "Enable billing",
            [f"https://console.cloud.google.com/billing/linkedaccount?project={project_id}"],
        )
        return False
    # "unknown" → probe failed, skip hint, continue

    # Check 4: required APIs
    missing = check_required_apis(project_id)
    if missing:
        apis_str = " ".join(missing)
        print_error("Required GCP APIs are not enabled.")
        print_fix("Enable APIs", [f"gcloud services enable {apis_str} --project {project_id}"])
        return False

    return True


# ---------------------------------------------------------------------------
# 3. Stage Failure Diagnosis
# ---------------------------------------------------------------------------

def diagnose_stage_failure(config: InstallerConfig, stage: str) -> list[str]:
    """Probe GCP state after a stage failure. Returns list of hint strings.

    ADC-first guard: if ADC is invalid, return immediately with one hint.
    Do NOT probe billing or other GCP resources when ADC is invalid — they
    would all fail with PERMISSION_DENIED and generate false positives.
    """
    project_id = config.get("gcp_project_id")
    region = config.get("region")
    zone = config.get("zone")
    cluster_name = "voipbin-gke-cluster"  # matches gke.tf var.env default

    hints: list[str] = []

    # ADC-first guard
    valid, _ = check_application_default_credentials()
    if not valid:
        hints.append(
            "Likely cause: Application Default Credentials expired → Fix: "
            "gcloud auth application-default login"
        )
        return hints

    # Billing (all stages)
    billing = check_billing_tristate(project_id)
    if billing == "disabled":
        hints.append(
            f"Likely cause: billing disabled on project '{project_id}' → Fix: "
            f"https://console.cloud.google.com/billing/linkedaccount?project={project_id}"
        )

    # Stage-specific checks
    if stage in ("terraform_init", "terraform_reconcile", "terraform_apply"):
        bucket = f"gs://{project_id}-voipbin-tf-state"
        r = run_cmd(["gcloud", "storage", "ls", bucket, f"--project={project_id}"])
        if r.returncode != 0:
            hints.append(
                f"Likely cause: state bucket does not exist → Fix: "
                f"gcloud storage buckets create {bucket} --project={project_id}"
            )

        missing = check_required_apis(project_id)
        if missing:
            hints.append(
                f"Likely cause: required APIs not enabled ({', '.join(missing)}) → Fix: "
                f"gcloud services enable {' '.join(missing)} --project {project_id}"
            )

        if stage in ("terraform_reconcile", "terraform_apply"):
            for q in check_quotas(project_id, region):
                if not q.ok:
                    hints.append(
                        f"Likely cause: insufficient {q.metric} quota "
                        f"({q.available:.0f} available, {q.required:.0f} required) → Fix: "
                        f"https://console.cloud.google.com/iam-admin/quotas?project={project_id}"
                    )

    elif stage == "ansible_run":
        r = run_cmd([
            "gcloud", "compute", "instances", "list",
            f"--project={project_id}",
            "--filter=labels.env=voipbin",
            "--format=value(name,status)",
        ])
        if r.returncode == 0:
            lines = [ln.strip() for ln in r.stdout.splitlines() if ln.strip()]
            if not lines:
                hints.append(
                    "Likely cause: VMs not yet created — terraform_apply stage may not have completed → Fix: "
                    "voipbin-install apply --stage terraform_apply"
                )
            else:
                statuses = [ln.split("\t")[-1] for ln in lines]
                if not any(s == "RUNNING" for s in statuses):
                    hints.append(
                        "Likely cause: VMs may still be booting → Fix: "
                        "wait 2 minutes and re-run: voipbin-install apply"
                    )
                if any(s == "TERMINATED" for s in statuses):
                    hints.append(
                        f"Likely cause: one or more VMs stopped unexpectedly → Fix: "
                        f"https://console.cloud.google.com/compute/instances?project={project_id}"
                    )

    elif stage == "k8s_apply":
        r = run_cmd([
            "gcloud", "container", "clusters", "describe", cluster_name,
            f"--project={project_id}",
            f"--zone={zone}",
            "--format=value(status)",
        ])
        if r.returncode != 0:
            hints.append(
                "Likely cause: GKE cluster not found — terraform_apply stage may not have completed → Fix: "
                "voipbin-install apply --stage terraform_apply"
            )
        elif "PROVISIONING" in r.stdout.upper():
            hints.append(
                "Likely cause: GKE cluster still provisioning → Fix: "
                "wait 5 minutes and re-run: voipbin-install apply"
            )

    return hints


# ---------------------------------------------------------------------------
# 4. OS-Aware Install Hints
# ---------------------------------------------------------------------------

def _detect_os() -> str:
    """Detect the current OS. Returns: macos, debian, rhel, fedora, arch, linux."""
    if platform.system() == "Darwin":
        return "macos"
    try:
        with open("/etc/os-release") as f:
            for line in f:
                if line.startswith("ID="):
                    val = line.split("=", 1)[1].strip().strip('"').lower()
                    if val in ("ubuntu", "debian"):
                        return "debian"
                    if val in ("rhel", "centos", "rocky", "almalinux"):
                        return "rhel"
                    if val == "fedora":
                        return "fedora"
                    if val == "arch":
                        return "arch"
    except OSError:
        pass
    return "linux"


# can_auto=True only for single, side-effect-free commands (no pipe operators,
# no shell redirections). Any step using | or >> must set can_auto=False so
# offer_tool_install displays it as a manual step rather than running it.
_INSTALL_HINTS: dict[str, dict[str, tuple[list[str], bool]]] = {
    "gcloud": {
        "macos":  (["brew install --cask google-cloud-sdk"], True),
        "debian": (["curl https://sdk.cloud.google.com | bash"], False),
        "rhel":   ([
            "sudo tee /etc/yum.repos.d/google-cloud-sdk.repo << 'EOM'\n"
            "[google-cloud-cli]\nname=Google Cloud CLI\n"
            "baseurl=https://packages.cloud.google.com/yum/repos/cloud-sdk-el9-x86_64\n"
            "enabled=1\ngpgcheck=1\nrepo_gpgcheck=0\n"
            "gpgkey=https://packages.cloud.google.com/yum/doc/rpm-package-key.gpg\nEOM",
            "sudo dnf install -y google-cloud-cli",
        ], False),
        "linux":  (["curl https://sdk.cloud.google.com | bash"], False),
    },
    "terraform": {
        "macos":  (["brew tap hashicorp/tap", "brew install hashicorp/tap/terraform"], True),
        "debian": ([
            "wget -O- https://apt.releases.hashicorp.com/gpg | sudo gpg --dearmor -o /usr/share/keyrings/hashicorp-archive-keyring.gpg",
            'echo "deb [signed-by=/usr/share/keyrings/hashicorp-archive-keyring.gpg] https://apt.releases.hashicorp.com $(lsb_release -cs) main" | sudo tee /etc/apt/sources.list.d/hashicorp.list',
            "sudo apt update && sudo apt install -y terraform",
        ], False),
        "rhel":   ([
            "sudo yum install -y yum-utils",
            "sudo yum-config-manager --add-repo https://rpm.releases.hashicorp.com/RHEL/hashicorp.repo",
            "sudo yum install -y terraform",
        ], False),
        "linux":  (["https://developer.hashicorp.com/terraform/downloads"], False),
    },
    "ansible": {
        "macos":  (["pip3 install ansible"], True),
        "debian": (["pip3 install ansible"], True),
        "rhel":   (["pip3 install ansible"], True),
        "linux":  (["pip3 install ansible"], True),
    },
    "kubectl": {
        "macos":  (["brew install kubectl"], True),
        "debian": (["gcloud components install kubectl"], True),
        "rhel":   (["gcloud components install kubectl"], True),
        "linux":  (["gcloud components install kubectl"], True),
    },
    "sops": {
        "macos":  (["brew install sops"], True),
        "debian": (["https://github.com/getsops/sops/releases/latest"], False),
        "rhel":   (["https://github.com/getsops/sops/releases/latest"], False),
        "linux":  (["https://github.com/getsops/sops/releases/latest"], False),
    },
    "python3": {
        "macos":  (["https://www.python.org/downloads/"], False),
        "debian": (["https://www.python.org/downloads/"], False),
        "rhel":   (["https://www.python.org/downloads/"], False),
        "linux":  (["https://www.python.org/downloads/"], False),
    },
}


def get_os_install_hint(tool: str) -> tuple[list[str], bool]:
    """Return (install_steps, can_auto_run) for the given tool on the current OS."""
    os_name = _detect_os()
    tool_hints = _INSTALL_HINTS.get(tool, {})
    steps, can_auto = tool_hints.get(os_name, tool_hints.get("linux", ([], False)))
    return steps, can_auto


def offer_tool_install(tool: str) -> bool:
    """Print install hints and optionally run them. Returns True only if tool is now on PATH."""
    steps, can_auto = get_os_install_hint(tool)
    print_fix(f"Install {tool}", steps)

    if not can_auto:
        print_error("Run the commands above in your terminal, then re-run: voipbin-install init")
        return False

    if not confirm(f"Install {tool} now?", default=True):
        return False

    for step in steps:
        result = run_cmd(step, capture=False)
        if result.returncode != 0:
            print_error(f"Install step failed: {step}")
            return False

    if check_tool_exists(tool):
        return True

    print_error(
        "Installation may require restarting your shell. "
        "Open a new terminal, then re-run: voipbin-install init"
    )
    return False
