"""The 'status' command: show current VoIPBin deployment status."""

import json
import sys

from scripts.config import InstallerConfig
from scripts.display import (
    console,
    print_banner,
    print_error,
    print_header,
    print_step,
    print_success,
    print_warning,
)
from scripts.k8s import k8s_cluster_status, k8s_status
from scripts.pipeline import STAGE_LABELS, load_state
from scripts.terraform import terraform_resource_count


def _stage_icon(status: str) -> str:
    """Return a Rich-formatted icon for a stage status."""
    icons = {
        "complete": "[green]✓[/green]",
        "running": "[yellow]…[/yellow]",
        "failed": "[red]✗[/red]",
        "pending": "[dim]-[/dim]",
    }
    return icons.get(status, "[dim]?[/dim]")


def _print_deployment_state(state: dict) -> None:
    """Print pipeline checkpoint info."""
    deploy_state = state.get("deployment_state", "unknown")
    timestamp = state.get("timestamp", "")

    print_header("Deployment State")
    print_step(f"Status:    {deploy_state}")
    if timestamp:
        print_step(f"Updated:   {timestamp}")

    stages = state.get("stages", {})
    if stages:
        console.print()
        for stage_name, status in stages.items():
            label = STAGE_LABELS.get(stage_name, stage_name)
            icon = _stage_icon(status)
            print_step(f"  {icon} {label}: {status}")


def _print_terraform_status(config: InstallerConfig) -> None:
    """Print Terraform resource count."""
    print_header("Terraform")
    try:
        count = terraform_resource_count(config)
    except Exception as exc:
        print_warning(f"Could not read Terraform state: {exc}")
        return
    if count < 0:
        print_warning("Could not read Terraform state")
    elif count == 0:
        print_step("No resources in state")
    else:
        print_success(f"{count} resources managed")


def _print_gke_status(config: InstallerConfig) -> None:
    """Print GKE cluster status."""
    print_header("GKE Cluster")
    cluster_info = k8s_cluster_status(config)
    status = cluster_info.get("status", "")
    if status == "error":
        print_warning(cluster_info.get("message", "Unknown error"))
    elif status == "not_found":
        print_step("No cluster found")
    else:
        name = cluster_info.get("name", "")
        version = cluster_info.get("version", "")
        nodes = cluster_info.get("node_count", "0")
        print_step(f"Cluster:   {name}")
        print_step(f"Status:    {status}")
        print_step(f"Version:   {version}")
        print_step(f"Nodes:     {nodes}")


def _print_pod_status(config: InstallerConfig) -> None:
    """Print pod status summary."""
    print_header("Pods")
    result = k8s_status(config)
    if result.get("error"):
        print_warning(result["error"])
        return
    summary = result.get("summary", {})
    if not summary:
        print_step("No pods found")
        return
    total = sum(summary.values())
    print_step(f"Total pods: {total}")
    for phase, count in sorted(summary.items()):
        if phase == "Running":
            print_success(f"  {phase}: {count}")
        elif phase in ("Failed", "CrashLoopBackOff"):
            print_error(f"  {phase}: {count}")
        else:
            print_step(f"  {phase}: {count}")


def _print_vm_status(config: InstallerConfig) -> None:
    """Print VM instance status."""
    print_header("VMs")
    project_id = config.get("gcp_project_id", "")
    zone = config.get("zone", "")
    from scripts.utils import run_cmd

    cmd = (
        f"gcloud compute instances list"
        f" --project {project_id}"
        f" --zones {zone}"
        f" --filter='tags.items=voipbin'"
        f" --format='json(name,status,networkInterfaces[0].accessConfigs[0].natIP)'"
    )
    result = run_cmd(cmd, capture=True, timeout=60)
    if result.returncode != 0:
        print_warning("Could not list VMs")
        return
    try:
        vms = json.loads(result.stdout)
    except json.JSONDecodeError:
        print_warning("Could not parse VM list")
        return
    if not vms:
        print_step("No VoIPBin VMs found")
        return
    for vm in vms:
        name = vm.get("name", "")
        status = vm.get("status", "")
        icon = "[green]✓[/green]" if status == "RUNNING" else "[yellow]⚠[/yellow]"
        print_step(f"  {icon} {name}: {status}")


def _build_json_status(config: InstallerConfig, state: dict) -> dict:
    """Build a machine-readable status dict."""
    try:
        tf_count = terraform_resource_count(config)
    except Exception:
        tf_count = -1

    deployment_state = state.get("deployment_state", "unknown")
    result: dict = {
        "deployment_state": deployment_state,
        "timestamp": state.get("timestamp", ""),
        "stages": state.get("stages", {}),
        "terraform_resource_count": tf_count,
    }

    if deployment_state == "deployed":
        result["gke_cluster"] = k8s_cluster_status(config)
        result["pods"] = k8s_status(config)

    return result


def cmd_status(as_json: bool = False) -> None:
    """Show current VoIPBin deployment status."""
    config = InstallerConfig()
    if not config.exists():
        if as_json:
            console.print(json.dumps({"deployment_state": "not_initialized"}, indent=2))
        else:
            print_banner()
            print_error("No configuration found. Run [bold]voipbin-install init[/bold] first.")
        return

    config.load()
    state = load_state()

    if as_json:
        status_data = _build_json_status(config, state)
        console.print(json.dumps(status_data, indent=2, default=str))
        return

    print_banner()

    print_header("Configuration")
    print_step(f"Project:  {config.get('gcp_project_id', 'unknown')}")
    print_step(f"Region:   {config.get('region', 'unknown')}")
    print_step(f"Domain:   {config.get('domain', 'unknown')}")

    if state:
        _print_deployment_state(state)
    else:
        print_header("Deployment State")
        print_step("Not yet deployed")

    _print_terraform_status(config)

    deployment_state = state.get("deployment_state", "") if state else ""
    if deployment_state == "deployed":
        _print_gke_status(config)
        _print_pod_status(config)
        _print_vm_status(config)

    console.print()
