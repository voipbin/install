"""Kubernetes operations for VoIPBin installer."""

import json
import os
import subprocess
import time
from typing import Any

from scripts.config import InstallerConfig
from scripts.display import console, print_error, print_step, print_success, print_warning
from scripts.secret_schema import BIN_SECRET_KEYS, VOIP_SECRET_KEYS
from scripts.secretmgr import decrypt_with_sops
from scripts.utils import INSTALLER_DIR, run_cmd


K8S_DIR = INSTALLER_DIR / "k8s"


# PR-R: Canonical (namespace, service, output-key) tuples for the 5 k8s
# LoadBalancer Services whose externalIPs Kamailio's env.j2 consumes.
# Note. asterisk-{call,registrar,conference} each have separate TCP and UDP
# Services with DISTINCT internal LB IPs (live-verified on dogfood
# voipbin-install-dev cluster, May 2026). Kamailio's env.j2 has one slot per
# component (ASTERISK_*_LB_ADDR); SIP dispatch uses UDP, so we harvest the
# UDP variant for each. The TCP IPs are allocated by GCP but currently
# unused by Kamailio.
#
# Service-name suffixes (-tcp / -udp) were added to the asterisk Helm
# charts as part of the protocol split. PR-T1 brought _LB_SERVICES into
# parity with the live chart after a smoke uncovered drift: PR-R initially
# encoded asterisk-{registrar,conference} without the -udp suffix and
# harvest_loadbalancer_ips() polled non-existent Services until timeout.
_LB_SERVICES: list[tuple[str, str, str]] = [
    ("infrastructure", "redis", "redis_lb_ip"),
    ("infrastructure", "rabbitmq", "rabbitmq_lb_ip"),
    ("infrastructure", "heplify-udp", "heplify_lb_ip"),
    ("voip", "asterisk-call-udp", "asterisk_call_lb_ip"),
    ("voip", "asterisk-registrar-udp", "asterisk_registrar_lb_ip"),
    ("voip", "asterisk-conference-udp", "asterisk_conference_lb_ip"),
]


def _get_service_external_ip(namespace: str, name: str) -> str:
    """Run `kubectl get svc <name> -n <ns> -o json` and parse externalIP.

    Returns "" on any failure path (kubectl non-zero, malformed JSON,
    pending LB with status=null, ingress=[], or ingress[0].ip absent).
    Never raises so the harvest poll loop is robust to GKE in-flight states.
    """
    result = subprocess.run(
        ["kubectl", "get", "svc", name, "-n", namespace, "-o", "json"],
        capture_output=True, text=True, timeout=30,
    )
    if result.returncode != 0:
        return ""
    try:
        data = json.loads(result.stdout)
        status = (data or {}).get("status") or {}
        load_balancer = (status or {}).get("loadBalancer") or {}
        ingress = (load_balancer or {}).get("ingress") or []
        if ingress and isinstance(ingress, list):
            first = ingress[0] or {}
            ip = first.get("ip", "") if isinstance(first, dict) else ""
            return ip if isinstance(ip, str) else ""
    except (json.JSONDecodeError, AttributeError, TypeError, IndexError):
        pass
    return ""


