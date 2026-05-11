"""Tests for scripts/preflight.py — version parsers and preflight result logic."""

import json
from unittest.mock import MagicMock, patch

from scripts.preflight import (
    PreflightResult,
    _parse_ansible,
    _parse_gcloud,
    _parse_generic,
    _parse_kubectl,
    _parse_terraform,
    run_preflight_display,
)


class TestParseGcloud:
    def test_simple_version(self):
        assert _parse_gcloud("Google Cloud SDK 456.0.0") == "456.0.0"

    def test_multiline(self):
        output = "Google Cloud SDK 456.0.0\nbq 2.0.94\ncore 2023.08.22\ngsutil 5.25"
        assert _parse_gcloud(output) == "456.0.0"

    def test_no_version(self):
        assert _parse_gcloud("not a valid output") == ""

    def test_empty(self):
        assert _parse_gcloud("") == ""


class TestParseTerraform:
    def test_with_v_prefix(self):
        assert _parse_terraform("Terraform v1.7.3") == "1.7.3"

    def test_without_prefix(self):
        assert _parse_terraform("1.5.0") == "1.5.0"

    def test_multiline(self):
        output = "Terraform v1.7.3\non linux_amd64\n"
        assert _parse_terraform(output) == "1.7.3"

    def test_no_version(self):
        assert _parse_terraform("unknown") == ""


class TestParseAnsible:
    def test_core_format(self):
        output = "ansible [core 2.16.3]\n  config file = /etc/ansible/ansible.cfg"
        assert _parse_ansible(output) == "2.16.3"

    def test_fallback_format(self):
        assert _parse_ansible("ansible 2.15.0") == "2.15.0"

    def test_no_version(self):
        assert _parse_ansible("something else") == ""


class TestParseKubectl:
    def test_json_format(self):
        data = {
            "clientVersion": {
                "major": "1",
                "minor": "30",
                "gitVersion": "v1.30.1",
            }
        }
        assert _parse_kubectl(json.dumps(data)) == "1.30.1"

    def test_fallback_regex(self):
        assert _parse_kubectl("Client Version: v1.28.5") == "1.28.5"

    def test_invalid_json(self):
        assert _parse_kubectl("{bad json") == ""

    def test_json_missing_key(self):
        assert _parse_kubectl('{"other": "data"}') == ""


class TestParseGeneric:
    def test_extracts_semver(self):
        assert _parse_generic("Python 3.12.3") == "3.12.3"

    def test_sops_version(self):
        assert _parse_generic("sops 3.8.1 (latest)") == "3.8.1"

    def test_no_version(self):
        assert _parse_generic("no version here") == ""


class TestPreflightResult:
    def test_dataclass_fields(self):
        r = PreflightResult(
            tool="gcloud", version="456.0.0", ok=True,
            required="400.0.0", hint="install link"
        )
        assert r.tool == "gcloud"
        assert r.version == "456.0.0"
        assert r.ok is True
        assert r.required == "400.0.0"
        assert r.hint == "install link"


class TestRunPreflightDisplay:
    def test_all_ok_returns_true(self):
        results = [
            PreflightResult("gcloud", "456.0.0", True, "400.0.0", ""),
            PreflightResult("terraform", "1.7.3", True, "1.5.0", ""),
        ]
        assert run_preflight_display(results) is True

    def test_one_failed_returns_false(self):
        results = [
            PreflightResult("gcloud", "456.0.0", True, "400.0.0", ""),
            PreflightResult("terraform", "", False, "1.5.0", "install terraform"),
        ]
        assert run_preflight_display(results) is False

    def test_empty_results_returns_true(self):
        assert run_preflight_display([]) is True


class TestRunPreflightDisplayOsHints:
    @patch("scripts.preflight.get_os_install_hint", return_value=(["pip3 install ansible"], True))
    @patch("scripts.preflight.print_check")
    @patch("scripts.preflight.print_error")
    def test_shows_os_hint_for_missing_tool(self, mock_err, mock_check, mock_hint):
        from scripts.preflight import PreflightResult, run_preflight_display
        results = [PreflightResult(
            tool="ansible", version="", ok=False, required="2.15.0",
            hint="pip install ansible"
        )]
        run_preflight_display(results)
        call_args = " ".join(str(a) for a in mock_err.call_args_list)
        assert "pip3 install ansible" in call_args


