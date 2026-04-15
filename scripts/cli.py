#!/usr/bin/env python3
"""VoIPBin Installer — CLI entry point."""

import sys
from pathlib import Path

# Ensure the project root is on sys.path so `config.*` and `scripts.*` resolve.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

import click

from scripts.commands.apply import cmd_apply
from scripts.commands.destroy import cmd_destroy
from scripts.commands.init import cmd_init
from scripts.commands.status import cmd_status
from scripts.commands.verify import cmd_verify


@click.group()
@click.version_option(version="1.0.0", prog_name="voipbin-install")
def cli():
    """VoIPBin Installer — deploy a full CPaaS platform to the cloud."""


@cli.command()
@click.option("--reconfigure", is_flag=True, help="Re-run wizard even if config exists")
@click.option("--config", "config_path", type=click.Path(), default="", help="Path to existing config.yaml")
@click.option("--skip-api-enable", is_flag=True, help="Skip GCP API enablement")
@click.option("--skip-quota-check", is_flag=True, help="Skip GCP quota validation")
@click.option("--dry-run", is_flag=True, help="Show what would be done without making changes")
def init(reconfigure, config_path, skip_api_enable, skip_quota_check, dry_run):
    """Initialize VoIPBin configuration and prepare GCP project."""
    cmd_init(
        reconfigure=reconfigure,
        config_path=config_path,
        skip_api_enable=skip_api_enable,
        skip_quota_check=skip_quota_check,
        dry_run=dry_run,
    )


@cli.command()
@click.option("--auto-approve", is_flag=True, help="Skip confirmation prompts")
@click.option("--dry-run", is_flag=True, help="Show what would be done without making changes")
@click.option("--stage", type=click.Choice(["terraform_init", "terraform_apply", "ansible_run", "k8s_apply"]), default=None, help="Run only a specific pipeline stage")
def apply(auto_approve, dry_run, stage):
    """Deploy VoIPBin infrastructure and services."""
    cmd_apply(auto_approve=auto_approve, dry_run=dry_run, stage=stage)


@cli.command()
@click.option("--auto-approve", is_flag=True, help="Skip confirmation prompts")
def destroy(auto_approve):
    """Tear down all VoIPBin resources."""
    cmd_destroy(auto_approve=auto_approve)


@cli.command()
@click.option("--json", "as_json", is_flag=True, help="Output status as JSON")
def status(as_json):
    """Show current VoIPBin deployment status."""
    cmd_status(as_json=as_json)


@cli.command()
@click.option("--check", "check_name", default=None, help="Run a specific check only")
def verify(check_name):
    """Verify VoIPBin deployment health."""
    cmd_verify(check_name=check_name)


if __name__ == "__main__":
    cli()
