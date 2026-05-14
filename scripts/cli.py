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
from scripts.commands.cert import (
    cmd_cert_clean_staging,
    cmd_cert_export_ca,
    cmd_cert_renew,
    cmd_cert_status,
)
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
@click.option(
    "--force-destroy-legacy-voipbin",
    is_flag=True,
    help="Opt in to destroying the legacy PR-D1 `voipbin` MySQL database during PR-D2 apply",
)
@click.option(
    "--stage",
    type=click.Choice([
        "terraform_init",
        "reconcile_imports",
        "terraform_apply",
        "reconcile_outputs",
        "k8s_apply",
        "reconcile_k8s_outputs",
        "cert_provision",
        "ansible_run",
        "terraform_reconcile",  # deprecated alias — expands to both new stages
    ]),
    default=None,
    help="Run only a specific pipeline stage",
)
def apply(auto_approve, dry_run, force_destroy_legacy_voipbin, stage):
    """Deploy VoIPBin infrastructure and services."""
    cmd_apply(
        auto_approve=auto_approve,
        dry_run=dry_run,
        stage=stage,
        force_destroy_legacy_voipbin=force_destroy_legacy_voipbin,
    )


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


# ---------------------------------------------------------------------------
# PR-Z cert subcommand group
# ---------------------------------------------------------------------------


@cli.group()
def cert():
    """Manage Kamailio TLS certificates (PR-Z)."""


@cert.command("status")
@click.option("--json", "as_json", is_flag=True, help="Output as JSON")
def cert_status(as_json):
    """Show per-SAN Kamailio TLS cert expiry, mode, and CA fingerprint."""
    rc = cmd_cert_status(as_json=as_json)
    if rc:
        sys.exit(rc)


@cert.command("renew")
@click.option(
    "--force", is_flag=True,
    help="Clear state.cert_state.leaf_certs first so short-circuit does not fire",
)
def cert_renew(force):
    """Re-run the cert_provision stage."""
    rc = cmd_cert_renew(force=force)
    if rc:
        sys.exit(rc)


@cert.command("clean-staging")
def cert_clean_staging():
    """Remove <workdir>/.cert-staging/ if present."""
    rc = cmd_cert_clean_staging()
    if rc:
        sys.exit(rc)


@cert.command("export-ca")
@click.option(
    "--out", "output_path", default=None, metavar="FILE",
    help="Write CA certificate to FILE instead of stdout.",
)
@click.option(
    "--der", "as_der", is_flag=True, default=False,
    help="Output DER-encoded bytes (default: PEM). Requires --out when stdout is a TTY.",
)
def cert_export_ca(output_path, as_der):
    """Export the installer-managed CA certificate (self_signed mode only)."""
    rc = cmd_cert_export_ca(output_path=output_path, as_der=as_der)
    sys.exit(rc)


if __name__ == "__main__":
    cli()
