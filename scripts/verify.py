"""Post-install verification checks for VoIPBin deployment."""

import json
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


def check_static_ips_reserved(project_id: str, region: str) -> dict:
    """Check that the 5 expected external-service static IPs are
    reserved in GCP. Returns a result dict.

    Lists ``gcloud compute addresses list`` filtered to the install's
    region; passes if all 5 expected names are present.
    """
    def _check():
        from scripts.utils import _validate_cmd_arg
        _validate_cmd_arg(project_id, "project_id")
        _validate_cmd_arg(region, "region")
        expected = {
            "api-manager-static-ip",
            "hook-manager-static-ip",
            "admin-static-ip",
            "talk-static-ip",
            "meet-static-ip",
        }
        cmd = [
            "gcloud", "compute", "addresses", "list",
            "--project", project_id,
            "--filter", f"region:{region}",
            "--format=json",
        ]
        result = run_cmd(cmd, capture=True, timeout=30)
        if result.returncode != 0:
            return "fail", f"gcloud error: {result.stderr.strip()}"
        try:
            data = json.loads(result.stdout)
        except json.JSONDecodeError:
            return "fail", "could not parse gcloud output"
        found = {a.get("name", "") for a in data}
        missing = expected - found
        if missing:
            return "warn", f"missing: {sorted(missing)}"
        ips = {
            a.get("name", ""): a.get("address", "")
            for a in data
            if a.get("name", "") in expected
        }
        ip_str = " ".join(f"{n}={a}" for n, a in sorted(ips.items()))
        return "pass", ip_str

    status, message, elapsed = _timed(_check)
    return _make_result("Static IPs", status, message, elapsed)


