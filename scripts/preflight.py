"""Prerequisite and preflight checks for VoIPBin installer."""

import json
import re
from dataclasses import dataclass
from typing import Callable, Optional

from scripts.diagnosis import (
    check_application_default_credentials,
    get_os_install_hint,
    offer_adc_setup,
)
from scripts.display import print_check, print_error, print_header, print_success
from scripts.utils import run_cmd


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