def harvest_loadbalancer_ips(
    timeout_seconds: int | None = None,
    poll_interval: int = 5,
) -> dict[str, str]:
    """Poll kubectl until each known LB Service has a non-empty externalIP.

    Returns dict {canonical_key: ip}. Best-effort: missing keys after timeout
    are simply omitted from the result; a warning is emitted per missing
    service so the operator knows what to rerun. Timeout default reads
    VOIPBIN_LB_HARVEST_TIMEOUT_SECONDS env var (default 300s).
    """
    if timeout_seconds is None:
        timeout_seconds = int(
            os.environ.get("VOIPBIN_LB_HARVEST_TIMEOUT_SECONDS", "300")
        )
    deadline = time.monotonic() + timeout_seconds
    result: dict[str, str] = {}
    pending = list(_LB_SERVICES)
    while pending and time.monotonic() < deadline:
        for entry in list(pending):
            ns, svc, key = entry
            ip = _get_service_external_ip(ns, svc)
            if ip:
                result[key] = ip
                pending.remove(entry)
        if pending and time.monotonic() < deadline:
            time.sleep(poll_interval)
    for ns, svc, key in pending:
        print_warning(
            f"LB Service {ns}/{svc} did not receive an externalIP within "
            f"{timeout_seconds}s. Downstream consumers will see empty {key}. "
            f"Self-diagnose with `kubectl get svc -n {ns} {svc}`; common "
            f"causes: GCP quota exhausted, subnet purpose mismatch, missing "
            f"`cloud.google.com/load-balancer-type: Internal` annotation. "
            f"Rerun via `voipbin-install apply --stage reconcile_k8s_outputs`."
        )
    return result


def _render_manifests_substitution(text: str, subs: dict) -> str:
    """Public-test entry point: mirrors `_render_manifests` substitution loop.

    Imported by PR-D2b tests to drive the real production sort/replace logic.
    Sort longest-first so that nested placeholders (e.g. a DSN containing
    both an IP and a password placeholder, or two placeholders sharing a
    prefix like `*_PRIVATE_IP` vs `*_PRIVATE_IP_CIDR`) resolve correctly.
    """
    for token in sorted(subs, key=len, reverse=True):
        text = text.replace(token, str(subs[token]))
    return text


