"""Prerequisite and preflight checks for VoIPBin installer."""

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from scripts.diagnosis import (
    check_application_default_credentials,
    get_os_install_hint,
    offer_adc_setup,
)
from scripts.display import print_check, print_error, print_header, print_success
from scripts.utils import run_cmd


CLOUDSQL_PRIVATE_IP_SENTINEL = "cloudsql-private.invalid"


class PreflightError(RuntimeError):
    """Raised when a preflight check rejects the current configuration."""


def check_cloudsql_private_ip(config) -> None:
    """Reject sentinel/empty values for ``cloudsql_private_ip``.

    Raises :class:`PreflightError` with an operator-facing message naming
    the field and pointing at the operations doc. Called from the install
    pipeline before manifests are rendered.
    """
    value = (config.get("cloudsql_private_ip", "") or "").strip()
    if not value or value == CLOUDSQL_PRIVATE_IP_SENTINEL:
        raise PreflightError(
            f"config.cloudsql_private_ip is not set (got {value!r}). "
            "Provide the private IP of your Cloud SQL instance (visible "
            "in GCP Console → SQL → connections → Private IP). VPC peering "
            "between your GKE VPC and the Cloud SQL service-networking "
            "VPC must be active. See docs/operations/cloudsql-private-ip.md."
        )


def check_legacy_voipbin_destroy_safety(config, force: bool = False) -> None:
    """PR-D2a: guard against destroying the legacy `voipbin` MySQL database.

    PR-D1 created `google_sql_database.voipbin` + `google_sql_user.voipbin`.
    PR-D2a destroys both and replaces with per-app `bin_manager` / `asterisk`
    databases and per-app users. The destroy is safe on dev (no consumer
    authenticates as MySQL user `voipbin`, audit-verified 2026-05-12 across
    install repo + monorepo). On an upgraded environment where someone
    out-of-band created data in the legacy db, the destroy could lose data.

    Probe with `gcloud sql databases describe voipbin --instance=voipbin-mysql`.
    - rc != 0 → legacy db is gone (or unreachable) → return silently (fresh
      cluster path, OR post-D2a path where the db is already destroyed).
    - rc == 0 → legacy db exists → raise PreflightError unless `force=True`.

    Soft-skips: ``force=True`` short-circuits at entry; missing
    ``gcp_project_id`` short-circuits (cannot probe gcloud at all).

    Args:
        config: InstallerConfig-like, must support ``.get(key, default)``.
        force: when True, the check returns immediately. Equivalent to the
               operator passing ``--force-destroy-legacy-voipbin``.

    Raises:
        PreflightError: when the legacy database is observably present and
                        ``force`` is False.
    """
    if force:
        return

    project = (config.get("gcp_project_id", "") or "").strip()
    if not project:
        # Without a project id we cannot probe Cloud SQL. Later pipeline
        # stages will fail loudly with more actionable errors.
        return

    proc = run_cmd(
        [
            "gcloud", "sql", "databases", "describe", "voipbin",
            "--instance=voipbin-mysql", f"--project={project}",
            "--format=value(name)",
        ],
        capture=True,
    )
    if proc.returncode != 0:
        # Either the db is gone (clean post-D2a / fresh install) or gcloud
        # could not reach it. Either way, no destroy-safety block.
        return

    raise PreflightError(
        "The legacy `voipbin` MySQL database still exists on this project. "
        "PR-D2 destroys it and replaces with per-app databases "
        "(`bin_manager`, `asterisk`). On the common dev path the legacy "
        "database is from PR-D1 and is empty; re-run with "
        "`voipbin-install apply --force-destroy-legacy-voipbin` to opt in. "
        "See docs/operations/cloud-sql-credentials.md."
    )


@dataclass
class PreflightResult:
    tool: str
    version: str
    ok: bool
    required: str
    hint: str


def _parse_gcloud(output: str) -> str:
    m = re.search(r"(\d+\.\d+\.\d+)", output)
    return m.group(1) if m else ""


def _parse_terraform(output: str) -> str:
    m = re.search(r"v?(\d+\.\d+\.\d+)", output)
    return m.group(1) if m else ""


def _parse_ansible(output: str) -> str:
    m = re.search(r"core (\d+\.\d+\.\d+)", output)
    if not m:
        m = re.search(r"(\d+\.\d+\.\d+)", output)
    return m.group(1) if m else ""


def _parse_kubectl(output: str) -> str:
    try:
        data = json.loads(output)
        return data["clientVersion"]["gitVersion"].lstrip("v")
    except (json.JSONDecodeError, KeyError):
        m = re.search(r"v?(\d+\.\d+\.\d+)", output)
        return m.group(1) if m else ""


