"""Kubernetes operations for VoIPBin installer."""

import json
import subprocess
from typing import Any

from scripts.config import InstallerConfig
from scripts.display import console, print_error, print_step, print_success, print_warning
from scripts.secretmgr import decrypt_with_sops
from scripts.utils import INSTALLER_DIR, run_cmd


K8S_DIR = INSTALLER_DIR / "k8s"


def _build_substitution_map(
    config: InstallerConfig,
    terraform_outputs: dict[str, Any],
    secrets: dict[str, str],
) -> dict[str, str]:
    """Build a mapping of PLACEHOLDER_* tokens to actual deployment values."""
    domain = config.get("domain", "")
    project_id = config.get("gcp_project_id", "")
    region = config.get("region", "")
    rabbitmq_user = secrets.get("rabbitmq_user", "voipbin")
    rabbitmq_password = secrets.get("rabbitmq_password", "")
    redis_password = secrets.get("redis_password", "")

    return {
        # Secrets
        "PLACEHOLDER_JWT_KEY": secrets.get("jwt_key", ""),
        "PLACEHOLDER_DB_USER": "root",
        "PLACEHOLDER_DB_PASSWORD": secrets.get("cloudsql_password", ""),
        "PLACEHOLDER_REDIS_PASSWORD": redis_password,
        "PLACEHOLDER_RABBITMQ_USER": rabbitmq_user,
        "PLACEHOLDER_RABBITMQ_PASSWORD": rabbitmq_password,
        "PLACEHOLDER_API_SIGNING_KEY": secrets.get("api_signing_key", ""),
        # Config
        "PLACEHOLDER_DOMAIN": domain,
        "PLACEHOLDER_PROJECT_ID": project_id,
        "PLACEHOLDER_REGION": region,
        "PLACEHOLDER_DB_NAME": "voipbin",
        "PLACEHOLDER_ACME_EMAIL": f"admin@{domain}",
        # Terraform outputs
        "PLACEHOLDER_INSTANCE_NAME": terraform_outputs.get(
            "cloudsql_instance_name", "voipbin-mysql"
        ),
        "PLACEHOLDER_CLOUDSQL_SA": terraform_outputs.get(
            "cloudsql_proxy_sa_name", "voipbin-cloudsql-proxy"
        ),
        "PLACEHOLDER_RECORDING_BUCKET_NAME": terraform_outputs.get(
            "recording_bucket_name", f"{project_id}-voipbin-recordings"
        ),
        # External Service static IPs.
        # NAME tokens (PR #10) kept for backward-compat; not consumed by
        # manifests in PR #3a. ADDRESS tokens are bound to
        # `Service.spec.loadBalancerIP` and must resolve to an IPv4
        # literal — empty string is acceptable as it produces
        # `loadBalancerIP:` (null) and the preflight in init.py raises
        # before manifests reach GCP if any ADDRESS is empty.
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
        # Derived composite values
        "PLACEHOLDER_RABBITMQ_ADDRESS": (
            f"amqp://{rabbitmq_user}:{rabbitmq_password}"
            "@rabbitmq.infrastructure.svc.cluster.local:5672/"
        ),
        "PLACEHOLDER_REDIS_ADDRESS": (
            f"redis://:{redis_password}"
            "@redis.infrastructure.svc.cluster.local:6379/0"
        ),
    }


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

    # 4. Substitute — process longest tokens first to avoid partial matches
    for token in sorted(subs, key=len, reverse=True):
        rendered = rendered.replace(token, subs[token])

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

    Steps:
    1. Fetch GKE credentials
    2. Preflight LoadBalancer ADDRESS tokens (hard fail if any empty
       except when `tls_strategy == "byoc"`)
    3. Render manifests via kustomize with placeholder substitution
    4. Preflight NodePort capacity (warning only)
    5. Apply rendered manifests via ``kubectl apply -f -`` (this
       creates the bin-manager namespace and the voipbin-secret with
       empty SSL_*_BASE64 fields, plus 4 LoadBalancer Services in
       Pending state pending the cert).
    6. Bootstrap voipbin-tls + patch voipbin-secret with SSL_CERT/
       PRIVKEY base64 (idempotent + atomic-pair contract; see
       scripts/tls_bootstrap.py). Apply happens BEFORE bootstrap so
       the Secret exists for the patch step.
    7. Rolling-restart the Pods that consume the patched cert so the
       new SSL env values take effect.
    """
    from scripts.preflight import (
        check_loadbalancer_addresses,
        check_nodeport_availability,
    )
    from scripts.tls_bootstrap import BootstrapError, bootstrap_voipbin_tls_secret

    if not k8s_get_credentials(config, terraform_outputs):
        return False

    # 2. LoadBalancer ADDRESS preflight (hard fail)
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

    print_step("Rendering manifests with secrets and config values...")
    ok, rendered, _unresolved = _render_manifests(config, terraform_outputs)
    if not ok:
        return False

    # 4. NodePort preflight (non-fatal warning)
    if not check_nodeport_availability(needed=4):
        print_warning(
            "Cluster may have fewer than 4 free NodePort slots in "
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

    # 6. TLS bootstrap (after manifest apply so voipbin-secret exists).
    # When tls_strategy == "byoc" the operator supplies the cert into
    # voipbin-secret manually; bootstrap detects populated keys and
    # leaves them untouched (atomic-pair contract case 2).
    domain = config.get("domain", "")
    hostnames = [f"{h}.{domain}" if domain else h for h in
                 ("api", "hook", "admin", "talk", "meet")]
    try:
        bootstrap_result = bootstrap_voipbin_tls_secret(hostnames=hostnames)
    except BootstrapError as exc:
        print_error(f"TLS bootstrap failed: {exc}")
        return False
    except Exception as exc:  # noqa: BLE001 — surface unexpected errors
        print_error(f"TLS bootstrap raised unexpected error: {exc}")
        return False

    # 7. If we patched the Secret, restart consumers so they pick up
    # the new env values. (Pods started by step 5 may have come up
    # with empty SSL env vars — TLS listener would have failed.)
    if bootstrap_result.voipbin_secret_action == "patched":
        print_step("Rolling-restart api-manager and hook-manager to pick up new TLS env...")
        for deploy in ("api-manager", "hook-manager"):
            r = subprocess.run(
                ["kubectl", "-n", "bin-manager", "rollout", "restart", f"deployment/{deploy}"],
                capture_output=True, text=True, timeout=60,
            )
            if r.returncode != 0:
                # Deployment may not exist yet in some unusual states;
                # warn but don't fail the whole apply.
                print_warning(
                    f"rollout restart {deploy} returned non-zero: {r.stderr.strip()}"
                )

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