def check_tls_cert_is_production(
    namespaces: tuple[str, ...] = ("bin-manager", "square-manager"),
    tls_secret_name: str = "voipbin-tls",
    opaque_secret_name: str = "voipbin-secret",
    opaque_secret_namespace: str = "bin-manager",
    tls_strategy: str = "self-signed",
    placeholder_cn: str | None = None,
) -> dict:
    """Verify the active TLS cert chain is operator-supplied, not the
    installer-managed self-signed placeholder.

    Inspects TWO sources because production cert replacement is a
    multi-Secret procedure (see README "Production Cert Replacement"):
      1. ``voipbin-tls`` Secret in each configured namespace
         (tls.crt data field). Consumed by frontend nginx sidecar.
      2. ``voipbin-secret.SSL_CERT_BASE64`` in ``bin-manager`` ns.
         Consumed by bin-api-manager / bin-hook-manager Go binaries
         as env-vars.

    ``placeholder_cn`` defaults to ``scripts.tls_bootstrap.CN_PLACEHOLDER``
    (single source of truth).

    ``tls_strategy`` gates severity of missing-Secret outcomes:
      - ``self-signed``: missing voipbin-tls Secret → warn (bootstrap
        will create on next init).
      - ``byoc``: missing voipbin-tls Secret → fail (operator forgot
        to provision; production-not-ready).

    Top-level status:
      - ``fail`` if ANY cert has Subject CN == placeholder_cn.
      - ``fail`` if any Secret is missing in BYOC mode.
      - ``warn`` if any Secret is missing in self-signed mode, or
        cert is unparseable, or namespace is missing.
      - ``pass`` otherwise.
    """
    if placeholder_cn is None:
        from scripts.tls_bootstrap import CN_PLACEHOLDER as _cn
        placeholder_cn = _cn

    def _check():
        try:
            from cryptography import x509
        except ImportError:
            return "warn", "cryptography library not installed"

        findings: list[str] = []
        worst = "pass"

        def _bump(level: str) -> None:
            nonlocal worst
            order = {"pass": 0, "warn": 1, "fail": 2}
            if order[level] > order[worst]:
                worst = level

        # Helper: parse a base64 PEM cert and return CN, or None on failure.
        def _cn_from_b64(b64_pem: str) -> tuple[str | None, str | None]:
            import base64
            try:
                pem = base64.b64decode(b64_pem)
                cert = x509.load_pem_x509_certificate(pem)
                attrs = cert.subject.get_attributes_for_oid(
                    x509.NameOID.COMMON_NAME
                )
                if not attrs:
                    return None, "no Subject CN"
                return attrs[0].value, None
            except Exception as exc:  # noqa: BLE001
                return None, f"parse error: {exc}"

        # Source 1: voipbin-tls Secret in each namespace.
        for ns in namespaces:
            cmd = [
                "kubectl", "-n", ns, "get", "secret", tls_secret_name,
                "-o", "json",
            ]
            r = run_cmd(cmd, capture=True, timeout=15)
            if r.returncode != 0:
                stderr = (r.stderr or "").lower()
                if "notfound" in stderr.replace(" ", "") or "not found" in stderr:
                    level = "fail" if tls_strategy == "byoc" else "warn"
                    findings.append(f"{ns}/{tls_secret_name}: missing")
                    _bump(level)
                    continue
                findings.append(f"{ns}/{tls_secret_name}: kubectl error")
                _bump("warn")
                continue
            try:
                data = json.loads(r.stdout)
            except json.JSONDecodeError:
                findings.append(f"{ns}/{tls_secret_name}: unparseable JSON")
                _bump("warn")
                continue
            crt_b64 = (data.get("data") or {}).get("tls.crt", "")
            if not crt_b64:
                findings.append(f"{ns}/{tls_secret_name}: tls.crt empty")
                _bump("warn")
                continue
            cn, err = _cn_from_b64(crt_b64)
            if err:
                findings.append(f"{ns}/{tls_secret_name}: {err}")
                _bump("warn")
            elif cn == placeholder_cn:
                findings.append(f"{ns}/{tls_secret_name}: PLACEHOLDER ({cn})")
                _bump("fail")
            else:
                findings.append(f"{ns}/{tls_secret_name}: {cn}")

        # Source 2: voipbin-secret.SSL_CERT_BASE64 in bin-manager.
        cmd = [
            "kubectl", "-n", opaque_secret_namespace, "get", "secret",
            opaque_secret_name, "-o", "json",
        ]
        r = run_cmd(cmd, capture=True, timeout=15)
        if r.returncode != 0:
            stderr = (r.stderr or "").lower()
            if "notfound" in stderr.replace(" ", "") or "not found" in stderr:
                level = "fail" if tls_strategy == "byoc" else "warn"
                findings.append(
                    f"{opaque_secret_namespace}/{opaque_secret_name}: missing"
                )
                _bump(level)
            else:
                findings.append(
                    f"{opaque_secret_namespace}/{opaque_secret_name}: kubectl error"
                )
                _bump("warn")
        else:
            try:
                data = json.loads(r.stdout)
                ssl_cert_b64_outer = (data.get("data") or {}).get(
                    "SSL_CERT_BASE64", ""
                )
            except json.JSONDecodeError:
                findings.append(
                    f"{opaque_secret_namespace}/{opaque_secret_name}: unparseable JSON"
                )
                _bump("warn")
                ssl_cert_b64_outer = ""
            if not ssl_cert_b64_outer:
                level = "fail" if tls_strategy == "byoc" else "warn"
                findings.append(
                    f"{opaque_secret_namespace}/{opaque_secret_name}.SSL_CERT_BASE64: empty"
                )
                _bump(level)
            else:
                # Secret.data fields are double-base64: kubectl returns
                # base64(value), and the env-var consumers expect the
                # value itself to be base64(PEM). So decode once to get
                # the operator-supplied base64-PEM, which is what
                # _cn_from_b64 already expects.
                import base64
                try:
                    inner_b64 = base64.b64decode(ssl_cert_b64_outer).decode(
                        "ascii", "replace"
                    ).strip()
                except Exception:  # noqa: BLE001
                    inner_b64 = ""
                cn, err = _cn_from_b64(inner_b64)
                if err:
                    findings.append(
                        f"{opaque_secret_namespace}/{opaque_secret_name}.SSL_CERT_BASE64: {err}"
                    )
                    _bump("warn")
                elif cn == placeholder_cn:
                    findings.append(
                        f"{opaque_secret_namespace}/{opaque_secret_name}.SSL_CERT_BASE64: PLACEHOLDER ({cn})"
                    )
                    _bump("fail")
                else:
                    findings.append(
                        f"{opaque_secret_namespace}/{opaque_secret_name}.SSL_CERT_BASE64: {cn}"
                    )

        message = "; ".join(findings) if findings else "no sources inspected"
        return worst, message

    status, message, elapsed = _timed(_check)
    return _make_result("TLS cert is production", status, message, elapsed)


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
    region = config.get("region", "")
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

    # Static IPs reserved (PR #2 of self-hosting redesign)
    if project_id and region:
        results.append(check_static_ips_reserved(project_id, region))

    # TLS cert is production-grade (not the self-signed bootstrap placeholder)
    tls_strategy = config.get("tls_strategy", "self-signed")
    results.append(check_tls_cert_is_production(tls_strategy=tls_strategy))

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