def _parse_generic(output: str) -> str:
    m = re.search(r"(\d+\.\d+\.\d+)", output)
    return m.group(1) if m else ""


PREREQUISITES = [
    {
        "tool": "gcloud",
        "flag": "--version",
        "min": "400.0.0",
        "parse": _parse_gcloud,
        "hint": "https://cloud.google.com/sdk/docs/install",
    },
    {
        "tool": "terraform",
        "flag": "--version",
        "min": "1.5.0",
        "parse": _parse_terraform,
        "hint": "https://developer.hashicorp.com/terraform/downloads",
    },
    {
        "tool": "ansible",
        "flag": "--version",
        "min": "2.15.0",
        "parse": _parse_ansible,
        "hint": "pip install ansible",
    },
    {
        "tool": "kubectl",
        "flag": "version --client -o json",
        "min": "1.28.0",
        "parse": _parse_kubectl,
        "hint": "https://kubernetes.io/docs/tasks/tools/",
    },
    {
        "tool": "python3",
        "flag": "--version",
        "min": "3.10.0",
        "parse": _parse_generic,
        "hint": "https://www.python.org/downloads/",
    },
    {
        "tool": "sops",
        "flag": "--version",
        "min": "3.7.0",
        "parse": _parse_generic,
        "hint": "https://github.com/getsops/sops/releases",
    },
]


def check_prerequisites() -> list[PreflightResult]:
    """Check all prerequisite tools and return results."""
    from scripts.utils import check_tool_exists, version_gte

    results: list[PreflightResult] = []
    for prereq in PREREQUISITES:
        tool = prereq["tool"]
        if not check_tool_exists(tool):
            results.append(PreflightResult(
                tool=tool, version="", ok=False,
                required=prereq["min"], hint=prereq["hint"],
            ))
            continue
        cmd = [tool] + prereq["flag"].split()
        result = run_cmd(cmd, timeout=30)
        output = result.stdout + result.stderr
        version = prereq["parse"](output) if result.returncode == 0 else ""

        ok = bool(version) and version_gte(version, prereq["min"])
        results.append(PreflightResult(
            tool=tool,
            version=version,
            ok=ok,
            required=prereq["min"],
            hint=prereq["hint"],
        ))
    return results


def check_gcp_auth() -> Optional[str]:
    """Check gcloud authentication. Returns account email or None."""
    result = run_cmd(
        ["gcloud", "auth", "list", "--filter=status:ACTIVE", "--format=value(account)"],
        timeout=15,
    )
    if result.returncode != 0 or not result.stdout.strip():
        return None
    return result.stdout.strip().splitlines()[0]


def check_gcp_project(project_id: str) -> bool:
    """Check that the GCP project exists and is accessible."""
    from scripts.utils import _validate_cmd_arg
    _validate_cmd_arg(project_id, "project_id")
    result = run_cmd(
        ["gcloud", "projects", "describe", project_id, "--format=value(projectId)"],
        timeout=15,
    )
    return result.returncode == 0 and project_id in result.stdout


def check_gcp_billing(project_id: str) -> bool:
    """Check that billing is enabled on the project."""
    from scripts.utils import _validate_cmd_arg
    _validate_cmd_arg(project_id, "project_id")
    result = run_cmd(
        ["gcloud", "billing", "projects", "describe", project_id,
         "--format=value(billingEnabled)"],
        timeout=15,
    )
    return result.returncode == 0 and "true" in result.stdout.lower()


def check_static_ip_quota(project_id: str, region: str, needed: int = 5) -> bool:
    """Check that the GCP project has at least *needed* regional
    static-IP slots free in *region*. Returns True on sufficient quota.

    Uses ``gcloud compute regions describe <region>`` and parses the
    STATIC_ADDRESSES quota. A return of False does not abort the
    install on its own; callers decide whether to treat the shortage
    as fatal.
    """
    from scripts.utils import _validate_cmd_arg
    _validate_cmd_arg(project_id, "project_id")
    _validate_cmd_arg(region, "region")
    result = run_cmd(
        [
            "gcloud", "compute", "regions", "describe", region,
            "--project", project_id, "--format=json",
        ],
        timeout=30,
    )
    if result.returncode != 0:
        return False
    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError:
        return False
    for q in data.get("quotas", []):
        if q.get("metric") == "STATIC_ADDRESSES":
            limit = q.get("limit", 0)
            usage = q.get("usage", 0)
            return (limit - usage) >= needed
    return False


