"""Tests for scripts/verify.py"""

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.verify import (
    _make_result,
    check_cloudsql_running,
    check_dns_resolution,
    check_gke_cluster,
    check_http_health,
    check_pods_ready,
    check_services_endpoints,
    check_sip_port,
    check_static_ips_reserved,
    check_vms_running,
    run_all_checks,
)


# ---------------------------------------------------------------------------
# _make_result
# ---------------------------------------------------------------------------

class TestMakeResult:
    def test_pass(self):
        r = _make_result("test-check", "pass", "all good", 42)
        assert r == {
            "name": "test-check",
            "status": "pass",
            "message": "all good",
            "duration_ms": 42,
        }

    def test_fail(self):
        r = _make_result("bad-check", "fail", "broken", 100)
        assert r["status"] == "fail"
        assert r["message"] == "broken"

    def test_warn(self):
        r = _make_result("slow-check", "warn", "timeout", 5000)
        assert r["status"] == "warn"


# ---------------------------------------------------------------------------
# check_gke_cluster
# ---------------------------------------------------------------------------

class TestCheckGkeCluster:
    @patch("scripts.verify.run_cmd")
    def test_running(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout="RUNNING\n", stderr="")
        r = check_gke_cluster("proj", "zone-a", "my-cluster")
        assert r["status"] == "pass"
        assert "RUNNING" in r["message"]
        assert r["name"] == "GKE cluster my-cluster"
        assert isinstance(r["duration_ms"], int)

    @patch("scripts.verify.run_cmd")
    def test_not_running(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout="PROVISIONING\n", stderr="")
        r = check_gke_cluster("proj", "zone-a", "my-cluster")
        assert r["status"] == "fail"

    @patch("scripts.verify.run_cmd")
    def test_error(self, mock_run):
        mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="not found")
        r = check_gke_cluster("proj", "zone-a", "my-cluster")
        assert r["status"] == "fail"
        assert "not found" in r["message"]


# ---------------------------------------------------------------------------
# check_pods_ready
# ---------------------------------------------------------------------------

class TestCheckPodsReady:
    @patch("scripts.verify.run_cmd")
    def test_all_ready(self, mock_run):
        stdout = (
            "pod-a   True   Running\n"
            "pod-b   True   Running\n"
        )
        mock_run.return_value = MagicMock(returncode=0, stdout=stdout, stderr="")
        r = check_pods_ready("ns")
        assert r["status"] == "pass"
        assert "2/2" in r["message"]

    @patch("scripts.verify.run_cmd")
    def test_some_not_ready(self, mock_run):
        stdout = (
            "pod-a   True   Running\n"
            "pod-b   False  CrashLoopBackOff\n"
        )
        mock_run.return_value = MagicMock(returncode=0, stdout=stdout, stderr="")
        r = check_pods_ready("ns")
        assert r["status"] == "fail"
        assert "1/2" in r["message"]

    @patch("scripts.verify.run_cmd")
    def test_no_pods(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
        r = check_pods_ready("ns")
        assert r["status"] == "warn"

    @patch("scripts.verify.run_cmd")
    def test_kubectl_error(self, mock_run):
        mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="connection refused")
        r = check_pods_ready("ns")
        assert r["status"] == "fail"


# ---------------------------------------------------------------------------
# check_services_endpoints
# ---------------------------------------------------------------------------

