"""The 'verify' command: post-deployment health checks."""

import sys

from scripts.config import InstallerConfig
from scripts.display import (
    console,
    print_error,
    print_header,
    print_success,
    print_warning,
)
from scripts.verify import run_all_checks


_STATUS_SYMBOL = {
    "pass": "[green]\u2713[/green]",
    "fail": "[red]\u2717[/red]",
    "warn": "[yellow]\u26a0[/yellow]",
}


def _display_results(results: list[dict]) -> None:
    """Render health-check results to the console."""
    for r in results:
        symbol = _STATUS_SYMBOL.get(r["status"], "?")
        name = r["name"]
        msg = r["message"]
        ms = r["duration_ms"]
        console.print(f"    {symbol} {name} {'.' * max(1, 46 - len(name))} {msg} ({ms}ms)")


def cmd_verify(check_name: str | None = None) -> None:
    """Run post-deployment verification checks."""
    cfg = InstallerConfig()
    if not cfg.exists():
        print_error("No configuration found. Run [bold]voipbin-install init[/bold] first.")
        sys.exit(1)

    cfg.load()
    config_dict = cfg.to_dict()

    print_header("Deployment Health Check")
    console.print("  [dim]" + "\u2500" * 21 + "[/dim]")
    console.print()

    if check_name:
        from scripts import verify as verify_mod

        fn_name = f"check_{check_name}"
        fn = getattr(verify_mod, fn_name, None)
        if fn is None:
            print_error(f"Unknown check: {check_name}")
            print_error(f"Available: gke_cluster, pods_ready, services_endpoints, vms_running, cloudsql_running, static_ips_reserved, dns_resolution, http_health, sip_port")
            sys.exit(1)

        # Build args from config for the individual check
        project_id = config_dict.get("gcp_project_id", "")
        zone = config_dict.get("zone", "")
        region = config_dict.get("region", "")
        domain = config_dict.get("domain", "")
        args_map = {
            "check_gke_cluster": (project_id, zone, "voipbin-cluster"),
            "check_pods_ready": ("bin-manager",),
            "check_services_endpoints": ("bin-manager",),
            "check_vms_running": (project_id, zone, "kamailio"),
            "check_cloudsql_running": (project_id, "voipbin-mysql"),
            "check_static_ips_reserved": (project_id, region),
            "check_dns_resolution": (f"api.{domain}",),
            "check_http_health": (f"https://api.{domain}/health",),
            "check_sip_port": (f"sip.{domain}",),
        }
        args = args_map.get(fn_name, ())
        results = [fn(*args)]
    else:
        results = run_all_checks(config_dict)

    _display_results(results)

    passed = sum(1 for r in results if r["status"] == "pass")
    warned = sum(1 for r in results if r["status"] == "warn")
    failed = sum(1 for r in results if r["status"] == "fail")
    total = len(results)

    console.print()
    parts = [f"{passed}/{total} passed"]
    if warned:
        parts.append(f"{warned} warning{'s' if warned != 1 else ''}")
    if failed:
        parts.append(f"{failed} failed")
    summary = ", ".join(parts)

    if failed:
        print_error(f"Result: {summary}")
        sys.exit(1)
    elif warned:
        print_warning(f"Result: {summary}")
    else:
        print_success(f"Result: {summary}")
