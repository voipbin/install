"""Terraform state reconciliation for VoIPBin installer.

Detects GCP resources that exist outside Terraform state and imports them
before terraform apply runs, making deployments resumable without 409 errors.
"""

import ipaddress
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from rich.table import Table

from scripts.config import InstallerConfig
from scripts.display import confirm, console, print_error, print_step, print_success, print_warning
from scripts.state_bucket import state_bucket_name
from scripts.terraform import TERRAFORM_DIR, terraform_state_list
from scripts.utils import _validate_cmd_arg, run_cmd


# Covers "NOT FOUND", "NOT_FOUND" (gcloud style), "notfound", "404 Not Found", etc.
_NOT_FOUND_PHRASES = ("not found", "notfound", "not_found", "does not exist", "404", "no such")


def _always_valid(v: Any) -> bool:
    return True


# GCS bucket naming rules (simple form): lowercase letters, digits, dots,
# hyphens, underscores; must start/end with alphanumeric; length 3..63.
# See: https://cloud.google.com/storage/docs/buckets#naming
_GCS_BUCKET_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{1,61}[a-z0-9]$")


def _is_valid_bucket_name(v: Any) -> bool:
    return isinstance(v, str) and bool(_GCS_BUCKET_RE.match(v))


def _is_valid_ipv4_address(v: Any) -> bool:
    """Validate an IPv4 address literal.

    Cloud SQL Private IP on Google VPC peering is always IPv4. Rejecting
    non-IPv4 strings (including the PR #5a sentinel `cloudsql-private.invalid`
    and PR-A empty scaffolding) early prevents bad values from reaching
    config.yaml.
    """
    if not isinstance(v, str) or not v:
        return False
    try:
        ipaddress.IPv4Address(v)
        return True
    except (ValueError, ipaddress.AddressValueError):
        return False


def _is_valid_ipv4_cidr(v: Any) -> bool:
    """Validate an IPv4 CIDR (e.g. ``10.126.80.0/20``).

    Requires an explicit prefix length. Rejects bare addresses and IPv6.
    """
    if not isinstance(v, str) or not v:
        return False
    if "/" not in v:
        return False
    try:
        ipaddress.IPv4Network(v, strict=False)
        return True
    except (ValueError, ipaddress.AddressValueError, ipaddress.NetmaskValueError):
        return False


def check_exists_in_gcp(check_cmd: list[str]) -> tuple[bool, bool]:
    """Check whether a GCP resource exists.

    Returns:
        (exists, check_succeeded): exists=True if the resource is present.
        check_succeeded=False means the check could not be completed (e.g.
        permission error) — callers should treat the resource as a potential
        conflict and include it in the import prompt.
    """
    result = run_cmd(check_cmd, capture=True, timeout=30)
    if result.returncode == 0:
        return True, True
    stderr_lower = (result.stderr or "").lower()
    if any(phrase in stderr_lower for phrase in _NOT_FOUND_PHRASES):
        return False, True
    return False, False


def import_resource(
    tf_address: str,
    import_id: str,
    project_id: str,
) -> tuple[bool, str]:
    """Run `terraform import` for a single resource.

    Returns:
        (success, error_message): error_message is empty on success.
    """
    _validate_cmd_arg(project_id, "project_id")
    _validate_cmd_arg(tf_address, "tf_address")
    _validate_cmd_arg(import_id, "import_id")
    cmd = [
        "terraform", "import", "-no-color",
        "-var", f"project_id={project_id}",
        tf_address,
        import_id,
    ]
    result = run_cmd(cmd, capture=True, timeout=120, cwd=TERRAFORM_DIR)
    return result.returncode == 0, (result.stderr or "").strip()


