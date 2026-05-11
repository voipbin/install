"""Post-install verification checks for VoIPBin deployment."""

import socket
import time
from typing import Any, Optional

from scripts.utils import run_cmd


def _make_result(name: str, status: str, message: str, duration_ms: int) -> dict:
    """Build a standardised check result dict."""
    return {
        "name": name,
        "status": status,
        "message": message,
        "duration_ms": duration_ms,
    }


def _timed(fn):
    """Call *fn* and return (result, elapsed_ms). *fn* should return (status, message)."""
    start = time.monotonic()
    try:
        status, message = fn()
    except Exception as exc:
        status, message = "fail", str(exc)
    elapsed = int((time.monotonic() - start) * 1000)
    return status, message, elapsed


# ---------------------------------------------------------------------------
# Individual checks
# ---------------------------------------------------------------------------

def check_gke_cluster(project_id: str, zone: str, cluster_name: str) -> dict:
    """Check that the GKE cluster is in RUNNING state."""

    def _check():
        cmd = (
            f"gcloud container clusters describe {cluster_name}"
            f" --project {project_id} --zone {zone}"
            f" --format='value(status)'"
        )
        result = run_cmd(cmd, timeout=30)
        if result.returncode != 0:
            return "fail", f"gcloud error: {result.stderr.strip()}"
        status_val = result.stdout.strip()
        if status_val == "RUNNING":
            return "pass", "RUNNING"
        return "fail", f"status={status_val}"

    status, message, elapsed = _timed(_check)
    return _make_result(f"GKE cluster {cluster_name}", status, message, elapsed)


def check_pods_ready(namespace: str) -> dict:
    """Check that all pods in *namespace* are Running and Ready."""

    def _check():
        cmd = (
            f"kubectl get pods -n {namespace}"
            " --no-headers"
            " -o custom-columns='NAME:.metadata.name,READY:.status.conditions[?(@.type==\"Ready\")].status,PHASE:.status.phase'"
        )
        result = run_cmd(cmd, timeout=30)
        if result.returncode != 0:
            return "fail", f"kubectl error: {result.stderr.strip()}"
        lines = [l for l in result.stdout.strip().splitlines() if l.strip()]
        if not lines:
            return "warn", "no pods found"
        total = len(lines)
        ready = 0
        for line in lines:
            parts = line.split()
            if len(parts) >= 3 and parts[1] == "True" and parts[2] == "Running":
                ready += 1
        if ready == total:
            return "pass", f"{ready}/{total} Ready"
        return "fail", f"{ready}/{total} Ready"

    status, message, elapsed = _timed(_check)
    return _make_result(f"Namespace {namespace} pods", status, message, elapsed)


def check_services_endpoints(namespace: str) -> dict:
    """Check that services in *namespace* have at least one endpoint."""

    def _check():
        svc_cmd = (
            f"kubectl get svc -n {namespace} --no-headers"
            " -o custom-columns='NAME:.metadata.name'"
        )
        svc_result = run_cmd(svc_cmd, timeout=30)
        if svc_result.returncode != 0:
            return "fail", f"kubectl error: {svc_result.stderr.strip()}"
        services = [s.strip() for s in svc_result.stdout.strip().splitlines() if s.strip()]
        if not services:
            return "warn", "no services found"
        no_endpoints = []
        for svc in services:
            ep_cmd = (
                f"kubectl get endpoints {svc} -n {namespace}"
                " --no-headers -o custom-columns='ENDPOINTS:.subsets[*].addresses[*].ip'"
            )
            ep_result = run_cmd(ep_cmd, timeout=15)
            if ep_result.returncode != 0 or not ep_result.stdout.strip() or ep_result.stdout.strip() == "<none>":
                no_endpoints.append(svc)
        if no_endpoints:
            return "warn", f"{len(no_endpoints)} service(s) missing endpoints: {', '.join(no_endpoints)}"
        return "pass", f"{len(services)} service(s) have endpoints"

    status, message, elapsed = _timed(_check)
    return _make_result(f"Namespace {namespace} endpoints", status, message, elapsed)


def check_vms_running(project_id: str, zone: str, prefix: str) -> dict:
    """Check that VMs whose names start with *prefix* are RUNNING."""

    def _check():
        cmd = (
            f"gcloud compute instances list --project {project_id}"
            f" --filter='name~^{prefix} AND zone:{zone}'"
            " --format='value(name,status)'"
        )
        result = run_cmd(cmd, timeout=30)
        if result.returncode != 0:
            return "fail", f"gcloud error: {result.stderr.strip()}"
        lines = [l for l in result.stdout.strip().splitlines() if l.strip()]
        if not lines:
            return "warn", "no VMs found"
        total = len(lines)
        running = sum(1 for l in lines if "RUNNING" in l)
        if running == total:
            return "pass", f"{running}/{total} RUNNING"
        return "fail", f"{running}/{total} RUNNING"

    status, message, elapsed = _timed(_check)
    return _make_result(f"{prefix} VMs", status, message, elapsed)


