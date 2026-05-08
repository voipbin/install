"""Tests for scripts/gcp.py — quota logic, API enablement, and KMS key formatting."""

import json
from unittest.mock import patch, MagicMock, call

from scripts.gcp import (
    QuotaResult,
    check_billing_tristate,
    check_required_apis,
    check_quotas,
    create_kms_keyring,
    create_service_account,
    display_quota_results,
)


class TestQuotaResult:
    def test_dataclass_fields(self):
        r = QuotaResult(
            metric="CPUS", available=24, required=8, ok=True,
            description="vCPU quota"
        )
        assert r.metric == "CPUS"
        assert r.available == 24
        assert r.required == 8
        assert r.ok is True
        assert r.description == "vCPU quota"

    def test_insufficient_quota(self):
        r = QuotaResult(
            metric="CPUS", available=2, required=8, ok=False,
            description="vCPU quota"
        )
        assert r.ok is False


class TestCheckQuotas:
    """Test check_quotas with mocked gcloud and quota definitions."""

    @patch("scripts.gcp._load_yaml_data")
    @patch("scripts.gcp.run_cmd")
    def test_all_sufficient(self, mock_run_cmd, mock_load_yaml):
        mock_load_yaml.return_value = {
            "quotas": [
                {"metric": "CPUS", "minimum": 8, "description": "vCPUs"},
                {"metric": "IN_USE_ADDRESSES", "minimum": 4, "description": "IPs"},
            ]
        }
        region_data = {
            "quotas": [
                {"metric": "CPUS", "limit": 24, "usage": 0},
                {"metric": "IN_USE_ADDRESSES", "limit": 8, "usage": 1},
            ]
        }
        mock_run_cmd.return_value = MagicMock(
            returncode=0, stdout=json.dumps(region_data)
        )
        results = check_quotas("my-project", "us-central1")
        assert len(results) == 2
        assert all(r.ok for r in results)

    @patch("scripts.gcp._load_yaml_data")
    @patch("scripts.gcp.run_cmd")
    def test_one_insufficient(self, mock_run_cmd, mock_load_yaml):
        mock_load_yaml.return_value = {
            "quotas": [
                {"metric": "CPUS", "minimum": 8, "description": "vCPUs"},
            ]
        }
        region_data = {
            "quotas": [
                {"metric": "CPUS", "limit": 6, "usage": 2},  # only 4 available
            ]
        }
        mock_run_cmd.return_value = MagicMock(
            returncode=0, stdout=json.dumps(region_data)
        )
        results = check_quotas("my-project", "us-central1")
        assert len(results) == 1
        assert results[0].ok is False
        assert results[0].available == 4

    @patch("scripts.gcp._load_yaml_data")
    @patch("scripts.gcp.run_cmd")
    def test_gcloud_failure_returns_all_failed(self, mock_run_cmd, mock_load_yaml):
        mock_load_yaml.return_value = {
            "quotas": [
                {"metric": "CPUS", "minimum": 8, "description": "vCPUs"},
            ]
        }
        mock_run_cmd.return_value = MagicMock(returncode=1, stdout="", stderr="error")
        results = check_quotas("my-project", "us-central1")
        assert len(results) == 1
        assert results[0].ok is False
        assert results[0].available == 0

    @patch("scripts.gcp._load_yaml_data")
    @patch("scripts.gcp.run_cmd")
    def test_missing_metric_defaults_to_zero(self, mock_run_cmd, mock_load_yaml):
        mock_load_yaml.return_value = {
            "quotas": [
                {"metric": "GPUS_ALL_REGIONS", "minimum": 1, "description": "GPUs"},
            ]
        }
        region_data = {
            "quotas": [
                {"metric": "CPUS", "limit": 24, "usage": 0},
            ]
        }
        mock_run_cmd.return_value = MagicMock(
            returncode=0, stdout=json.dumps(region_data)
        )
        results = check_quotas("my-project", "us-central1")
        assert results[0].available == 0
        assert results[0].ok is False

    @patch("scripts.gcp._load_yaml_data")
    @patch("scripts.gcp.run_cmd")
    def test_invalid_json_returns_empty(self, mock_run_cmd, mock_load_yaml):
        mock_load_yaml.return_value = {
            "quotas": [
                {"metric": "CPUS", "minimum": 8, "description": "vCPUs"},
            ]
        }
        mock_run_cmd.return_value = MagicMock(returncode=0, stdout="{bad json")
        results = check_quotas("my-project", "us-central1")
        assert results == []