def _build_substitution_map(
    config: InstallerConfig,
    terraform_outputs: dict[str, Any],
    secrets: dict[str, str],
) -> dict[str, str]:
    """Build a mapping of PLACEHOLDER_* tokens to actual deployment values.

    Defaults are sourced from ``scripts/secret_schema.py``. The operator's
    sops-decrypted ``secrets.yaml`` values override the defaults so any key
    can be customized without editing manifests.
    """
    domain = config.get("domain", "")
    project_id = config.get("gcp_project_id", "")
    region = config.get("region", "")
    kamailio_lb_address = config.get("kamailio_internal_lb_address", "")
    kamailio_lb_name = config.get("kamailio_internal_lb_name", "kamailio-internal-lb")
    cloudsql_private_ip = config.get("cloudsql_private_ip", "")
    cloudsql_private_ip_cidr = config.get("cloudsql_private_ip_cidr", "") or (
        f"{cloudsql_private_ip}/32" if cloudsql_private_ip else ""
    )

    # Seed: PLACEHOLDER_<KEY> -> default value from secret_schema.
    subs: dict[str, str] = {}
    for key, meta in BIN_SECRET_KEYS.items():
        subs[f"PLACEHOLDER_{key}"] = str(meta["default"])
    for key, meta in VOIP_SECRET_KEYS.items():
        # Same token namespace; voip dupes (RABBITMQ_ADDRESS etc.) collapse
        # cleanly because the values match by design.
        subs[f"PLACEHOLDER_{key}"] = str(meta["default"])

    # Override schema defaults from sops secrets.yaml (when present).
    # Keys are expected to be UPPER_SNAKE matching the Secret keys exactly.
    for key, value in secrets.items():
        if value is None:
            continue
        token = f"PLACEHOLDER_{key}"
        subs[token] = str(value)

    # Top-level config / infra tokens (independent of secret schema).
    subs.update({
        "PLACEHOLDER_DOMAIN": domain,
        "PLACEHOLDER_PROJECT_ID": project_id,
        "PLACEHOLDER_REGION": region,
        "PLACEHOLDER_ACME_EMAIL": f"admin@{domain}" if domain else "",
        "PLACEHOLDER_KAMAILIO_INTERNAL_LB_ADDRESS": kamailio_lb_address
            or subs.get("PLACEHOLDER_KAMAILIO_INTERNAL_LB_ADDRESS", ""),
        "PLACEHOLDER_KAMAILIO_INTERNAL_LB_NAME": kamailio_lb_name,
        # Cloud SQL private IPs. PR-D2a/D2b:
        # - `PLACEHOLDER_CLOUDSQL_PRIVATE_IP[_CIDR]` retained as MySQL alias
        #   for k8s/voip/secret.yaml and k8s/network-policies/*.
        # - `PLACEHOLDER_CLOUDSQL_MYSQL_PRIVATE_IP` is the new canonical key
        #   consumed by k8s/backend/secret.yaml DSN strings (PR-D2b rewrite).
        # - `PLACEHOLDER_CLOUDSQL_POSTGRES_PRIVATE_IP` consumed by the
        #   DATABASE_DSN_POSTGRES line.
        "PLACEHOLDER_CLOUDSQL_PRIVATE_IP": cloudsql_private_ip,
        "PLACEHOLDER_CLOUDSQL_PRIVATE_IP_CIDR": cloudsql_private_ip_cidr,
        "PLACEHOLDER_CLOUDSQL_MYSQL_PRIVATE_IP": cloudsql_private_ip,
        "PLACEHOLDER_CLOUDSQL_POSTGRES_PRIVATE_IP": config.get(
            "cloudsql_postgres_private_ip", ""
        ),
        # PR-D2b: Cloud SQL application user passwords. Sourced from terraform
        # outputs (sensitive=true on the terraform side).
        "PLACEHOLDER_DSN_PASSWORD_MYSQL_BIN_MANAGER": terraform_outputs.get(
            "cloudsql_mysql_password_bin_manager", ""
        ),
        "PLACEHOLDER_DSN_PASSWORD_MYSQL_ASTERISK": terraform_outputs.get(
            "cloudsql_mysql_password_asterisk", ""
        ),
        "PLACEHOLDER_DSN_PASSWORD_MYSQL_CALL_MANAGER": terraform_outputs.get(
            "cloudsql_mysql_password_call_manager", ""
        ),
        "PLACEHOLDER_DSN_PASSWORD_MYSQL_KAMAILIORO": terraform_outputs.get(
            "cloudsql_mysql_password_kamailioro", ""
        ),
        "PLACEHOLDER_DSN_PASSWORD_POSTGRES_BIN_MANAGER": terraform_outputs.get(
            "cloudsql_postgres_password_bin_manager", ""
        ),
        # PR-U-2: HOMER (heplify-server + homer-app) database credentials.
        # heplify-server writes capture rows to `homer_data`; homer-app reads
        # dashboards/config from `homer_config`. Both DBs live on the existing
        # CloudSQL Postgres instance (terraform/cloudsql.tf:145), owned by a
        # dedicated `homer` Postgres user provisioned by Terraform. The user
        # name is the literal string "homer" (PR-U-2 locked decision); the
        # password is the Terraform output `cloudsql_postgres_password_homer`,
        # harvested into terraform_outputs by reconcile_outputs.
        #
        # Preflight (scripts/preflight.py:check_homer_credentials_present)
        # asserts the password is non-empty when k8s/infrastructure/homer/
        # exists, so an empty value here cannot silently CrashLoop a freshly-
        # applied Pod.
        "PLACEHOLDER_HOMER_DB_USER": "homer",
        "PLACEHOLDER_HOMER_DB_PASS": terraform_outputs.get(
            "cloudsql_postgres_password_homer", ""
        ),
        # Terraform outputs.
        # RabbitMQ broker bootstrap credentials. Default user/pass is
        # `guest`/`guest` to match production. Operator may override via
        # config.yaml rabbitmq_user / secrets.yaml rabbitmq_password.
        "PLACEHOLDER_RABBITMQ_USER": config.get("rabbitmq_user", "guest"),
        "PLACEHOLDER_RABBITMQ_PASSWORD": secrets.get("rabbitmq_password", "guest"),
        "PLACEHOLDER_RECORDINGS_BUCKET": terraform_outputs.get(
            "recordings_bucket_name", config.get("recordings_bucket", "")
        ),
        "PLACEHOLDER_TMP_BUCKET": terraform_outputs.get(
            "tmp_bucket_name", config.get("tmp_bucket", "")
        ),
        # External Service static IPs (PR #10/#11).
        "PLACEHOLDER_STATIC_IP_NAME_API_MANAGER": terraform_outputs.get(
            "api_manager_static_ip_name", "api-manager-static-ip"
        ),
        "PLACEHOLDER_STATIC_IP_NAME_HOOK_MANAGER": terraform_outputs.get(
            "hook_manager_static_ip_name", "hook-manager-static-ip"
        ),
        "PLACEHOLDER_STATIC_IP_NAME_ADMIN": terraform_outputs.get(
            "admin_static_ip_name", "admin-static-ip"
        ),
        "PLACEHOLDER_STATIC_IP_NAME_TALK": terraform_outputs.get(
            "talk_static_ip_name", "talk-static-ip"
        ),
        "PLACEHOLDER_STATIC_IP_NAME_MEET": terraform_outputs.get(
            "meet_static_ip_name", "meet-static-ip"
        ),
        "PLACEHOLDER_STATIC_IP_ADDRESS_API_MANAGER": terraform_outputs.get(
            "api_manager_static_ip_address", ""
        ),
        "PLACEHOLDER_STATIC_IP_ADDRESS_HOOK_MANAGER": terraform_outputs.get(
            "hook_manager_static_ip_address", ""
        ),
        "PLACEHOLDER_STATIC_IP_ADDRESS_ADMIN": terraform_outputs.get(
            "admin_static_ip_address", ""
        ),
        "PLACEHOLDER_STATIC_IP_ADDRESS_TALK": terraform_outputs.get(
            "talk_static_ip_address", ""
        ),
        "PLACEHOLDER_STATIC_IP_ADDRESS_MEET": terraform_outputs.get(
            "meet_static_ip_address", ""
        ),
    })

    return subs


