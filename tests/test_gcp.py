"""Tests for scripts/gcp.py — quota logic, API enablement, and KMS key formatting."""

import json
from unittest.mock import patch, MagicMock

from scripts.gcp import (
    QuotaResult,
    check_quotas,
    create_kms_keyring,
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
    @patch("scripts.gcp.run_cmd")
    def test_returns_correct_key_id(self, mock_run_cmd):
        mock_run_cmd.return_value = MagicMock(returncode=0)
        key_id = create_kms_keyring("my-project")
        assert key_id == (
            "projects/my-project/locations/global"
            "/keyRings/voipbin-sops/cryptoKeys/voipbin-sops-key"
        )

    @patch("scripts.gcp.run_cmd")
    def test_custom_names(self, mock_run_cmd):
        mock_run_cmd.return_value = MagicMock(returncode=0)
        key_id = create_kms_keyring(
            "test-proj", keyring_name="my-ring", key_name="my-key",
            location="us-east1"
        )
        assert key_id == (
            "projects/test-proj/locations/us-east1"
            "/keyRings/my-ring/cryptoKeys/my-key"
        )

    @patch("scripts.gcp.run_cmd")
    def test_calls_gcloud_twice(self, mock_run_cmd):
        mock_run_cmd.return_value = MagicMock(returncode=0)
        create_kms_keyring("my-project")
        # Should call gcloud for keyring creation and then key creation
        assert mock_run_cmd.call_count == 2