def check_nodeport_availability(needed: int = 7) -> bool:
    """Check the cluster has at least *needed* free NodePort slots.

    Default NodePort range in Kubernetes is 30000-32767 (2768 slots).
    PR #3a consumed 4 NodePorts (api 443, frontends 80 × 3).
    PR #3b raises this to 7:
      api-manager LB         (443)               1
      hook-manager LB        (80, 443)           2
      hook-manager LB        healthCheckNodePort 1 (externalTrafficPolicy: Local)
      admin LB               (443)               1
      talk LB                (443)               1
      meet LB                (443)               1
      Total                                      7

    Returns True on sufficient capacity. False return is a non-fatal
    warning — callers decide whether to treat as warning or fatal.
    Returns False on kubectl failure or malformed output (defensive).
    """
    result = run_cmd(
        ["kubectl", "get", "svc", "--all-namespaces", "-o", "json"],
        timeout=30,
    )
    if result.returncode != 0:
        return False
    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError:
        return False

    used: set[int] = set()
    for item in data.get("items", []):
        for port in (item.get("spec", {}) or {}).get("ports", []) or []:
            np = port.get("nodePort")
            if isinstance(np, int):
                used.add(np)
    total = 32767 - 30000 + 1
    free = total - len(used)
    return free >= needed


def check_oslogin_setup() -> Optional[str]:
    """Verify the operator's OS Login profile is provisioned and the
    matching SSH key file is present locally.

    The Ansible stage connects to Kamailio/RTPEngine VMs using OS Login.
    That requires three things to exist BEFORE we invoke ansible-playbook:
      1. A POSIX account on the operator's OS Login profile (created
         automatically by ``gcloud compute config-ssh`` or by the first
         ``gcloud compute ssh`` invocation).
      2. The local SSH private key at ``~/.ssh/google_compute_engine``.
      3. The corresponding public key registered to the OS Login profile.

    Returns None on success, or a human-readable error string the caller
    surfaces verbatim. The remediation in every error message is the same
    single command, so operators have one thing to run and re-try.
    """
    remediation = (
        "Run the following once, then re-run: voipbin-install apply\n\n"
        "    gcloud compute config-ssh\n\n"
        "This generates ~/.ssh/google_compute_engine if missing and\n"
        "uploads the matching public key to your OS Login profile."
    )

    key_path = Path(os.path.expanduser("~/.ssh/google_compute_engine"))
    if not key_path.exists():
        return (
            f"OS Login SSH key not found at {key_path}.\n\n{remediation}"
        )

    result = run_cmd(
        ["gcloud", "compute", "os-login", "describe-profile",
         "--format=value(posixAccounts[0].username)"],
        timeout=30,
    )
    if result.returncode != 0:
        return (
            "Could not query OS Login profile. Ensure your active gcloud\n"
            "account has the 'Service Account User' and 'Compute OS Login'\n"
            f"IAM roles, then re-run.\n\n{remediation}"
        )
    username = result.stdout.strip()
    if not username:
        return (
            "OS Login profile has no POSIX account yet.\n\n" + remediation
        )

    pub_result = run_cmd(
        ["gcloud", "compute", "os-login", "ssh-keys", "list",
         "--format=value(key)"],
        timeout=30,
    )
    if pub_result.returncode != 0 or not pub_result.stdout.strip():
        return (
            "OS Login profile has no SSH keys registered.\n\n" + remediation
        )
    return None


def check_loadbalancer_addresses(terraform_outputs: dict[str, str]) -> list[str]:
    """Return the list of ADDRESS output names that are missing or empty.

    PR #3b binds all 5 reserved IPs to LB Services (api-manager,
    hook-manager, admin, talk, meet). PR #3a kept hook-manager as
    ClusterIP so its IP was excluded; this PR flips hook-manager to
    LoadBalancer and the IP must be wired or kustomize substitutes
    an empty string and GCP allocates an ephemeral IP, breaking
    the static-IP/DNS contract.

    Caller hard-fails when this returns a non-empty list.
    """
    required = [
        "api_manager_static_ip_address",
        "hook_manager_static_ip_address",
        "admin_static_ip_address",
        "talk_static_ip_address",
        "meet_static_ip_address",
    ]
    return [k for k in required if not (terraform_outputs.get(k) or "").strip()]


# PR-U-2: K8S_DIR semantics MUST match the K8S_DIR constant in scripts/k8s.py:16.
# Using a local Path computation here keeps preflight.py independent of k8s.py
# (preventing a cyclic import — k8s.py already imports preflight for the
# LoadBalancer check at k8s.py:k8s_apply).
_K8S_DIR = Path(__file__).resolve().parent.parent / "k8s"