class TestCheckServicesEndpoints:
    @patch("scripts.verify.run_cmd")
    def test_all_have_endpoints(self, mock_run):
        def side_effect(cmd, **kwargs):
            if "get svc" in cmd:
                return MagicMock(returncode=0, stdout="svc-a\nsvc-b\n", stderr="")
            return MagicMock(returncode=0, stdout="10.0.0.1\n", stderr="")

        mock_run.side_effect = side_effect
        r = check_services_endpoints("ns")
        assert r["status"] == "pass"

    @patch("scripts.verify.run_cmd")
    def test_missing_endpoints(self, mock_run):
        def side_effect(cmd, **kwargs):
            if "get svc" in cmd:
                return MagicMock(returncode=0, stdout="svc-a\n", stderr="")
            return MagicMock(returncode=0, stdout="<none>\n", stderr="")

        mock_run.side_effect = side_effect
        r = check_services_endpoints("ns")
        assert r["status"] == "warn"

    @patch("scripts.verify.run_cmd")
    def test_no_services(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
        r = check_services_endpoints("ns")
        assert r["status"] == "warn"


# ---------------------------------------------------------------------------
# check_vms_running
# ---------------------------------------------------------------------------

class TestCheckVmsRunning:
    @patch("scripts.verify.run_cmd")
    def test_all_running(self, mock_run):
        stdout = "kamailio-1\tRUNNING\nkamailio-2\tRUNNING\n"
        mock_run.return_value = MagicMock(returncode=0, stdout=stdout, stderr="")
        r = check_vms_running("proj", "zone-a", "kamailio")
        assert r["status"] == "pass"
        assert "2/2" in r["message"]

    @patch("scripts.verify.run_cmd")
    def test_some_stopped(self, mock_run):
        stdout = "kamailio-1\tRUNNING\nkamailio-2\tTERMINATED\n"
        mock_run.return_value = MagicMock(returncode=0, stdout=stdout, stderr="")
        r = check_vms_running("proj", "zone-a", "kamailio")
        assert r["status"] == "fail"
        assert "1/2" in r["message"]

    @patch("scripts.verify.run_cmd")
    def test_no_vms(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
        r = check_vms_running("proj", "zone-a", "kamailio")
        assert r["status"] == "warn"


# ---------------------------------------------------------------------------
# check_cloudsql_running
# ---------------------------------------------------------------------------

class TestCheckCloudSqlRunning:
    @patch("scripts.verify.run_cmd")
    def test_runnable(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout="RUNNABLE\n", stderr="")
        r = check_cloudsql_running("proj", "voipbin-mysql")
        assert r["status"] == "pass"

    @patch("scripts.verify.run_cmd")
    def test_suspended(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout="SUSPENDED\n", stderr="")
        r = check_cloudsql_running("proj", "voipbin-mysql")
        assert r["status"] == "fail"


# ---------------------------------------------------------------------------
# check_dns_resolution
# ---------------------------------------------------------------------------

class TestCheckDnsResolution:
    @patch("scripts.verify.socket.getaddrinfo")
    def test_resolves(self, mock_gai):
        mock_gai.return_value = [(2, 1, 6, "", ("1.2.3.4", 0))]
        r = check_dns_resolution("api.example.com")
        assert r["status"] == "pass"
        assert "1.2.3.4" in r["message"]

    @patch("scripts.verify.socket.getaddrinfo")
    def test_not_resolving(self, mock_gai):
        import socket as _socket
        mock_gai.side_effect = _socket.gaierror("Name or service not known")
        r = check_dns_resolution("bad.example.com")
        assert r["status"] == "fail"


# ---------------------------------------------------------------------------
# check_http_health
# ---------------------------------------------------------------------------

class TestCheckHttpHealth:
    @patch("scripts.verify.run_cmd")
    def test_200(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout="200", stderr="")
        r = check_http_health("https://api.example.com/health")
        assert r["status"] == "pass"
        assert "200" in r["message"]

    @patch("scripts.verify.run_cmd")
    def test_500(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout="500", stderr="")
        r = check_http_health("https://api.example.com/health")
        assert r["status"] == "fail"

    @patch("scripts.verify.run_cmd")
    def test_timeout(self, mock_run):
        mock_run.return_value = MagicMock(returncode=28, stdout="", stderr="Connection timed out")
        r = check_http_health("https://api.example.com/health")
        assert r["status"] == "warn"

    @patch("scripts.verify.run_cmd")
    def test_curl_error(self, mock_run):
        mock_run.return_value = MagicMock(returncode=7, stdout="", stderr="Failed to connect")
        r = check_http_health("https://api.example.com/health")
        assert r["status"] == "fail"


# ---------------------------------------------------------------------------
# check_sip_port
# ---------------------------------------------------------------------------

class TestCheckSipPort:
    @patch("scripts.verify.socket.socket")
    def test_open(self, mock_socket_cls):
        mock_sock = MagicMock()
        mock_sock.connect_ex.return_value = 0
        mock_socket_cls.return_value = mock_sock
        r = check_sip_port("sip.example.com", 5060)
        assert r["status"] == "pass"
        assert "open" in r["message"]

    @patch("scripts.verify.socket.socket")
    def test_refused(self, mock_socket_cls):
        mock_sock = MagicMock()
        mock_sock.connect_ex.return_value = 111
        mock_socket_cls.return_value = mock_sock
        r = check_sip_port("sip.example.com", 5060)
        assert r["status"] == "fail"

    @patch("scripts.verify.socket.socket")
    def test_timeout(self, mock_socket_cls):
        import socket as _socket
        mock_sock = MagicMock()
        mock_sock.connect_ex.side_effect = _socket.timeout("timed out")
        mock_socket_cls.return_value = mock_sock
        r = check_sip_port("sip.example.com", 5060)
        assert r["status"] == "warn"


# ---------------------------------------------------------------------------
# run_all_checks
# ---------------------------------------------------------------------------

class TestRunAllChecks:
    @patch("scripts.verify.check_sip_port")
    @patch("scripts.verify.check_http_health")
    @patch("scripts.verify.check_dns_resolution")
    @patch("scripts.verify.check_cloudsql_running")
    @patch("scripts.verify.check_vms_running")
    @patch("scripts.verify.check_services_endpoints")
    @patch("scripts.verify.check_pods_ready")
    @patch("scripts.verify.check_gke_cluster")
    def test_returns_list_of_dicts(
        self,
        mock_gke, mock_pods, mock_svc, mock_vms,
        mock_sql, mock_dns, mock_http, mock_sip,
    ):
        for m in (mock_gke, mock_pods, mock_svc, mock_vms, mock_sql, mock_dns, mock_http, mock_sip):
            m.return_value = _make_result("test", "pass", "ok", 10)

        config = {
            "gcp_project_id": "proj",
            "zone": "us-central1-a",
            "domain": "example.com",
        }
        results = run_all_checks(config)
        assert isinstance(results, list)
        assert len(results) > 0
        for r in results:
            assert "name" in r
            assert "status" in r
            assert "message" in r
            assert "duration_ms" in r

    @patch("scripts.verify.check_cloudsql_running")
    @patch("scripts.verify.check_vms_running")
    @patch("scripts.verify.check_services_endpoints")
    @patch("scripts.verify.check_pods_ready")
    @patch("scripts.verify.check_gke_cluster")
    def test_no_domain_skips_dns_http_sip(
        self,
        mock_gke, mock_pods, mock_svc, mock_vms, mock_sql,
    ):
        for m in (mock_gke, mock_pods, mock_svc, mock_vms, mock_sql):
            m.return_value = _make_result("test", "pass", "ok", 10)

        config = {
            "gcp_project_id": "proj",
            "zone": "us-central1-a",
            "domain": "",
        }
        results = run_all_checks(config)
        names = [r["name"] for r in results]
        assert not any("DNS" in n for n in names)
        assert not any("HTTP" in n for n in names)
        assert not any("SIP" in n for n in names)


# ---------------------------------------------------------------------------
# Result formatting
# ---------------------------------------------------------------------------

class TestResultFormatting:
    """Verify the result dict contract used by the CLI display layer."""

    def test_valid_statuses(self):
        for status in ("pass", "fail", "warn"):
            r = _make_result("check", status, "msg", 0)
            assert r["status"] in ("pass", "fail", "warn")

    def test_duration_is_int(self):
        r = _make_result("check", "pass", "ok", 42)
        assert isinstance(r["duration_ms"], int)

    def test_all_keys_present(self):
        r = _make_result("n", "pass", "m", 0)
        assert set(r.keys()) == {"name", "status", "message", "duration_ms"}


# ---------------------------------------------------------------------------
# check_static_ips_reserved (PR #2 of self-hosting redesign)
# ---------------------------------------------------------------------------

class TestCheckStaticIpsReserved:
    EXPECTED = [
        {"name": "api-manager-static-ip", "address": "203.0.113.10"},
        {"name": "hook-manager-static-ip", "address": "203.0.113.11"},
        {"name": "admin-static-ip", "address": "203.0.113.12"},
        {"name": "talk-static-ip", "address": "203.0.113.13"},
        {"name": "meet-static-ip", "address": "203.0.113.14"},
    ]

    @patch("scripts.verify.run_cmd")
    def test_all_five_present(self, mock_run):
        import json
        mock_run.return_value = MagicMock(returncode=0, stdout=json.dumps(self.EXPECTED), stderr="")
        r = check_static_ips_reserved("proj", "us-central1")
        assert r["status"] == "pass"
        # Message lists every expected static-IP name.
        for entry in self.EXPECTED:
            assert entry["name"] in r["message"]
        assert r["name"] == "Static IPs"

    @patch("scripts.verify.run_cmd")
    def test_missing_addresses_warn(self, mock_run):
        import json
        # Only 3 of 5 present.
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout=json.dumps(self.EXPECTED[:3]),
            stderr="",
        )
        r = check_static_ips_reserved("proj", "us-central1")
        assert r["status"] == "warn"
        assert "talk-static-ip" in r["message"]
        assert "meet-static-ip" in r["message"]

    @patch("scripts.verify.run_cmd")
    def test_gcloud_error_fail(self, mock_run):
        mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="forbidden")
        r = check_static_ips_reserved("proj", "us-central1")
        assert r["status"] == "fail"
        assert "forbidden" in r["message"]

    @patch("scripts.verify.run_cmd")
    def test_invalid_json_fail(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout="{not json", stderr="")
        r = check_static_ips_reserved("proj", "us-central1")
        assert r["status"] == "fail"


