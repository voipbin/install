"""The 'destroy' command: tear down all VoIPBin GCP resources."""

import sys

from scripts.config import InstallerConfig
from scripts.diagnosis import check_application_default_credentials, offer_adc_setup
from scripts.display import (
    console,
    confirm,
    print_banner,
    print_error,
    print_header,
    print_result_box,
    print_step,
    print_success,
    print_warning,
    prompt_text,
)
from scripts.pipeline import destroy_pipeline, load_state


def cmd_destroy(auto_approve: bool = False) -> None:
    """Tear down all VoIPBin GCP resources."""
    print_banner()

    # Load config
    config = InstallerConfig()
    if not config.exists():
        print_error("No configuration found. Nothing to destroy.")
        sys.exit(1)

    config.load()

    # ADC check — terraform destroy requires valid ADC credentials
    adc_ok, _ = check_application_default_credentials()
    if not adc_ok:
        refreshed = offer_adc_setup(auto_accept=auto_approve)
        if not refreshed:
            print_error("Terraform destroy requires Application Default Credentials.")
            sys.exit(1)

    project_id = config.get("gcp_project_id", "unknown")
    region = config.get("region", "unknown")

    # Check deployment state
    state = load_state()
    deploy_state = state.get("deployment_state", "")
    if deploy_state in ("destroyed", ""):
        print_warning("No active deployment found.")
        if not auto_approve and not confirm("Attempt destroy anyway?", default=False):
            return

    # Show what will be destroyed
    print_header("Resources to destroy")
    print_step(f"Project:  {project_id}")
    print_step(f"Region:   {region}")
    console.print()
    print_step("  This will delete:")
    print_step("    - Kubernetes workloads and services")
    print_step("    - GKE cluster and node pools")
    print_step("    - Kamailio and RTPEngine VMs")
    print_step("    - Cloud SQL instance and databases")
    print_step("    - VPC, subnets, firewall rules")
    print_step("    - Load balancers and static IPs")
    print_step("    - DNS zone (if auto-managed)")
    console.print()

    # First confirmation
    if not auto_approve:
        print_warning("[bold red]This action is irreversible![/bold red]")
        if not confirm("Are you sure you want to destroy all resources?", default=False):
            console.print("  Cancelled.")
            return

        # Double-confirm with project ID
        entered = prompt_text(
            f"Type the project ID to confirm ([bold]{project_id}[/bold])",
        )
        if entered != project_id:
            print_error("Project ID does not match. Aborting.")
            sys.exit(1)

    # Run destroy
    ok = destroy_pipeline(config=config, auto_approve=auto_approve)

    # Summary
    console.print()
    if ok:
        print_result_box([
            "[green]✓ All resources destroyed[/green]",
            "",
            f"  Project: {project_id}",
            "",
            "  Config files are still present.",
            "  To redeploy: [bold]voipbin-install apply[/bold]",
        ])
    else:
        print_result_box([
            "[red]✗ Destroy incomplete[/red]",
            "",
            "  Some resources may still exist.",
            "  Re-run: [bold]voipbin-install destroy[/bold]",
            "  Or clean up manually in the GCP Console.",
        ], style="red")
        sys.exit(1)