def build_registry(config: InstallerConfig) -> list[dict[str, Any]]:
    """Build the list of GCP resources to check and import if needed.

    Entries are ordered by dependency (key ring before crypto key, etc.).
    Each entry has:
        tf_address   — Terraform resource address
        description  — human-readable name for display
        gcloud_check — gcloud command list; exit 0 = exists
        import_id    — ID string passed to `terraform import`
    """
    project = config.get("gcp_project_id")
    region = config.get("region")
    zone = config.get("zone")
    kamailio_count = config.get("kamailio_count", 1)
    rtpengine_count = config.get("rtpengine_count", 1)

    entries: list[dict[str, Any]] = []

    # -- Service accounts ------------------------------------------------
    # NOTE (PR #5a): `sa_cloudsql_proxy` intentionally omitted. The
    # Terraform module still defines the SA; PR #5b will remove it. Until
    # then, reconcile silently ignores its presence in GCP — terraform
    # apply will keep it in sync via the existing module declaration.
    sa_specs = [
        ("sa_gke_nodes",      "sa-voipbin-gke-nodes",      "GKE Node Pool SA"),
        ("sa_kamailio",       "sa-voipbin-kamailio",        "Kamailio VM SA"),
        ("sa_rtpengine",      "sa-voipbin-rtpengine",       "RTPEngine VM SA"),
    ]
    for tf_name, sa_id, desc in sa_specs:
        email = f"{sa_id}@{project}.iam.gserviceaccount.com"
        entries.append({
            "tf_address":   f"google_service_account.{tf_name}",
            "description":  desc,
            "gcloud_check": ["gcloud", "iam", "service-accounts", "describe", email, f"--project={project}"],
            "import_id":    f"projects/{project}/serviceAccounts/{email}",
        })

    # -- KMS (key ring must come before crypto key) ----------------------
    entries.append({
        "tf_address":   "google_kms_key_ring.voipbin_sops",
        "description":  "KMS Key Ring",
        "gcloud_check": ["gcloud", "kms", "keyrings", "describe", "voipbin-sops",
                         "--location=global", f"--project={project}"],
        "import_id":    f"projects/{project}/locations/global/keyRings/voipbin-sops",
    })
    entries.append({
        "tf_address":   "google_kms_crypto_key.voipbin_sops_key",
        "description":  "KMS Crypto Key",
        "gcloud_check": ["gcloud", "kms", "keys", "describe", "voipbin-sops-key",
                         "--keyring=voipbin-sops", "--location=global", f"--project={project}"],
        "import_id":    f"projects/{project}/locations/global/keyRings/voipbin-sops/cryptoKeys/voipbin-sops-key",
    })

    # -- Network ---------------------------------------------------------
    entries.append({
        "tf_address":   "google_compute_network.voipbin",
        "description":  "VPC Network",
        "gcloud_check": ["gcloud", "compute", "networks", "describe", "voipbin-vpc",
                         f"--project={project}"],
        "import_id":    f"projects/{project}/global/networks/voipbin-vpc",
    })
    entries.append({
        "tf_address":   "google_compute_subnetwork.voipbin_main",
        "description":  "VPC Subnetwork",
        "gcloud_check": ["gcloud", "compute", "networks", "subnets", "describe", "voipbin-main",
                         f"--region={region}", f"--project={project}"],
        "import_id":    f"projects/{project}/regions/{region}/subnetworks/voipbin-main",
    })
    entries.append({
        "tf_address":   "google_compute_router.voipbin",
        "description":  "Cloud Router",
        "gcloud_check": ["gcloud", "compute", "routers", "describe", "voipbin-router",
                         f"--region={region}", f"--project={project}"],
        "import_id":    f"projects/{project}/regions/{region}/routers/voipbin-router",
    })
    entries.append({
        "tf_address":   "google_compute_router_nat.voipbin",
        "description":  "Cloud NAT",
        "gcloud_check": ["gcloud", "compute", "routers", "nats", "describe", "voipbin-nat",
                         "--router=voipbin-router", f"--region={region}", f"--project={project}"],
        "import_id":    f"{project}/{region}/voipbin-router/voipbin-nat",
    })

    # -- Firewall rules --------------------------------------------------
    fw_rules = [
        ("fw_allow_internal",    "voipbin-fw-allow-internal",    "Firewall: internal"),
        ("fw_gke_internal",      "voipbin-fw-gke-internal",      "Firewall: GKE internal"),
        ("fw_healthcheck",       "voipbin-fw-healthcheck",       "Firewall: health check"),
        ("fw_iap_ssh",           "voipbin-fw-iap-ssh",           "Firewall: IAP SSH"),
        ("fw_kamailio_sip",      "voipbin-fw-kamailio-sip",      "Firewall: Kamailio SIP"),
        ("fw_rtpengine_control", "voipbin-fw-rtpengine-control", "Firewall: RTPEngine control"),
        ("fw_rtpengine_rtp",     "voipbin-fw-rtpengine-rtp",     "Firewall: RTPEngine RTP"),
        ("fw_vm_to_infra",       "voipbin-fw-vm-to-infra",       "Firewall: VM to infra"),
    ]
    for tf_name, gcp_name, desc in fw_rules:
        entries.append({
            "tf_address":   f"google_compute_firewall.{tf_name}",
            "description":  desc,
            "gcloud_check": ["gcloud", "compute", "firewall-rules", "describe", gcp_name,
                             f"--project={project}"],
            "import_id":    f"projects/{project}/global/firewalls/{gcp_name}",
        })

    # -- Compute addresses -----------------------------------------------
    for tf_name, gcp_name, desc in [
        ("nat_ip",               "voipbin-nat-ip",               "NAT Static IP"),
        ("kamailio_lb_external", "voipbin-kamailio-lb-external", "Kamailio LB IP (ext)"),
        ("kamailio_lb_internal", "voipbin-kamailio-lb-internal", "Kamailio LB IP (int)"),
    ]:
        entries.append({
            "tf_address":   f"google_compute_address.{tf_name}",
            "description":  desc,
            "gcloud_check": ["gcloud", "compute", "addresses", "describe", gcp_name,
                             f"--region={region}", f"--project={project}"],
            "import_id":    f"projects/{project}/regions/{region}/addresses/{gcp_name}",
        })
    for i in range(rtpengine_count):
        gcp_name = f"external-ip-rtpengine-voipbin-{zone}-{i}"
        entries.append({
            "tf_address":   f"google_compute_address.rtpengine[{i}]",
            "description":  f"RTPEngine IP [{i}]",
            "gcloud_check": ["gcloud", "compute", "addresses", "describe", gcp_name,
                             f"--region={region}", f"--project={project}"],
            "import_id":    f"projects/{project}/regions/{region}/addresses/{gcp_name}",
        })

    # -- External service static IPs (PR #2 reservations) ---------------
    # These five regional EXTERNAL addresses back the api-manager,
    # hook-manager, admin, talk, and meet Service annotations. PR #2
    # introduced the for_each but did not register them here; PR-B
    # closes GAP-10 so partial-apply failures can be resumed.
    for key in ("api-manager", "hook-manager", "admin", "talk", "meet"):
        gcp_name = f"{key}-static-ip"
        entries.append({
            "tf_address":   f'google_compute_address.external_service["{key}"]',
            "description":  f"External Service Static IP ({key})",
            "gcloud_check": ["gcloud", "compute", "addresses", "describe", gcp_name,
                             f"--region={region}", f"--project={project}"],
            "import_id":    f"projects/{project}/regions/{region}/addresses/{gcp_name}",
        })

    # -- VPC peering reserved range (PR-B) ------------------------------
    # google_service_networking_connection.voipbin is intentionally NOT
    # registered: `gcloud services vpc-peerings list` returns rc=0 with
    # empty stdout on absence, which would false-positive the rc==0
    # heuristic in check_exists_in_gcp. See design §2.4 / §4.
    entries.append({
        "tf_address":   "google_compute_global_address.cloudsql_peering",
        "description":  "VPC Peering Reserved Range",
        "gcloud_check": ["gcloud", "compute", "addresses", "describe", "voipbin-cloudsql-peering",
                         "--global", f"--project={project}"],
        "import_id":    f"projects/{project}/global/addresses/voipbin-cloudsql-peering",
    })

    # -- Health checks ---------------------------------------------------
    entries.append({
        "tf_address":   "google_compute_http_health_check.kamailio_external",
        "description":  "HTTP Health Check (ext)",
        "gcloud_check": ["gcloud", "compute", "http-health-checks", "describe",
                         "voipbin-hc-kamailio-external", f"--project={project}"],
        "import_id":    f"projects/{project}/global/httpHealthChecks/voipbin-hc-kamailio-external",
    })
    entries.append({
        "tf_address":   "google_compute_health_check.kamailio_internal",
        "description":  "Health Check (int)",
        "gcloud_check": ["gcloud", "compute", "health-checks", "describe",
                         "voipbin-hc-kamailio-internal", f"--project={project}"],
        "import_id":    f"projects/{project}/global/healthChecks/voipbin-hc-kamailio-internal",
    })

    # -- Load balancer resources -----------------------------------------
    entries.append({
        "tf_address":   "google_compute_target_pool.kamailio",
        "description":  "Target Pool (Kamailio)",
        "gcloud_check": ["gcloud", "compute", "target-pools", "describe",
                         "voipbin-pool-kamailio", f"--region={region}", f"--project={project}"],
        "import_id":    f"{project}/{region}/voipbin-pool-kamailio",
    })
    entries.append({
        "tf_address":   "google_compute_region_backend_service.kamailio_internal",
        "description":  "Backend Service (internal)",
        "gcloud_check": ["gcloud", "compute", "backend-services", "describe",
                         "voipbin-bs-kamailio-internal", f"--region={region}", f"--project={project}"],
        "import_id":    f"projects/{project}/regions/{region}/backendServices/voipbin-bs-kamailio-internal",
    })
    for tf_name, gcp_name, desc in [
        ("kamailio_internal", "voipbin-kamailio-fwd-internal", "Forwarding Rule (internal)"),
        ("kamailio_tcp_sip",  "voipbin-kamailio-fwd-tcp-sip",  "Forwarding Rule (TCP SIP)"),
        ("kamailio_tcp_wss",  "voipbin-kamailio-fwd-tcp-wss",  "Forwarding Rule (TCP WSS)"),
        ("kamailio_udp_sip",  "voipbin-kamailio-fwd-udp-sip",  "Forwarding Rule (UDP SIP)"),
    ]:
        entries.append({
            "tf_address":   f"google_compute_forwarding_rule.{tf_name}",
            "description":  desc,
            "gcloud_check": ["gcloud", "compute", "forwarding-rules", "describe", gcp_name,
                             f"--region={region}", f"--project={project}"],
            "import_id":    f"projects/{project}/regions/{region}/forwardingRules/{gcp_name}",
        })

    # -- Compute instances and instance group ----------------------------
    entries.append({
        "tf_address":   "google_compute_instance_group.kamailio",
        "description":  "Instance Group (Kamailio)",
        "gcloud_check": ["gcloud", "compute", "instance-groups", "unmanaged", "describe",
                         f"voipbin-ig-kamailio-{zone}", f"--zone={zone}", f"--project={project}"],
        "import_id":    f"{zone}/voipbin-ig-kamailio-{zone}",
    })
    for i in range(kamailio_count):
        gcp_name = f"instance-kamailio-voipbin-{zone}-{i}"
        entries.append({
            "tf_address":   f"google_compute_instance.kamailio[{i}]",
            "description":  f"Kamailio VM [{i}]",
            "gcloud_check": ["gcloud", "compute", "instances", "describe", gcp_name,
                             f"--zone={zone}", f"--project={project}"],
            "import_id":    f"{project}/{zone}/{gcp_name}",
        })
    for i in range(rtpengine_count):
        gcp_name = f"instance-rtpengine-voipbin-{zone}-{i}"
        entries.append({
            "tf_address":   f"google_compute_instance.rtpengine[{i}]",
            "description":  f"RTPEngine VM [{i}]",
            "gcloud_check": ["gcloud", "compute", "instances", "describe", gcp_name,
                             f"--zone={zone}", f"--project={project}"],
            "import_id":    f"{project}/{zone}/{gcp_name}",
        })

    # -- GCS buckets (media first, then state bucket) -------------------
    entries.append({
        "tf_address":   "google_storage_bucket.media",
        "description":  "GCS Media Bucket",
        "gcloud_check": ["gcloud", "storage", "buckets", "describe",
                         f"gs://{project}-voipbin-media", f"--project={project}"],
        "import_id":    f"{project}-voipbin-media",
    })
    entries.append({
        "tf_address":   "google_storage_bucket.terraform_state",
        "description":  "GCS TF State Bucket",
        "gcloud_check": ["gcloud", "storage", "buckets", "describe",
                         f"gs://{state_bucket_name(config)}", f"--project={project}"],
        "import_id":    state_bucket_name(config),
    })
    entries.append({
        "tf_address":   "google_storage_bucket.recordings",
        "description":  "GCS Recordings Bucket",
        "gcloud_check": ["gcloud", "storage", "buckets", "describe",
                         f"gs://{config.get('env')}-voipbin-recordings",
                         f"--project={project}"],
        "import_id":    f"{config.get('env')}-voipbin-recordings",
    })
    entries.append({
        "tf_address":   "google_storage_bucket.tmp",
        "description":  "GCS Tmp Bucket",
        "gcloud_check": ["gcloud", "storage", "buckets", "describe",
                         f"gs://{config.get('env')}-voipbin-tmp",
                         f"--project={project}"],
        "import_id":    f"{config.get('env')}-voipbin-tmp",
    })

    # -- Cloud SQL (instance first, then database and user) -------------
    entries.append({
        "tf_address":   "google_sql_database_instance.voipbin",
        "description":  "Cloud SQL Instance",
        "gcloud_check": ["gcloud", "sql", "instances", "describe", "voipbin-mysql",
                         f"--project={project}"],
        "import_id":    f"projects/{project}/instances/voipbin-mysql",
    })
    entries.append({
        "tf_address":   "google_sql_database.voipbin",
        "description":  "Cloud SQL Database",
        "gcloud_check": ["gcloud", "sql", "databases", "describe", "voipbin",
                         "--instance=voipbin-mysql", f"--project={project}"],
        "import_id":    f"projects/{project}/instances/voipbin-mysql/databases/voipbin",
    })
    entries.append({
        "tf_address":   "google_sql_user.voipbin",
        "description":  "Cloud SQL User",
        "gcloud_check": ["gcloud", "sql", "users", "list", "--instance=voipbin-mysql",
                         "--filter=name=voipbin", f"--project={project}"],
        "import_id":    f"{project}/voipbin-mysql/voipbin",
    })

    # -- GKE (cluster must come before node pool) -----------------------
    entries.append({
        "tf_address":   "google_container_cluster.voipbin",
        "description":  "GKE Cluster",
        "gcloud_check": ["gcloud", "container", "clusters", "describe", "voipbin-gke-cluster",
                         f"--zone={zone}", f"--project={project}"],
        "import_id":    f"projects/{project}/locations/{zone}/clusters/voipbin-gke-cluster",
    })
    entries.append({
        "tf_address":   "google_container_node_pool.voipbin",
        "description":  "GKE Node Pool",
        "gcloud_check": ["gcloud", "container", "node-pools", "describe", "voipbin-node-pool",
                         "--cluster=voipbin-gke-cluster", f"--zone={zone}", f"--project={project}"],
        "import_id":    f"projects/{project}/locations/{zone}/clusters/voipbin-gke-cluster/nodePools/voipbin-node-pool",
    })

    return entries


