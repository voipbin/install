"""The 'init' command: wizard + preflight + GCP setup + config generation."""

import sys

import yaml

from scripts.config import InstallerConfig
from scripts.utils import INSTALLER_DIR
from scripts.display import (
    console,
    create_progress,
    print_banner,
    print_cost_table,
    print_error,
    print_header,
    print_result_box,
    print_step,
    print_success,
    print_warning,
    confirm,
)
from scripts.gcp import (
    check_quotas,
    create_kms_keyring,
    create_service_account,
    display_quota_results,
    enable_apis,
)
from scripts.preflight import (
    check_gcp_auth,
    check_gcp_billing,
    check_gcp_project,
    check_prerequisites,
    check_static_ip_quota,
    run_preflight_display,
)
from scripts.diagnosis import (
    check_application_default_credentials,
    get_os_install_hint,
    offer_adc_setup,
    offer_tool_install,
)
from scripts.secretmgr import generate_and_encrypt, write_sops_config
from scripts.wizard import run_wizard


def _can_auto_run(tool: str) -> bool:
    _, can_auto = get_os_install_hint(tool)
    return can_auto


def _count_gcp_apis() -> int:
    """Count the number of GCP APIs defined in config."""
    path = INSTALLER_DIR / "config" / "gcp_apis.yaml"
    with open(path) as f:
        data = yaml.safe_load(f)
    return len(data.get("apis", []))