# ---------------------------------------------------------------------------
# check_tls_cert_is_production
# ---------------------------------------------------------------------------

import base64 as _b64
from unittest.mock import MagicMock as _MM, patch as _patch
import json as _json
from datetime import datetime, timedelta, timezone
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID


def _make_cert(cn: str) -> bytes:
    """Return PEM bytes for a self-signed cert with the given CN."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subj = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, cn)])
    now = datetime.now(timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(subj)
        .issuer_name(subj)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=5))
        .not_valid_after(now + timedelta(days=10))
        .sign(private_key=key, algorithm=hashes.SHA256())
    )
    return cert.public_bytes(serialization.Encoding.PEM)


def _tls_secret_json(cert_pem: bytes) -> str:
    return _json.dumps({
        "data": {
            "tls.crt": _b64.b64encode(cert_pem).decode(),
            "tls.key": _b64.b64encode(b"dummy").decode(),
        },
    })


def _opaque_secret_json(cert_pem: bytes) -> str:
    inner_b64 = _b64.b64encode(cert_pem).decode()
    outer_b64 = _b64.b64encode(inner_b64.encode()).decode()
    return _json.dumps({"data": {"SSL_CERT_BASE64": outer_b64}})


class TestCheckTlsCertIsProduction:
    """Verify the tls_cert_is_production check across multi-source layout."""

    @_patch("scripts.verify.run_cmd")
    def test_all_placeholder_fail(self, mock_run):
        from scripts.verify import check_tls_cert_is_production
        placeholder_pem = _make_cert("voipbin-self-signed")
        responses = [
            _MM(returncode=0, stdout=_tls_secret_json(placeholder_pem), stderr=""),
            _MM(returncode=0, stdout=_tls_secret_json(placeholder_pem), stderr=""),
            _MM(returncode=0, stdout=_opaque_secret_json(placeholder_pem), stderr=""),
        ]
        mock_run.side_effect = responses
        r = check_tls_cert_is_production()
        assert r["status"] == "fail"
        assert "PLACEHOLDER" in r["message"]

    @_patch("scripts.verify.run_cmd")
    def test_all_real_pass(self, mock_run):
        from scripts.verify import check_tls_cert_is_production
        real_pem = _make_cert("api.example.com")
        responses = [
            _MM(returncode=0, stdout=_tls_secret_json(real_pem), stderr=""),
            _MM(returncode=0, stdout=_tls_secret_json(real_pem), stderr=""),
            _MM(returncode=0, stdout=_opaque_secret_json(real_pem), stderr=""),
        ]
        mock_run.side_effect = responses
        r = check_tls_cert_is_production()
        assert r["status"] == "pass"

    @_patch("scripts.verify.run_cmd")
    def test_mixed_state_fails(self, mock_run):
        """voipbin-tls is real but voipbin-secret SSL_CERT_BASE64 still has placeholder."""
        from scripts.verify import check_tls_cert_is_production
        real_pem = _make_cert("api.example.com")
        placeholder_pem = _make_cert("voipbin-self-signed")
        responses = [
            _MM(returncode=0, stdout=_tls_secret_json(real_pem), stderr=""),
            _MM(returncode=0, stdout=_tls_secret_json(real_pem), stderr=""),
            _MM(returncode=0, stdout=_opaque_secret_json(placeholder_pem), stderr=""),
        ]
        mock_run.side_effect = responses
        r = check_tls_cert_is_production()
        assert r["status"] == "fail"

    @_patch("scripts.verify.run_cmd")
    def test_missing_secret_self_signed_mode_warns(self, mock_run):
        from scripts.verify import check_tls_cert_is_production
        responses = [
            _MM(returncode=1, stdout="", stderr='Error from server (NotFound): secrets "voipbin-tls" not found'),
            _MM(returncode=1, stdout="", stderr='Error from server (NotFound): secrets "voipbin-tls" not found'),
            _MM(returncode=1, stdout="", stderr='Error from server (NotFound): secrets "voipbin-secret" not found'),
        ]
        mock_run.side_effect = responses
        r = check_tls_cert_is_production(tls_strategy="self-signed")
        assert r["status"] == "warn"

    @_patch("scripts.verify.run_cmd")
    def test_missing_secret_byoc_mode_fails(self, mock_run):
        from scripts.verify import check_tls_cert_is_production
        responses = [
            _MM(returncode=1, stdout="", stderr='Error from server (NotFound): secrets "voipbin-tls" not found'),
            _MM(returncode=1, stdout="", stderr='Error from server (NotFound): secrets "voipbin-tls" not found'),
            _MM(returncode=1, stdout="", stderr='Error from server (NotFound): secrets "voipbin-secret" not found'),
        ]
        mock_run.side_effect = responses
        r = check_tls_cert_is_production(tls_strategy="byoc")
        assert r["status"] == "fail"

    @_patch("scripts.verify.run_cmd")
    def test_unparseable_cert_warns(self, mock_run):
        from scripts.verify import check_tls_cert_is_production
        garbage = _json.dumps({"data": {"tls.crt": _b64.b64encode(b"not a real cert").decode()}})
        real_pem = _make_cert("api.example.com")
        responses = [
            _MM(returncode=0, stdout=garbage, stderr=""),
            _MM(returncode=0, stdout=_tls_secret_json(real_pem), stderr=""),
            _MM(returncode=0, stdout=_opaque_secret_json(real_pem), stderr=""),
        ]
        mock_run.side_effect = responses
        r = check_tls_cert_is_production()
        assert r["status"] == "warn"

    def test_placeholder_cn_constant_matches_bootstrap(self):
        """Single source of truth: verify's placeholder CN must equal
        the one tls_bootstrap uses when generating the cert."""
        from scripts.tls_bootstrap import CN_PLACEHOLDER
        assert CN_PLACEHOLDER == "voipbin-self-signed"
