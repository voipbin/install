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
    """VoIPBin Installer — deploy a full CPaaS platform to GCP.

    \b
    Typical workflow (first-time setup):
      1. ./voipbin-install init      — run the setup wizard
      2. ./voipbin-install apply     — provision all GCP resources
      3. ./voipbin-install verify    — confirm everything is healthy

    \b
    Day-to-day commands:
      ./voipbin-install status       — show current deployment state
      ./voipbin-install destroy      — tear down all resources

    Run any command with --help for detailed options.
    """


@cli.command()
@click.option("--reconfigure", is_flag=True, help="Re-run wizard even if config exists")
@click.option("--config", "config_path", type=click.Path(), default="", help="Path to existing config.yaml")
@click.option("--skip-api-enable", is_flag=True, help="Skip GCP API enablement")
@click.option("--skip-quota-check", is_flag=True, help="Skip GCP quota validation")
@click.option("--dry-run", is_flag=True, help="Show what would be done without making changes")
def init(reconfigure, config_path, skip_api_enable, skip_quota_check, dry_run):
    """Initialize VoIPBin configuration and prepare your GCP project.

    \b
    This is the first command to run. Before running init, authenticate with GCP:
      gcloud auth login
      gcloud auth application-default login

    \b
    The wizard prompts for 8 settings:
      - GCP project ID, region, GKE cluster type
      - TLS strategy: self-signed (auto) or byoc (Bring Your Own Cert)
      - Docker image tag strategy: latest or pinned
      - Domain name
      - Kamailio cert mode: self_signed or manual
      - Cloud DNS mode: auto or manual
    It also enables required GCP APIs, creates a service account and IAM
    bindings, provisions a KMS key ring for secrets encryption, and writes
    config.yaml and an encrypted secrets.yaml.

    \b
    Examples:
      ./voipbin-install init                        # Interactive setup (recommended)
      ./voipbin-install init --reconfigure          # Re-run wizard to change settings
      ./voipbin-install init --dry-run              # Preview changes without applying
      ./voipbin-install init --config path/to/config.yaml  # Import existing config
    """
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
    """Deploy VoIPBin infrastructure and services to GCP.

    \b
    Runs the full deployment pipeline in order:
      1. terraform_init         — initialize Terraform backend
      2. reconcile_imports      — import any drifted GCP resources into state
      3. terraform_apply        — provision GCP infrastructure (VPC, GKE, Cloud SQL, VMs)
      4. reconcile_outputs      — read Terraform outputs into config.yaml
      5. k8s_apply              — deploy Kubernetes workloads
      6. reconcile_k8s_outputs  — read K8s LB IPs into config.yaml
      7. cert_provision         — issue Kamailio TLS certificates
      8. ansible_run            — configure Kamailio and RTPEngine VMs

    The pipeline is resumable: if a stage fails, fix the issue and re-run
    `apply` — it picks up from where it left off.

    \b
    Examples:
      ./voipbin-install apply                        # Full deployment (interactive)
      ./voipbin-install apply --auto-approve         # Skip confirmation prompts (CI/CD)
      ./voipbin-install apply --dry-run              # Preview plan without applying
      ./voipbin-install apply --stage ansible_run    # Re-run only the Ansible stage
    """
    cmd_apply(
        auto_approve=auto_approve,
        dry_run=dry_run,
        stage=stage,
        force_destroy_legacy_voipbin=force_destroy_legacy_voipbin,
    )


@cli.command()
@click.option("--auto-approve", is_flag=True, help="Skip confirmation prompts")
def destroy(auto_approve):
    """Tear down all VoIPBin GCP resources.

    \b
    Removes all infrastructure created by `apply` in reverse order:
      - Kubernetes workloads
      - GKE cluster
      - Cloud SQL instances (MySQL and Postgres)
      - Compute VMs (Kamailio, RTPEngine)
      - VPC, subnets, firewall rules, load balancers
      - Service accounts, IAM bindings

    \b
    WARNING: This operation is IRREVERSIBLE. All data in Cloud SQL will be
    permanently deleted. Export any data before running this command.

    \b
    Examples:
      ./voipbin-install destroy                  # Interactive (asks for confirmation)
      ./voipbin-install destroy --auto-approve   # Skip confirmation (use with care)
    """
    cmd_destroy(auto_approve=auto_approve)


@cli.command()
@click.option("--json", "as_json", is_flag=True, help="Output status as JSON")
def status(as_json):
    """Show current VoIPBin deployment status.

    \b
    Displays a summary of all deployment components:
      - Deployment state (not deployed / deploying / deployed / failed)
      - Terraform resource count
      - GKE cluster status and node count  (only when deployed)
      - Kubernetes pod phases              (only when deployed)
      - VM instance status                (only when deployed)

    \b
    Examples:
      ./voipbin-install status            # Human-readable output
      ./voipbin-install status --json     # Machine-readable JSON (for scripting)
    """
    cmd_status(as_json=as_json)


@cli.command()
@click.option("--check", "check_name", default=None, help="Run a specific check only")
def verify(check_name):
    """Verify VoIPBin deployment health.

    \b
    Runs a series of health checks against the live deployment:
      - API endpoint reachability
      - SIP registration connectivity
      - Kubernetes pod readiness
      - TLS certificate validity

    Run after `apply` completes to confirm everything is working correctly.

    \b
    Examples:
      ./voipbin-install verify                       # Run all health checks
      ./voipbin-install verify --check http_health   # Run only the HTTP health check
    """
    cmd_verify(check_name=check_name)


# ---------------------------------------------------------------------------
# PR-Z cert subcommand group
# ---------------------------------------------------------------------------


@cli.group()
def cert():
    """Manage Kamailio TLS certificates."""


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
    help="Force re-issuance even if certificates are not yet expired",
)
def cert_renew(force):
    """Re-run the cert_provision stage to renew Kamailio TLS certificates.

    Use when certificates have expired, are approaching expiry, or after
    changing the domain name. With --force, skips the expiry check and
    always re-issues.
    """
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