def _render_manifests(
    config: InstallerConfig,
    terraform_outputs: dict[str, Any],
) -> tuple[bool, str, int]:
    """Render kustomize manifests with all placeholders substituted.

    Decrypts secrets.yaml, builds the substitution map, renders via
    ``kubectl kustomize``, and replaces every PLACEHOLDER_* token.

    Returns (success, rendered_yaml, unresolved_placeholder_count).
    """
    # 1. Decrypt secrets
    secrets_path = config.secrets_path
    secrets: dict[str, str] = {}
    if secrets_path.exists():
        decrypted = decrypt_with_sops(secrets_path)
        if decrypted is None:
            print_error("Failed to decrypt secrets.yaml — cannot substitute K8s placeholders")
            return False, "", 0
        secrets = {k: str(v) for k, v in decrypted.items()}
    else:
        print_warning("secrets.yaml not found — K8s manifests will have empty secret values")

    # 2. Build substitution map
    subs = _build_substitution_map(config, terraform_outputs, secrets)

    # 3. Render via kustomize
    result = run_cmd(["kubectl", "kustomize", str(K8S_DIR)], capture=True, timeout=120)
    if result.returncode != 0:
        print_error(f"kubectl kustomize failed:\n{result.stderr}")
        return False, "", 0
    rendered = result.stdout

    # 4. Substitute — delegate to _render_manifests_substitution (single
    # source of truth, tested directly in PR-D2b).
    rendered = _render_manifests_substitution(rendered, subs)

    # 5. Warn about any remaining placeholders
    remaining = [
        line.strip()
        for line in rendered.splitlines()
        if "PLACEHOLDER_" in line
    ]
    if remaining:
        print_warning(f"{len(remaining)} unresolved PLACEHOLDER_ values remain:")
        for line in remaining[:5]:
            print_step(f"  [dim]{line}[/dim]")

    return True, rendered, len(remaining)


