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
    2. Render manifests via kustomize with placeholder substitution
    3. Apply the rendered manifests via ``kubectl apply -f -``
    """
    if not k8s_get_credentials(config, terraform_outputs):
        return False

    print_step("Rendering manifests with secrets and config values...")
    ok, rendered, _unresolved = _render_manifests(config, terraform_outputs)
    if not ok:
        return False

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