class TestDisplayQuotaResults:
    def test_all_ok_returns_true(self):
        results = [
            QuotaResult("CPUS", 24, 8, True, "vCPUs"),
            QuotaResult("IN_USE_ADDRESSES", 7, 4, True, "IPs"),
        ]
        assert display_quota_results(results, "my-project") is True

    def test_one_failed_returns_false(self):
        results = [
            QuotaResult("CPUS", 24, 8, True, "vCPUs"),
            QuotaResult("IN_USE_ADDRESSES", 2, 4, False, "IPs"),
        ]
        assert display_quota_results(results, "my-project") is False

    def test_empty_returns_true(self):
        assert display_quota_results([], "my-project") is True


class TestCreateKmsKeyring:
    @patch("scripts.gcp.run_cmd_with_retry")
    @patch("scripts.gcp.run_cmd")
    def test_returns_correct_key_id(self, mock_run_cmd, mock_retry):
        mock_run_cmd.return_value = MagicMock(returncode=0, stdout="user@example.com")
        mock_retry.return_value = MagicMock(returncode=0)
        key_id = create_kms_keyring("my-project")
        assert key_id == (
            "projects/my-project/locations/global"
            "/keyRings/voipbin-sops/cryptoKeys/voipbin-sops-key"
        )

    @patch("scripts.gcp.run_cmd_with_retry")
    @patch("scripts.gcp.run_cmd")
    def test_custom_names(self, mock_run_cmd, mock_retry):
        mock_run_cmd.return_value = MagicMock(returncode=0, stdout="user@example.com")
        mock_retry.return_value = MagicMock(returncode=0)
        key_id = create_kms_keyring(
            "test-proj", keyring_name="my-ring", key_name="my-key",
            location="us-east1"
        )
        assert key_id == (
            "projects/test-proj/locations/us-east1"
            "/keyRings/my-ring/cryptoKeys/my-key"
        )

    @patch("scripts.gcp.run_cmd_with_retry")
    @patch("scripts.gcp.run_cmd")
    def test_calls_gcloud_for_keyring_key_and_account(self, mock_run_cmd, mock_retry):
        mock_run_cmd.return_value = MagicMock(returncode=0, stdout="user@example.com")
        mock_retry.return_value = MagicMock(returncode=0)
        create_kms_keyring("my-project")
        # run_cmd: keyring create, key create, get-value account
        assert mock_run_cmd.call_count == 3
        # run_cmd_with_retry: IAM binding
        assert mock_retry.call_count == 1

    @patch("scripts.gcp.run_cmd_with_retry")
    @patch("scripts.gcp.run_cmd")
    def test_grants_user_role_for_user_account(self, mock_run_cmd, mock_retry):
        mock_run_cmd.return_value = MagicMock(
            returncode=0, stdout="alice@example.com\n"
        )
        mock_retry.return_value = MagicMock(returncode=0)
        create_kms_keyring("my-project")
        binding_args = mock_retry.call_args[0][0]
        assert "--member=user:alice@example.com" in binding_args
        assert "--role=roles/cloudkms.cryptoKeyEncrypterDecrypter" in binding_args

    @patch("scripts.gcp.run_cmd_with_retry")
    @patch("scripts.gcp.run_cmd")
    def test_grants_service_account_role_for_sa(self, mock_run_cmd, mock_retry):
        sa = "installer@my-project.iam.gserviceaccount.com"
        mock_run_cmd.return_value = MagicMock(returncode=0, stdout=sa)
        mock_retry.return_value = MagicMock(returncode=0)
        create_kms_keyring("my-project")
        binding_args = mock_retry.call_args[0][0]
        assert f"--member=serviceAccount:{sa}" in binding_args

    @patch("scripts.gcp.print_warning")
    @patch("scripts.gcp.run_cmd_with_retry")
    @patch("scripts.gcp.run_cmd")
    def test_skips_iam_binding_when_no_account(self, mock_run_cmd, mock_retry, mock_warn):
        mock_run_cmd.return_value = MagicMock(returncode=0, stdout="")
        create_kms_keyring("my-project")
        mock_retry.assert_not_called()
        mock_warn.assert_called_once()

    @patch("scripts.gcp.print_warning")
    @patch("scripts.gcp.run_cmd_with_retry")
    @patch("scripts.gcp.run_cmd")
    def test_skips_iam_binding_when_account_is_unset_literal(
        self, mock_run_cmd, mock_retry, mock_warn
    ):
        # gcloud prints "(unset)" when no account is configured.
        mock_run_cmd.return_value = MagicMock(returncode=0, stdout="(unset)\n")
        create_kms_keyring("my-project")
        mock_retry.assert_not_called()
        mock_warn.assert_called_once()

    @patch("scripts.gcp.print_warning")
    @patch("scripts.gcp.run_cmd_with_retry")
    @patch("scripts.gcp.run_cmd")
    def test_skips_iam_binding_when_gcloud_account_command_fails(
        self, mock_run_cmd, mock_retry, mock_warn
    ):
        # First two run_cmd calls (keyring/key create) succeed; the third
        # (gcloud config get-value account) fails — must not trust its stdout.
        mock_run_cmd.side_effect = [
            MagicMock(returncode=0, stdout=""),
            MagicMock(returncode=0, stdout=""),
            MagicMock(returncode=1, stdout="garbage stdout on error"),
        ]
        create_kms_keyring("my-project")
        mock_retry.assert_not_called()
        mock_warn.assert_called_once()

    @patch("scripts.gcp.print_warning")
    @patch("scripts.gcp.run_cmd_with_retry")
    @patch("scripts.gcp.run_cmd")
    def test_warns_when_iam_binding_fails(
        self, mock_run_cmd, mock_retry, mock_warn
    ):
        mock_run_cmd.return_value = MagicMock(
            returncode=0, stdout="alice@example.com"
        )
        mock_retry.return_value = MagicMock(returncode=1, stderr="permission denied")
        create_kms_keyring("my-project")
        mock_warn.assert_called_once()
        warn_msg = mock_warn.call_args[0][0]
        assert "alice@example.com" in warn_msg
        assert "manually" in warn_msg.lower() or "manual" in warn_msg.lower()

    @patch("scripts.gcp.run_cmd_with_retry")
    @patch("scripts.gcp.run_cmd")
    def test_rejects_account_with_unsafe_characters(self, mock_run_cmd, mock_retry):
        import pytest
        mock_run_cmd.return_value = MagicMock(
            returncode=0, stdout="evil; rm -rf /"
        )
        with pytest.raises(ValueError):
            create_kms_keyring("my-project")
        mock_retry.assert_not_called()