def cmd_init(
    reconfigure: bool = False,
    config_path: str = "",
    skip_api_enable: bool = False,
    skip_quota_check: bool = False,
    dry_run: bool = False,
) -> None:
    """Run the full init flow: wizard → preflight → GCP setup → config."""
    print_banner()

    if dry_run:
        console.print("  [bold yellow]DRY RUN[/bold yellow] — no changes will be made\n")

    cfg = InstallerConfig()

    # --- If config exists and not reconfiguring, ask ---
    if cfg.exists() and not reconfigure and not config_path:
        console.print("  [yellow]Configuration already exists.[/yellow]")
        if not confirm("Reconfigure?", default=False):
            console.print("\n  Using existing config. Run [bold]voipbin-install apply[/bold] to deploy.")
            return

    # --- Step 1: Preflight checks ---
    results = check_prerequisites()
    all_ok = run_preflight_display(results)
    if not all_ok:
        # Pass 1: auto-installable tools — attempt each in sequence
        for r in results:
            if not r.ok and _can_auto_run(r.tool):
                installed = offer_tool_install(r.tool)
                if not installed:
                    sys.exit(1)

        # Pass 2: display-only tools — show ALL hints before exiting
        display_only_missing = [r for r in results if not r.ok and not _can_auto_run(r.tool)]
        if display_only_missing:
            for r in display_only_missing:
                offer_tool_install(r.tool)  # always returns False; prints print_fix block
            sys.exit(1)
        # All tools now installed — continue

    # --- Step 2: GCP auth ---
    print_header("Checking GCP credentials...")
    account = check_gcp_auth()
    if not account:
        print_error("Not authenticated with gcloud. Run: gcloud auth login")
        sys.exit(1)
    print_success(f"Authenticated as {account}")

    # Check ADC (separate from gcloud user auth)
    adc_ok, _ = check_application_default_credentials()
    if not adc_ok:
        refreshed = offer_adc_setup()
        if not refreshed:
            sys.exit(1)
    print_success("Application Default Credentials valid")

    # --- Step 3: Wizard (or load from file) ---
    if config_path:
        cfg = InstallerConfig()
        cfg._dir = __import__("pathlib").Path(config_path).parent
        cfg.load()
        wizard_values = cfg.to_dict()
    else:
        existing = None
        if cfg.exists():
            cfg.load()
            existing = cfg.to_dict()
        wizard_values = run_wizard(existing_config=existing)
        if wizard_values is None:
            sys.exit(0)

    cfg.set_many(wizard_values)
    cfg.apply_defaults()

    project_id = cfg.get("gcp_project_id")
    region = cfg.get("region")

    # --- Step 4: Validate project and billing ---
    print_header("Validating GCP project...")
    if not check_gcp_project(project_id):
        print_error(f"Cannot access project '{project_id}'. Check the project ID and your permissions.")
        sys.exit(1)
    print_success(f"Project: {project_id}")

    if not check_gcp_billing(project_id):
        print_error(f"Billing is not enabled on project '{project_id}'.")
        print_error("Enable it at: https://console.cloud.google.com/billing")
        sys.exit(1)
    print_success("Billing enabled")

    # Static-IP quota: non-fatal warning. The redesign reserves 5
    # regional EXTERNAL static IPs per install (one per externally
    # exposed Service). gcloud-reported quota can lag actual quota,
    # so this is advisory only.
    if not check_static_ip_quota(project_id, region, needed=5):
        print_warning(
            f"Region {region} may not have 5 free STATIC_ADDRESSES slots. "
            f"Request a quota increase before deploying: "
            f"gcloud compute regions describe {region} --project {project_id}"
        )

    # --- Step 5: Quota check ---
    if not skip_quota_check:
        quota_results = check_quotas(project_id, region)
        quotas_ok = display_quota_results(quota_results, project_id)
        if not quotas_ok:
            print_warning("Some quotas are insufficient. Deployment may fail.")
            print_warning("Consider requesting increases before running 'apply'.")
    else:
        print_step("[dim]Skipping quota check (--skip-quota-check)[/dim]")

    # --- Dry run stops here: show what would be done ---
    if dry_run:
        console.print()
        print_result_box([
            "[yellow]DRY RUN — the following actions would be performed:[/yellow]",
            "",
            f"  1. Enable {_count_gcp_apis()} GCP APIs on project {project_id}",
            f"  2. Create service account: voipbin-installer@{project_id}.iam.gserviceaccount.com",
            f"  3. Create KMS key ring in {region} for SOPS encryption",
            f"  4. Generate 6 secrets (jwt_key, cloudsql_password, redis_password,",
            f"     rabbitmq_user, rabbitmq_password, api_signing_key)",
            f"  5. Encrypt secrets.yaml with SOPS + GCP KMS",
            f"  6. Write config.yaml and .sops.yaml",
            "",
            f"  Project:  {project_id}",
            f"  Region:   {region}",
            f"  Domain:   {cfg.get('domain')}",
            f"  GKE:      {cfg.get('gke_type')}",
            f"  TLS:      {cfg.get('tls_strategy')}",
            "",
            "  No files written. No GCP resources created.",
            "  Run without --dry-run to proceed.",
        ], style="yellow")

        console.print()
        print_cost_table(cfg.get("gke_type", "zonal"))
        return

    # --- Step 6: Enable APIs ---
    if not skip_api_enable:
        print_header("Enabling GCP APIs...")
        with create_progress() as progress:
            task = progress.add_task("Enabling APIs...", total=_count_gcp_apis())

            def on_api(api_name: str) -> None:
                progress.update(task, advance=1, description=f"Enabling {api_name}...")

            succeeded, failed = enable_apis(project_id, progress_callback=on_api)

        print_success(f"{len(succeeded)} APIs enabled")
        if failed:
            print_warning(f"{len(failed)} APIs failed: {', '.join(failed)}")
            print_warning("Re-run init or enable them manually.")
    else:
        print_step("[dim]Skipping API enablement (--skip-api-enable)[/dim]")

    # --- Step 7: Service account ---
    print_header("Creating installer service account...")
    sa_email = create_service_account(project_id)
    if sa_email:
        print_success(f"Service account: {sa_email}")
    else:
        print_warning("Could not create service account. Check IAM permissions.")

    # --- Step 8: KMS ---
    print_header("Creating KMS key ring...")
    kms_key_id = create_kms_keyring(project_id)
    if kms_key_id:
        print_success("KMS key ring and crypto key ready")
    else:
        print_error("Failed to create KMS key. Check permissions.")
        sys.exit(1)

    # --- Step 9: Secrets ---
    print_header("Generating secrets...")
    ok, secrets_dict = generate_and_encrypt(
        kms_key_id, cfg.secrets_path, domain=cfg.get("domain", "")
    )
    if ok:
        for name in secrets_dict:
            print_success(f"{name}")
        print_success("Secrets encrypted with SOPS")
    else:
        print_error("SOPS encryption failed. Cannot proceed without encrypted secrets.")
        print_error(f"  Fix SOPS/KMS and re-run, or encrypt manually:")
        print_error(f"  sops --encrypt --in-place --gcp-kms {kms_key_id} {cfg.secrets_path}")
        sys.exit(1)

    # --- Step 10: Write .sops.yaml ---
    write_sops_config(kms_key_id, cfg._dir)

    # --- Step 11: Save config ---
    cfg.save()

    # --- Step 12: Summary ---
    console.print()
    print_cost_table(cfg.get("gke_type", "zonal"))
    console.print()

    print_result_box([
        "[green]✓ Configuration saved[/green]",
        f"  config.yaml   (non-sensitive)",
        f"  secrets.yaml  (SOPS-encrypted)",
        "",
        f"  Project:  {project_id}",
        f"  Region:   {region}",
        f"  Domain:   {cfg.get('domain')}",
        f"  GKE:      {cfg.get('gke_type')}",
        f"  TLS:      {cfg.get('tls_strategy')}",
        "",
        "  Next: [bold]./voipbin-install apply[/bold]",
    ])