class TestCheckStaticIpQuota:
    """check_static_ip_quota inspects STATIC_ADDRESSES quota for the region."""

    @patch("scripts.preflight.run_cmd")
    def test_sufficient_quota(self, mock_run):
        from scripts.preflight import check_static_ip_quota
        payload = {"quotas": [{"metric": "STATIC_ADDRESSES", "limit": 8, "usage": 2}]}
        mock_run.return_value = MagicMock(returncode=0, stdout=json.dumps(payload), stderr="")
        assert check_static_ip_quota("proj", "us-central1", needed=5) is True

    @patch("scripts.preflight.run_cmd")
    def test_insufficient_quota(self, mock_run):
        from scripts.preflight import check_static_ip_quota
        payload = {"quotas": [{"metric": "STATIC_ADDRESSES", "limit": 8, "usage": 5}]}
        mock_run.return_value = MagicMock(returncode=0, stdout=json.dumps(payload), stderr="")
        # 8 - 5 = 3, need 5 -> False
        assert check_static_ip_quota("proj", "us-central1", needed=5) is False

    @patch("scripts.preflight.run_cmd")
    def test_gcloud_error_returns_false(self, mock_run):
        from scripts.preflight import check_static_ip_quota
        mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="forbidden")
        assert check_static_ip_quota("proj", "us-central1") is False

    @patch("scripts.preflight.run_cmd")
    def test_invalid_json_returns_false(self, mock_run):
        from scripts.preflight import check_static_ip_quota
        mock_run.return_value = MagicMock(returncode=0, stdout="{not json", stderr="")
        assert check_static_ip_quota("proj", "us-central1") is False

    @patch("scripts.preflight.run_cmd")
    def test_missing_metric_returns_false(self, mock_run):
        from scripts.preflight import check_static_ip_quota
        payload = {"quotas": [{"metric": "CPUS", "limit": 100, "usage": 10}]}
        mock_run.return_value = MagicMock(returncode=0, stdout=json.dumps(payload), stderr="")
        assert check_static_ip_quota("proj", "us-central1") is False


class TestCheckNodeportAvailability:
    """check_nodeport_availability counts free NodePort slots via kubectl."""

    @patch("scripts.preflight.run_cmd")
    def test_sufficient_capacity(self, mock_run):
        from scripts.preflight import check_nodeport_availability
        payload = {"items": [
            {"spec": {"ports": [{"nodePort": 30001}]}},
            {"spec": {"ports": [{"nodePort": 30002}]}},
        ]}
        mock_run.return_value = MagicMock(returncode=0, stdout=json.dumps(payload), stderr="")
        # Default range 30000-32767 = 2768; 2 used => plenty
        assert check_nodeport_availability(needed=4) is True

    @patch("scripts.preflight.run_cmd")
    def test_exact_capacity(self, mock_run):
        from scripts.preflight import check_nodeport_availability
        # Fill 2764 of 2768 slots -> 4 free
        payload = {"items": [
            {"spec": {"ports": [{"nodePort": p} for p in range(30000, 30000 + 2764)]}},
        ]}
        mock_run.return_value = MagicMock(returncode=0, stdout=json.dumps(payload), stderr="")
        assert check_nodeport_availability(needed=4) is True
        assert check_nodeport_availability(needed=5) is False

    @patch("scripts.preflight.run_cmd")
    def test_kubectl_error_returns_false(self, mock_run):
        from scripts.preflight import check_nodeport_availability
        mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="connection refused")
        assert check_nodeport_availability(needed=4) is False

    @patch("scripts.preflight.run_cmd")
    def test_malformed_json_returns_false(self, mock_run):
        from scripts.preflight import check_nodeport_availability
        mock_run.return_value = MagicMock(returncode=0, stdout="{not-json", stderr="")
        assert check_nodeport_availability(needed=4) is False


class TestCheckLoadbalancerAddresses:
    """check_loadbalancer_addresses returns the empty/missing ADDRESS outputs."""

    def test_all_populated(self):
        from scripts.preflight import check_loadbalancer_addresses
        outputs = {
            "api_manager_static_ip_address": "1.2.3.4",
            "admin_static_ip_address": "1.2.3.5",
            "talk_static_ip_address": "1.2.3.6",
            "meet_static_ip_address": "1.2.3.7",
            "hook_manager_static_ip_address": "1.2.3.8",
        }
        assert check_loadbalancer_addresses(outputs) == []

    def test_some_missing(self):
        from scripts.preflight import check_loadbalancer_addresses
        outputs = {
            "api_manager_static_ip_address": "1.2.3.4",
            "admin_static_ip_address": "",
            "talk_static_ip_address": "   ",
        }
        missing = check_loadbalancer_addresses(outputs)
        assert "admin_static_ip_address" in missing
        assert "talk_static_ip_address" in missing
        assert "meet_static_ip_address" in missing
        assert "api_manager_static_ip_address" not in missing
        # hook is not required in PR #3a
        assert "hook_manager_static_ip_address" not in missing

    def test_all_missing(self):
        from scripts.preflight import check_loadbalancer_addresses
        assert len(check_loadbalancer_addresses({})) == 4