def imports(config: InstallerConfig) -> bool:
    """Detect GCP resources missing from Terraform state and import them.

    Returns True if the pipeline may proceed (no conflicts, or all imports
    succeeded). Returns False if the user declined or any import failed.
    """
    project_id = config.get("gcp_project_id")
    if not project_id:
        print_error("gcp_project_id is not configured — cannot run reconcile")
        return False
    registry = build_registry(config)
    in_state = terraform_state_list(config)

    # Filter to resources not yet in state
    candidates = [e for e in registry if e["tf_address"] not in in_state]
    if not candidates:
        return True

    # Check which candidates exist in GCP.
    # Resources whose gcloud check fails (permission error, API unavailable) are
    # treated as potential conflicts and included in the import prompt — otherwise
    # they silently pass through here and cause 409 errors in terraform apply.
    # Trade-off: on a fresh install where GCP APIs are not yet enabled, all checks
    # may fail, causing all registry entries to be offered for import. Those imports
    # will fail harmlessly (resource not found), reconcile returns False, and the
    # user must re-run once APIs are enabled. This is preferable to the silent-skip
    # path which masks real 409 conflicts on resume deployments.
    conflicts: list[dict] = []

    for entry in candidates:
        exists, check_ok = check_exists_in_gcp(entry["gcloud_check"])
        if not check_ok:
            conflicts.append({**entry, "unverified": True})
        elif exists:
            conflicts.append(entry)

    if not conflicts:
        return True

    # Display conflict table
    verified_count = sum(1 for e in conflicts if not e.get("unverified"))
    unverified_count = len(conflicts) - verified_count
    if verified_count:
        print_warning(f"{verified_count} resources exist in GCP but are missing from Terraform state")
    if unverified_count:
        print_warning(f"{unverified_count} resources could not be verified — will attempt import")
    table = Table(show_header=True, header_style="bold", box=None, padding=(0, 2))
    table.add_column("#",                style="dim", width=4)
    table.add_column("Terraform Address",              min_width=45)
    table.add_column("Description",                    min_width=30)
    table.add_column("Status",                         width=14)
    console.print()
    for i, entry in enumerate(conflicts, 1):
        status = "[yellow]unverified[/yellow]" if entry.get("unverified") else "[green]exists[/green]"
        table.add_row(str(i), entry["tf_address"], entry["description"], status)
    console.print(table)
    console.print()

    if not confirm("Import all into Terraform state and continue?", default=True):
        print_step("Pipeline halted. Re-run [bold]voipbin-install apply[/bold] after resolving conflicts manually.")
        return False

    # Import each conflict
    successes, failures = [], []
    for entry in conflicts:
        print_step(f"↺ Importing [dim]{entry['tf_address']}[/dim]...")
        ok, err = import_resource(entry["tf_address"], entry["import_id"], project_id)
        if ok:
            print_success(f"Imported {entry['tf_address']}")
            successes.append(entry)
        else:
            print_error(f"Import failed: {err}")
            failures.append((entry, err))

    console.print()
    print_step(f"Summary: {len(successes)} imported, {len(failures)} failed")

    if failures:
        print_error("Import failed for:")
        for entry, err in failures:
            tf_addr = entry["tf_address"]
            import_id = entry["import_id"]
            note = " [dim]# unverified — confirm resource exists before importing[/dim]" if entry.get("unverified") else ""
            console.print(f"    [dim]Run manually:[/dim] terraform import -var project_id={project_id} {tf_addr} {import_id}{note}")
        return False

    return True


