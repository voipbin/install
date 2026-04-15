"""The 'apply' command: deploy VoIPBin to GCP."""

import sys
from typing import Optional

from scripts.config import InstallerConfig
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
)
from scripts.pipeline import APPLY_STAGES, STAGE_LABELS, load_state, run_pipeline


def _show_plan(config: InstallerConfig, dry_run: bool, only_stage: Optional[str]) -> None:
    """Display the deployment plan before executing."""
    project_id = config.get("gcp_project_id", "unknown")
    region = config.get("region", "unknown")
    domain = config.get("domain", "unknown")
    mode = "DRY RUN" if dry_run else "DEPLOY"

    print_header(f"Deployment Plan ({mode})")
    print_step(f"Project:  {project_id}")
    print_step(f"Region:   {region}")
    print_step(f"Domain:   {domain}")
    console.print()

    state = load_state()
    stages = state.get("stages", {})

    if only_stage:
        label = STAGE_LABELS.get(only_stage, only_stage)
        print_step(f"  [bold]-> {label}[/bold] (single stage)")
    else:
        for stage_name in APPLY_STAGES:
            label = STAGE_LABELS.get(stage_name, stage_name)
            status = stages.get(stage_name, "pending")
            if status == "complete":
                print_step(f"  [dim][green]✓[/green] {label} (complete)[/dim]")
            else:
                print_step(f"  [bold]-> {label}[/bold]")
    console.print()


def cmd_apply(
    auto_approve: bool = False,
    dry_run: bool = False,
    stage: Optional[str] = None,
) -> None:
    """Run the full deployment pipeline."""
    print_banner()

    # Load config
    config = InstallerConfig()
    if not config.exists():
        print_error("No configuration found. Run [bold]voipbin-install init[/bold] first.")
        sys.exit(1)

    config.load()
    errors = config.validate()
    if errors:
        print_error("Configuration is invalid:")
        for err in errors:
            print_error(f"  {err}")
        sys.exit(1)

    # Validate --stage option
    if stage and stage not in APPLY_STAGES:
        valid = ", ".join(APPLY_STAGES)
        print_error(f"Unknown stage: {stage}")
        print_step(f"Valid stages: {valid}")
        sys.exit(1)

    # Show plan
    _show_plan(config, dry_run, stage)

    # Check for previous state
    state = load_state()
    prev_state = state.get("deployment_state", "")
    if prev_state == "failed":
        print_warning("Previous deployment failed. Resuming from last checkpoint.")
    elif prev_state == "deployed" and not stage:
        print_warning("Environment is already deployed.")
        if not auto_approve and not confirm("Re-apply?", default=False):
            return

    # Confirm
    if not auto_approve and not dry_run:
        if not confirm("Proceed with deployment?", default=True):
            console.print("  Cancelled.")
            return

    # Run pipeline
    ok = run_pipeline(
        config=config,
        dry_run=dry_run,
        auto_approve=auto_approve,
        only_stage=stage,
    )

    # Summary
    console.print()
    if ok:
        if dry_run:
            print_result_box([
                "[green]✓ Dry run complete[/green]",
                "",
                "  No changes were made.",
                "  Run [bold]voipbin-install apply[/bold] to deploy for real.",
            ])
        else:
            project_id = config.get("gcp_project_id", "")
            domain = config.get("domain", "")
            print_result_box([
                "[green]✓ Deployment complete[/green]",
                "",
                f"  Project:  {project_id}",
                f"  Domain:   {domain}",
                "",
                "  Next steps:",
                "    1. Configure DNS records (if manual)",
                "    2. Verify with: [bold]voipbin-install status[/bold]",
            ])
    else:
        print_result_box([
            "[red]✗ Deployment failed[/red]",
            "",
            "  Check the error above.",
            "  Fix the issue and re-run: [bold]voipbin-install apply[/bold]",
            "  The pipeline will resume from the failed stage.",
        ], style="red")
        sys.exit(1)