def k8s_get_credentials(
    config: InstallerConfig,
    terraform_outputs: dict[str, Any],
) -> bool:
    """Fetch GKE credentials via gcloud. Returns True on success."""
    cluster_name = terraform_outputs.get("gke_cluster_name", "")
    zone = config.get("zone", "")
    project_id = config.get("gcp_project_id", "")
    if not cluster_name:
        print_error("GKE cluster name not found in Terraform outputs")
        return False
    cmd = [
        "gcloud", "container", "clusters", "get-credentials", cluster_name,
        "--zone", zone, "--project", project_id,
    ]
    print_step(f"Fetching credentials for cluster: {cluster_name}")
    result = run_cmd(cmd, capture=True, timeout=120)
    if result.returncode != 0:
        print_error(f"Failed to get GKE credentials:\n{result.stderr}")
        return False
    print_success(f"Kubectl context set for {cluster_name}")
    return True


def k8s_apply(
    config: InstallerConfig,
    terraform_outputs: dict[str, Any],
) -> bool:
    """Apply Kubernetes manifests with secrets/config substituted.

    PR #4 simplification: tls_bootstrap.py now seeds ``secrets.yaml`` at
    ``init`` time, so by the time we render here the four SSL_*_BASE64
    keys and JWT_KEY are already in the sops file. Apply is a single
    ``kubectl apply`` — no post-apply patching or rollout restarts.
    """
    from scripts.preflight import (
        PreflightError,
        check_homer_credentials_present,
        check_loadbalancer_addresses,
        check_nodeport_availability,
    )

    if not k8s_get_credentials(config, terraform_outputs):
        return False

    # LoadBalancer ADDRESS preflight (hard fail)
    tls_strategy = config.get("tls_strategy", "self-signed")
    if tls_strategy != "byoc":
        missing = check_loadbalancer_addresses(terraform_outputs)
        if missing:
            print_error(
                "LoadBalancer ADDRESS outputs missing from Terraform "
                "state: " + ", ".join(missing)
            )
            print_error(
                "Run `terraform apply` in install/terraform/ and ensure "
                "the static IP resources are created before re-running."
            )
            return False

    # PR-U-2: HOMER Postgres credentials preflight (hard fail).
    # No-op when k8s/infrastructure/homer/ is absent (custom install profiles).
    try:
        check_homer_credentials_present(terraform_outputs)
    except PreflightError as exc:
        print_error(str(exc))
        return False

    print_step("Rendering manifests with secrets and config values...")
    ok, rendered, _unresolved = _render_manifests(config, terraform_outputs)
    if not ok:
        return False

    # NodePort preflight (non-fatal warning)
    if not check_nodeport_availability(needed=7):
        print_warning(
            "Cluster may have fewer than 7 free NodePort slots in "
            "30000-32767. LB Service creation may stall. Free unused "
            "NodePort Services or expand the range."
        )

    print_step("Running: kubectl apply -f - (rendered manifests)")
    result = subprocess.run(
        ["kubectl", "apply", "-f", "-"],
        input=rendered,
        capture_output=True,
        text=True,
        timeout=600,
    )
    if result.returncode != 0:
        print_error(f"kubectl apply failed:\n{result.stderr}")
        return False
    if result.stdout:
        _print_apply_summary(result.stdout)
    print_success("Kubernetes manifests applied")
    return True


def k8s_dry_run(
    config: InstallerConfig,
    terraform_outputs: dict[str, Any],
) -> bool:
    """Validate K8s manifests without applying.

    Renders manifests with placeholder substitution and runs
    ``kubectl apply --dry-run=client`` to validate them.
    Does not require a live cluster connection.
    """
    print_step("Rendering manifests with secrets and config values...")
    ok, rendered, unresolved = _render_manifests(config, terraform_outputs)
    if not ok:
        return False

    resource_count = sum(
        1 for line in rendered.splitlines() if line.startswith("kind:")
    )
    if unresolved:
        print_warning(f"Manifests rendered: {resource_count} resources, {unresolved} unresolved placeholders")
    else:
        print_success(f"Manifests rendered: {resource_count} resources, all placeholders resolved")

    # Validate with client-side dry-run (no cluster needed)
    print_step("Running: kubectl apply --dry-run=client -f - (validation only)")
    result = subprocess.run(
        ["kubectl", "apply", "--dry-run=client", "-f", "-"],
        input=rendered,
        capture_output=True,
        text=True,
        timeout=120,
    )
    if result.returncode != 0:
        print_error(f"Manifest validation failed:\n{result.stderr}")
        return False
    if result.stdout:
        _print_apply_summary(result.stdout)
    print_success("Manifests validated (dry-run=client)")
    return True