# Backward-compatible alias. PR-A renamed `reconcile` → `imports`; the alias
# keeps existing imports (e.g. `tests/test_terraform_reconcile.py`) working.
# Bind-time only — monkeypatching `imports` does NOT update `reconcile`.
reconcile = imports


@dataclass(frozen=True)
class TfOutputFieldMapping:
    """Mapping from a Terraform output key to a config.yaml field.

    Used by `outputs()` to auto-populate select config slots from
    `terraform output` after apply. PRs C/D/G append entries; PR-A ships empty.
    """
    tf_key: str
    cfg_key: str
    validator: Callable[[Any], bool] = _always_valid


# PRs C/D append further entries; PR-G adds the GCS bucket fields below.
FIELD_MAP: list[TfOutputFieldMapping] = [
    TfOutputFieldMapping(
        tf_key="recordings_bucket_name",
        cfg_key="recordings_bucket",
        validator=_is_valid_bucket_name,
    ),
    TfOutputFieldMapping(
        tf_key="tmp_bucket_name",
        cfg_key="tmp_bucket",
        validator=_is_valid_bucket_name,
    ),
    TfOutputFieldMapping(
        tf_key="cloudsql_mysql_private_ip",
        cfg_key="cloudsql_private_ip",
        validator=_is_valid_ipv4_address,
    ),
    TfOutputFieldMapping(
        tf_key="cloudsql_peering_range_cidr",
        cfg_key="cloudsql_private_ip_cidr",
        validator=_is_valid_ipv4_cidr,
    ),
]