def check_cloudsql_running(project_id: str, instance_name: str) -> dict:
    """Check that the Cloud SQL instance is RUNNABLE."""

    def _check():
        cmd = (
            f"gcloud sql instances describe {instance_name}"
            f" --project {project_id}"
            " --format='value(state)'"
        )
        result = run_cmd(cmd, timeout=30)
        if result.returncode != 0:
            return "fail", f"gcloud error: {result.stderr.strip()}"
        state = result.stdout.strip()
        if state == "RUNNABLE":
            return "pass", "RUNNABLE"
        return "fail", f"state={state}"

    status, message, elapsed = _timed(_check)
    return _make_result("Cloud SQL", status, message, elapsed)


def check_dns_resolution(domain: str) -> dict:
    """Check that *domain* resolves via DNS."""

    def _check():
        try:
            results = socket.getaddrinfo(domain, None)
            if results:
                addr = results[0][4][0]
                return "pass", f"resolves to {addr}"
            return "fail", "no results"
        except socket.gaierror as exc:
            return "fail", f"DNS lookup failed: {exc}"

    status, message, elapsed = _timed(_check)
    return _make_result(f"DNS {domain}", status, message, elapsed)


def check_http_health(url: str, expected_status: int = 200) -> dict:
    """HTTP health check against *url*."""

    def _check():
        cmd = f"curl -sf -o /dev/null -w '%{{http_code}}' --max-time 5 '{url}'"
        result = run_cmd(cmd, timeout=10)
        if result.returncode != 0:
            stderr = result.stderr.strip()
            if "timed out" in stderr.lower() or "timeout" in stderr.lower():
                return "warn", "timeout"
            return "fail", f"curl error (rc={result.returncode}): {stderr}"
        code = result.stdout.strip()
        try:
            code_int = int(code)
        except ValueError:
            return "fail", f"unexpected response: {code}"
        if code_int == expected_status:
            return "pass", f"HTTP {code_int}"
        return "fail", f"HTTP {code_int} (expected {expected_status})"

    status, message, elapsed = _timed(_check)
    return _make_result(f"HTTP {url}", status, message, elapsed)


def check_sip_port(host: str, port: int = 5060) -> dict:
    """Check that SIP port is open via TCP socket connect."""

    def _check():
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(5)
        try:
            err = sock.connect_ex((host, port))
            if err == 0:
                return "pass", "open"
            return "fail", f"connection refused (errno={err})"
        except socket.timeout:
            return "warn", "timeout"
        except socket.gaierror as exc:
            return "fail", f"DNS error: {exc}"
        except OSError as exc:
            return "fail", str(exc)
        finally:
            sock.close()

    status, message, elapsed = _timed(_check)
    return _make_result(f"SIP {host}:{port}", status, message, elapsed)


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------

def run_all_checks(
    config: dict[str, Any],
    terraform_outputs: Optional[dict[str, Any]] = None,
) -> list[dict]:
    """Run all verification checks and return a list of result dicts.

    *config* is the installer config dict.
    *terraform_outputs* is an optional dict of Terraform output values.
    """
    tf = terraform_outputs or {}
    project_id = config.get("gcp_project_id", "")
    zone = config.get("zone", "")
    domain = config.get("domain", "")
    cluster_name = tf.get("gke_cluster_name", "voipbin-cluster")
    sql_instance = tf.get("cloudsql_instance_name", "voipbin-mysql")

    results: list[dict] = []

    # GKE cluster
    results.append(check_gke_cluster(project_id, zone, cluster_name))

    # Pods in key namespaces
    for ns in ("bin-manager", "infrastructure", "voip"):
        results.append(check_pods_ready(ns))

    # Service endpoints
    for ns in ("bin-manager", "infrastructure", "voip"):
        results.append(check_services_endpoints(ns))

    # VMs
    results.append(check_vms_running(project_id, zone, "kamailio"))
    results.append(check_vms_running(project_id, zone, "rtpengine"))

    # Cloud SQL
    results.append(check_cloudsql_running(project_id, sql_instance))

    # DNS
    if domain:
        results.append(check_dns_resolution(f"api.{domain}"))

    # HTTP health
    if domain:
        results.append(check_http_health(f"https://api.{domain}/health"))

    # SIP port
    sip_host = tf.get("sip_external_ip", f"sip.{domain}" if domain else "")
    if sip_host:
        results.append(check_sip_port(sip_host, 5060))

    return results