def _print_apply_summary(stdout: str) -> None:
    """Summarize kubectl apply output counts."""
    counts: dict[str, int] = {}
    for line in stdout.strip().splitlines():
        for action in ("created", "configured", "unchanged"):
            if action in line:
                counts[action] = counts.get(action, 0) + 1
    parts = [f"{count} {action}" for action, count in counts.items()]
    if parts:
        print_step(f"  Resources: {', '.join(parts)}")


def k8s_status(config: InstallerConfig) -> dict[str, Any]:
    """Check pod statuses. Returns a summary dict."""
    cmd = ["kubectl", "get", "pods", "--all-namespaces", "-o", "json"]
    result = run_cmd(cmd, capture=True, timeout=60)
    if result.returncode != 0:
        return {"error": result.stderr.strip(), "pods": []}
    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError:
        return {"error": "Failed to parse kubectl output", "pods": []}
    pods = []
    for item in data.get("items", []):
        metadata = item.get("metadata", {})
        status = item.get("status", {})
        phase = status.get("phase", "Unknown")
        pods.append({
            "namespace": metadata.get("namespace", ""),
            "name": metadata.get("name", ""),
            "phase": phase,
        })
    summary = _compute_pod_summary(pods)
    return {"pods": pods, "summary": summary}


def _compute_pod_summary(pods: list[dict[str, str]]) -> dict[str, int]:
    """Count pods by phase."""
    summary: dict[str, int] = {}
    for pod in pods:
        phase = pod.get("phase", "Unknown")
        summary[phase] = summary.get(phase, 0) + 1
    return summary


def k8s_delete(config: InstallerConfig) -> bool:
    """Delete Kubernetes resources via kustomize. Returns True on success."""
    cmd = ["kubectl", "delete", "-k", str(K8S_DIR), "--ignore-not-found"]
    print_step("Running: kubectl delete -k k8s/")
    result = run_cmd(cmd, capture=True, timeout=600)
    if result.returncode != 0:
        # kubectl delete can partially succeed; only hard-fail on unexpected errors
        stderr = result.stderr.strip()
        if "NotFound" in stderr or "not found" in stderr:
            print_warning("Some resources already deleted")
        else:
            print_error(f"kubectl delete failed:\n{stderr}")
            return False
    print_success("Kubernetes resources deleted")
    return True


def k8s_cluster_status(config: InstallerConfig) -> dict[str, str]:
    """Return basic GKE cluster info or error."""
    project_id = config.get("gcp_project_id", "")
    zone = config.get("zone", "")
    cmd = [
        "gcloud", "container", "clusters", "list",
        "--project", project_id, "--zone", zone, "--format=json",
    ]
    result = run_cmd(cmd, capture=True, timeout=60)
    if result.returncode != 0:
        return {"status": "error", "message": result.stderr.strip()}
    try:
        clusters = json.loads(result.stdout)
    except json.JSONDecodeError:
        return {"status": "error", "message": "Failed to parse gcloud output"}
    if not clusters:
        return {"status": "not_found"}
    cluster = clusters[0]
    return {
        "status": cluster.get("status", "UNKNOWN"),
        "name": cluster.get("name", ""),
        "node_count": str(cluster.get("currentNodeCount", 0)),
        "version": cluster.get("currentMasterVersion", ""),
    }