class TestCheckBillingTristate:
    @patch("scripts.gcp.run_cmd")
    def test_billing_enabled(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout="True\n")
        assert check_billing_tristate("my-project") == "enabled"

    @patch("scripts.gcp.run_cmd")
    def test_billing_enabled_lowercase(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout="true\n")
        assert check_billing_tristate("my-project") == "enabled"

    @patch("scripts.gcp.run_cmd")
    def test_billing_disabled(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout="False\n")
        assert check_billing_tristate("my-project") == "disabled"

    @patch("scripts.gcp.run_cmd")
    def test_billing_unknown_on_error(self, mock_run):
        mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="PERMISSION_DENIED")
        assert check_billing_tristate("my-project") == "unknown"


class TestCheckRequiredApis:
    @patch("scripts.gcp.run_cmd")
    def test_all_enabled_returns_empty(self, mock_run):
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout="compute.googleapis.com\ncontainer.googleapis.com\nsqladmin.googleapis.com\n"
        )
        assert check_required_apis("my-project") == []

    @patch("scripts.gcp.run_cmd")
    def test_one_missing(self, mock_run):
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout="compute.googleapis.com\ncontainer.googleapis.com\n"
        )
        missing = check_required_apis("my-project")
        assert missing == ["sqladmin.googleapis.com"]

    @patch("scripts.gcp.run_cmd")
    def test_probe_failure_returns_empty(self, mock_run):
        mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="error")
        assert check_required_apis("my-project") == []


class TestCreateServiceAccount:
    @patch("scripts.gcp.print_warning")
    @patch("scripts.gcp._load_yaml_data")
    @patch("scripts.gcp.run_cmd_with_retry")
    @patch("scripts.gcp.run_cmd")
    def test_already_exists_is_silent(self, mock_run, mock_retry, mock_load, mock_warn):
        mock_run.return_value = MagicMock(
            returncode=1, stderr="ERROR: (gcloud.iam.service-accounts.create) Resource in projects [p] already exists",
            stdout=""
        )
        mock_load.return_value = {"roles": []}
        email = create_service_account("my-project")
        assert email == "voipbin-installer@my-project.iam.gserviceaccount.com"
        mock_warn.assert_not_called()

    @patch("scripts.gcp._load_yaml_data")
    @patch("scripts.gcp.run_cmd_with_retry")
    @patch("scripts.gcp.run_cmd")
    @patch("scripts.gcp.print_warning")
    def test_real_error_prints_warning_and_continues(self, mock_warn, mock_run, mock_retry, mock_load):
        mock_run.return_value = MagicMock(returncode=1, stderr="PERMISSION_DENIED: foo", stdout="")
        mock_load.return_value = {"roles": ["roles/editor"]}
        email = create_service_account("my-project")
        assert email is not None
        assert mock_warn.called  # warning printed
        assert mock_retry.called  # role binding still ran