def outputs(config: InstallerConfig, tf_outputs: dict[str, Any]) -> bool:
    """Auto-populate select config.yaml fields from Terraform outputs.

    Runs AFTER `terraform_apply`. Reads `tf_outputs` (already collected by the
    pipeline) and writes mapped values into `config.yaml`, skipping any field
    the operator has already set. Returns True on success.
    """
    if not FIELD_MAP:
        print_step("[dim]No outputs to populate (no fields registered yet).[/dim]")
        return True
    # PR-E: import at function scope to avoid an unconditional dependency on
    # scripts.preflight at module import time (preflight imports from diagnosis).
    from scripts.preflight import CLOUDSQL_PRIVATE_IP_SENTINEL

    changed = False
    for mapping in FIELD_MAP:
        value = tf_outputs.get(mapping.tf_key)
        if value is None or value == "":
            continue
        if not mapping.validator(value):
            print_warning(f"Invalid output for {mapping.tf_key}: {value!r}; skipping.")
            continue
        # PR-E: overwrite when the current value is the sentinel
        # (operator upgraded from a previous installer that wrote a
        # `cloudsql-private.invalid` default into config.yaml).
        current = config.get(mapping.cfg_key)
        if not current or current == CLOUDSQL_PRIVATE_IP_SENTINEL:
            config.set(mapping.cfg_key, value)
            changed = True
    if changed:
        config.save()
        print_success("Updated config.yaml from Terraform outputs.")
    else:
        print_step("[dim]All output-derived config fields already set; no changes.[/dim]")
    return True
