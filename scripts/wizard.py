"""Interactive setup wizard for VoIPBin installer."""

import re
from typing import Any, Optional

from config.defaults import (
    DNS_MODES,
    GKE_TYPES,
    IMAGE_TAG_STRATEGIES,
    REGIONS,
    TLS_STRATEGIES,
)
from scripts.display import (
    console,
    print_header,
    prompt_choice,
    prompt_text,
)
from scripts.gcp import get_project_id, list_active_projects


def _validate_domain(value: str) -> Optional[str]:
    """Validate domain name. Returns error message or None."""
    if not value:
        return "Domain name is required"
    if value.startswith("http://") or value.startswith("https://"):
        return "Enter domain name only, without http:// or https://"
    if not re.match(r"^[a-z0-9][a-z0-9.\-]+[a-z0-9]$", value):
        return "Invalid domain. Use lowercase letters, numbers, dots, and hyphens."
    if "." not in value:
        return "Domain must contain at least one dot (e.g., voipbin.example.com)"
    return None


def _validate_project_id(value: str) -> Optional[str]:
    """Validate GCP project ID format."""
    if not value:
        return "Project ID is required"
    if len(value) < 6:
        return "Project ID must be at least 6 characters"
    if not re.match(r"^[a-z][a-z0-9\-]+[a-z0-9]$", value):
        return "Invalid project ID. Use lowercase letters, numbers, and hyphens."
    return None


def _validate_custom_region(value: str) -> Optional[str]:
    """Validate a custom region name."""
    if not value:
        return "Region is required"
    if not re.match(r"^[a-z]+-[a-z]+\d+$", value):
        return "Invalid region format. Example: us-central1, europe-west4"
    return None


def _validate_cert_manual_dir(value: str) -> Optional[str]:
    """PR-Z: validate manual-mode cert directory path.

    Checks only that the path exists and is a directory. The fine-grained
    per-SAN layout check (fullchain.pem + privkey.pem under each SAN
    subdirectory) is performed by cert_lifecycle._validate_manual_cert_dir
    at apply time, not here.
    """
    import os
    if not value:
        return "Path is required for cert_mode=manual"
    if not os.path.isdir(value):
        return f"Directory does not exist: {value}"
    return None


def derive_zone(region: str, gke_type: str) -> str:
    """Derive default zone from region. For zonal, append '-a'."""
    if gke_type == "regional":
        return region
    return f"{region}-a"