def check_homer_credentials_present(terraform_outputs: dict[str, str]) -> None:
    """PR-U-2: assert HOMER Postgres password is harvested before k8s_apply.

    The HOMER manifest set under k8s/infrastructure/homer/ embeds the password
    via PLACEHOLDER_HOMER_DB_PASS substitution. An empty password renders as
    an empty DSN, heplify-server crashes on Postgres connect, and the Pod
    enters CrashLoopBackOff. This check makes the failure explicit at preflight
    instead of silent at apply.

    No-op when k8s/infrastructure/homer/ is absent (custom install profiles
    may exclude HOMER).
    """
    homer_dir = _K8S_DIR / "infrastructure" / "homer"
    if not homer_dir.exists():
        return
    pw = terraform_outputs.get("cloudsql_postgres_password_homer", "")
    if not pw:
        raise PreflightError(
            "HOMER Postgres password is empty in terraform_outputs. "
            "Run `voipbin-install apply --stage reconcile_outputs` to harvest "
            "it from Terraform, or confirm `terraform apply` succeeded for "
            "google_sql_user.voipbin_postgres_homer."
        )


def check_kamailio_homer_uri_present(
    terraform_outputs: dict[str, str],
    config,
) -> None:
    """PR-U-3: assert Kamailio HEP capture has a destination address.

    When `config.homer_enabled` is True (default), the heplify-client
    sidecar in the Kamailio docker-compose needs ${HOMER_URI} to point at
    a real heplify-server LoadBalancer. The harvested `heplify_lb_ip` is
    the source of truth; if it is empty, the Jinja gate in group_vars
    renders an empty HOMER_URI and the compose-level
    `{% if homer_enabled and heplify_lb_ip %}` gate omits the sidecar
    entirely. That silent-skip is benign on its own, but the operator's
    intent was capture-on. Make the failure explicit at preflight.

    No-op when `config.homer_enabled` is explicitly False.
    """
    if not bool(config.get("homer_enabled", True)):
        return
    lb_ip = (terraform_outputs.get("heplify_lb_ip", "") or "").strip()
    if not lb_ip:
        raise PreflightError(
            "Kamailio HOMER capture is enabled (config.homer_enabled=true) "
            "but heplify_lb_ip is empty in terraform_outputs. Run "
            "`voipbin-install apply --stage reconcile_k8s_outputs` to "
            "harvest the heplify Service LoadBalancer address, or set "
            "`homer_enabled: false` in config.yaml to disable HEP capture."
        )


def check_cert_provisioned() -> None:
    """Raise PreflightError if cert_provision has not run or left incomplete state.

    Called from _run_ansible inside the ``if not dry_run:`` block, so it is
    never reached on any dry_run path — including the ansible --check path
    (which only runs when outputs contain kamailio_internal_ips).

    Reads from load_state() only; no config argument needed.
    """
    from scripts.pipeline import load_state  # lazy: avoids top-level cycle

    state = load_state()
    cert_state = state.get("cert_state") or {}

    if not cert_state or not cert_state.get("actual_mode"):
        raise PreflightError(
            "cert_provision has not run or failed — cert_state is absent in state.yaml. "
            "Re-run with: voipbin-install cert renew"
        )

    mode = cert_state["actual_mode"]

    if mode == "self_signed" and not cert_state.get("ca_fingerprint_sha256"):
        raise PreflightError(
            "cert_state.actual_mode=self_signed but CA fingerprint is absent. "
            "cert_provision may have failed mid-run. "
            "Re-run with: voipbin-install cert renew"
        )

    san_list = cert_state.get("san_list") or []
    leaf_certs = cert_state.get("leaf_certs") or {}

    # Empty san_list is intentionally allowed: cert_provision ran but the
    # domain has no Kamailio SANs yet. Ansible will deploy no certs (no-op).
    for san in san_list:
        if san not in leaf_certs:
            raise PreflightError(
                f"cert_provision: leaf cert missing for SAN {san!r}. "
                "Re-run with: voipbin-install cert renew"
            )
        if not leaf_certs[san].get("fingerprint_sha256"):
            raise PreflightError(
                f"cert_provision: leaf cert for {san!r} has no fingerprint — "
                "cert state may be corrupted. "
                "Re-run with: voipbin-install cert renew"
            )


def run_preflight_display(results: list[PreflightResult]) -> bool:
    """Display preflight results. Returns True if all passed."""
    print_header("Checking prerequisites...")
    all_ok = True
    for r in results:
        print_check(r.tool, r.version, r.ok, r.required)
        if not r.ok:
            all_ok = False
            steps, can_auto = get_os_install_hint(r.tool)
            if steps:
                hint_line = steps[0] if len(steps) == 1 or can_auto else steps[-1]
            else:
                hint_line = r.hint
            print_error(f"  Install: {hint_line}")
    return all_ok