def run_wizard(existing_config: Optional[dict[str, Any]] = None) -> Optional[dict[str, Any]]:
    """Run the interactive 7-question setup wizard.

    Returns the config dict, or None if the user cancels (Ctrl+C).
    """
    config: dict[str, Any] = {}
    defaults = existing_config or {}

    try:
        # --- Q1: GCP Project ID ---
        print_header("1. GCP Project ID")
        detected = get_project_id()
        default_project = defaults.get("gcp_project_id") or detected or ""

        # PR-V: Try to list visible ACTIVE projects and offer a numbered
        # picker. On empty list (no permission / gcloud failure), fall
        # through to the original text-prompt path so the wizard remains
        # usable on restricted-IAM hosts.
        # Print a progress hint because the per-account billing fetch can
        # take ~20s per billing account in the worst case (iter-2 nit #1).
        console.print("[dim]      Fetching GCP project list...[/dim]")
        listings = list_active_projects()
        project_id = ""
        if listings:
            options = []
            default_idx = 1
            for i, lp in enumerate(listings, 1):
                if lp.billing_enabled is True:
                    billing_str = "billing: yes"
                elif lp.billing_enabled is False:
                    billing_str = "billing: no"
                else:
                    billing_str = "billing: unknown"
                # `*` marker reflects the EFFECTIVE numeric default; aligned
                # with default_idx (NOT raw `detected`) per iter-1 I5.
                marker = " *" if lp.project_id == default_project else ""
                options.append({
                    "id": lp.project_id,
                    "name": f"{lp.project_id}{marker}",
                    "note": f"{lp.name} ({billing_str})" if lp.name else billing_str,
                })
                if lp.project_id == default_project:
                    default_idx = i
            options.append({
                "id": "__manual__",
                "name": "Enter manually...",
                "note": "type a project ID not in the list above",
            })
            choice_idx = prompt_choice(
                "Select your GCP project",
                options,
                default=default_idx,
            )
            # Read back the selected option's id so a renamed sentinel
            # (`__manual` vs `__manual__`) is observable in tests
            # (iter-1 I3 → mutant #6 catchable).
            selected_id = options[choice_idx - 1]["id"]
            if selected_id != "__manual__":
                project_id = selected_id

        # Manual entry fallback: empty list OR operator chose "Enter manually..."
        if not project_id:
            if detected:
                console.print(f"      [dim]Detected: {detected}[/dim]")
            project_id = prompt_text(
                "Enter your GCP project ID",
                default=default_project,
                validate_fn=_validate_project_id,
            )
        config["gcp_project_id"] = project_id

        # --- Q2: Region ---
        print_header("2. GCP Region")
        default_region_idx = 1
        if defaults.get("region"):
            for i, r in enumerate(REGIONS, 1):
                if r["id"] == defaults["region"]:
                    default_region_idx = i
                    break
        region_idx = prompt_choice("Select a region for deployment:", REGIONS, default=default_region_idx)

        if REGIONS[region_idx - 1]["id"] == "custom":
            region = prompt_text("Enter region", validate_fn=_validate_custom_region)
        else:
            region = REGIONS[region_idx - 1]["id"]
        config["region"] = region

        # --- Q3: GKE Cluster Type ---
        print_header("3. GKE Cluster Type")
        default_gke = 1 if defaults.get("gke_type") != "regional" else 2
        gke_idx = prompt_choice("Select GKE cluster type:", GKE_TYPES, default=default_gke)
        config["gke_type"] = GKE_TYPES[gke_idx - 1]["id"]

        # Derive zone
        config["zone"] = derive_zone(region, config["gke_type"])

        # --- Q4: TLS Strategy ---
        print_header("4. TLS Certificate Strategy")
        default_tls = 1
        if defaults.get("tls_strategy"):
            for i, t in enumerate(TLS_STRATEGIES, 1):
                if t["id"] == defaults["tls_strategy"]:
                    default_tls = i
                    break
        tls_idx = prompt_choice("Select TLS certificate strategy:", TLS_STRATEGIES, default=default_tls)
        config["tls_strategy"] = TLS_STRATEGIES[tls_idx - 1]["id"]

        # --- Q5: Docker Image Tags ---
        print_header("5. Docker Image Tags")
        default_img = 2 if defaults.get("image_tag_strategy") != "latest" else 1
        img_idx = prompt_choice("Select Docker image version strategy:", IMAGE_TAG_STRATEGIES, default=default_img)
        config["image_tag_strategy"] = IMAGE_TAG_STRATEGIES[img_idx - 1]["id"]

        # --- Q6: Domain Name ---
        print_header("6. Domain Name")
        domain = prompt_text(
            "Enter your domain (e.g., voipbin.example.com)",
            default=defaults.get("domain", ""),
            validate_fn=_validate_domain,
        )
        config["domain"] = domain

        # --- Q6b (PR-Z): Kamailio TLS Cert Mode ---
        print_header("6b. Kamailio TLS Certificate Mode")
        cert_options = [
            {"id": "self_signed", "name": "self_signed",
             "note": "installer generates a CA + per-SAN leaves (default)"},
            {"id": "manual", "name": "manual",
             "note": "operator supplies fullchain.pem + privkey.pem per SAN"},
        ]
        default_cert = 1 if defaults.get("cert_mode") != "manual" else 2
        cert_idx = prompt_choice(
            "Certificate mode for Kamailio TLS (self_signed/manual)?",
            cert_options,
            default=default_cert,
        )
        config["cert_mode"] = cert_options[cert_idx - 1]["id"]
        if config["cert_mode"] == "manual":
            manual_dir = prompt_text(
                "Path to manual cert directory",
                default=defaults.get("cert_manual_dir", "") or "",
                validate_fn=_validate_cert_manual_dir,
            )
            config["cert_manual_dir"] = manual_dir
        else:
            config["cert_manual_dir"] = None

        # --- Q7: Cloud DNS ---
        print_header("7. Cloud DNS")
        default_dns = 1 if defaults.get("dns_mode") != "manual" else 2
        dns_idx = prompt_choice(
            "Should the installer manage DNS via Cloud DNS?",
            DNS_MODES,
            default=default_dns,
        )
        config["dns_mode"] = DNS_MODES[dns_idx - 1]["id"]

        return config

    except (KeyboardInterrupt, EOFError):
        console.print("\n\n  [yellow]Setup cancelled.[/yellow]")
        return None
