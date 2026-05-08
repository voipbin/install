"""Tests for scripts/diagnosis.py."""

import os
from pathlib import Path
from unittest.mock import MagicMock, patch
from datetime import datetime, timezone, timedelta

import pytest


class TestCheckApplicationDefaultCredentials:
    @patch("scripts.diagnosis.run_cmd")
    def test_valid_token_returns_true_with_account(self, mock_run):
        mock_run.side_effect = [
            MagicMock(returncode=0, stdout="ya29.token\n"),
            MagicMock(returncode=0, stdout="user@example.com\n"),
        ]
        from scripts.diagnosis import check_application_default_credentials
        valid, account = check_application_default_credentials()
        assert valid is True
        assert account == "user@example.com"

    @patch("scripts.diagnosis.run_cmd")
    def test_invalid_token_returns_false_none(self, mock_run):
        mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="invalid_grant")
        from scripts.diagnosis import check_application_default_credentials
        valid, account = check_application_default_credentials()
        assert valid is False
        assert account is None
        assert mock_run.call_count == 1

    @patch("scripts.diagnosis.run_cmd")
    def test_valid_token_unset_account_returns_true_none(self, mock_run):
        mock_run.side_effect = [
            MagicMock(returncode=0, stdout="ya29.token\n"),
            MagicMock(returncode=0, stdout="(unset)\n"),
        ]
        from scripts.diagnosis import check_application_default_credentials
        valid, account = check_application_default_credentials()
        assert valid is True
        assert account is None


class TestOfferAdcSetup:
    @patch("scripts.diagnosis.shutil.which", return_value=None)
    @patch("scripts.diagnosis.print_error")
    def test_gcloud_missing_returns_false(self, mock_err, mock_which):
        from scripts.diagnosis import offer_adc_setup
        result = offer_adc_setup()
        assert result is False
        mock_err.assert_called()

    @patch("scripts.diagnosis.shutil.which", return_value=None)
    @patch("scripts.diagnosis.print_error")
    def test_auto_accept_with_gcloud_missing_still_returns_false(self, mock_err, mock_which):
        from scripts.diagnosis import offer_adc_setup
        result = offer_adc_setup(auto_accept=True)
        assert result is False

    @patch("scripts.diagnosis.check_application_default_credentials")
    @patch("scripts.diagnosis.run_cmd")
    @patch("scripts.diagnosis.shutil.which", return_value="/usr/bin/gcloud")
    @patch("scripts.diagnosis.confirm", return_value=False)
    @patch("scripts.diagnosis.print_fix")
    def test_user_declines_prints_fix_returns_false(
        self, mock_fix, mock_confirm, mock_which, mock_run, mock_check
    ):
        from scripts.diagnosis import offer_adc_setup
        mock_check.return_value = (False, None)
        with patch("pathlib.Path.exists", return_value=False):
            result = offer_adc_setup()
        assert result is False
        mock_fix.assert_called()

    @patch("scripts.diagnosis.check_application_default_credentials")
    @patch("scripts.diagnosis.run_cmd")
    @patch("scripts.diagnosis.shutil.which", return_value="/usr/bin/gcloud")
    def test_auto_accept_invokes_login_directly(self, mock_which, mock_run, mock_check):
        mock_run.return_value = MagicMock(returncode=0)
        mock_check.return_value = (True, "user@example.com")
        with patch("pathlib.Path.exists", return_value=True):
            from scripts.diagnosis import offer_adc_setup
            result = offer_adc_setup(auto_accept=True)
        mock_run.assert_called()
        assert result is True

    def test_cloudsdk_config_env_var_used(self, tmp_path, monkeypatch):
        monkeypatch.setenv("CLOUDSDK_CONFIG", str(tmp_path))
        with patch("scripts.diagnosis.shutil.which", return_value="/usr/bin/gcloud"), \
             patch("scripts.diagnosis.check_application_default_credentials", return_value=(False, None)), \
             patch("scripts.diagnosis.confirm", return_value=False), \
             patch("scripts.diagnosis.print_fix"):
            from scripts.diagnosis import offer_adc_setup
            offer_adc_setup()


def _make_config(project_id="my-project", region="us-central1", zone="us-central1-a"):
    cfg = MagicMock()
    cfg.get.side_effect = lambda k, *a: {
        "gcp_project_id": project_id, "region": region, "zone": zone
    }.get(k, a[0] if a else None)
    return cfg


class TestRunPreApplyChecks:
    @patch("scripts.diagnosis.check_required_apis", return_value=[])
    @patch("scripts.diagnosis.check_billing_tristate", return_value="enabled")
    @patch("scripts.diagnosis.run_cmd")
    @patch("scripts.diagnosis.check_application_default_credentials", return_value=(True, "u@e.com"))
    def test_all_pass_returns_true(self, mock_adc, mock_run, mock_bill, mock_apis):
        mock_run.return_value = MagicMock(returncode=0, stdout="my-project")
        from scripts.diagnosis import run_pre_apply_checks
        assert run_pre_apply_checks(_make_config()) is True

    @patch("scripts.diagnosis.offer_adc_setup", return_value=False)
    @patch("scripts.diagnosis.check_application_default_credentials", return_value=(False, None))
    def test_adc_fail_returns_false(self, mock_adc, mock_setup):
        from scripts.diagnosis import run_pre_apply_checks
        assert run_pre_apply_checks(_make_config()) is False

    @patch("scripts.diagnosis.check_required_apis", return_value=[])
    @patch("scripts.diagnosis.check_billing_tristate", return_value="disabled")
    @patch("scripts.diagnosis.run_cmd")
    @patch("scripts.diagnosis.check_application_default_credentials", return_value=(True, "u@e.com"))
    def test_billing_disabled_returns_false(self, mock_adc, mock_run, mock_bill, mock_apis):
        mock_run.return_value = MagicMock(returncode=0, stdout="my-project")
        from scripts.diagnosis import run_pre_apply_checks
        assert run_pre_apply_checks(_make_config()) is False

    @patch("scripts.diagnosis.check_required_apis", return_value=[])
    @patch("scripts.diagnosis.check_billing_tristate", return_value="unknown")
    @patch("scripts.diagnosis.run_cmd")
    @patch("scripts.diagnosis.check_application_default_credentials", return_value=(True, "u@e.com"))
    def test_billing_unknown_continues(self, mock_adc, mock_run, mock_bill, mock_apis):
        mock_run.return_value = MagicMock(returncode=0, stdout="my-project")
        from scripts.diagnosis import run_pre_apply_checks
        assert run_pre_apply_checks(_make_config()) is True

    @patch("scripts.diagnosis.check_required_apis", return_value=["sqladmin.googleapis.com"])
    @patch("scripts.diagnosis.check_billing_tristate", return_value="enabled")
    @patch("scripts.diagnosis.run_cmd")
    @patch("scripts.diagnosis.check_application_default_credentials", return_value=(True, "u@e.com"))
    def test_missing_api_returns_false(self, mock_adc, mock_run, mock_bill, mock_apis):
        mock_run.return_value = MagicMock(returncode=0, stdout="my-project")
        from scripts.diagnosis import run_pre_apply_checks
        assert run_pre_apply_checks(_make_config()) is False

    @patch("scripts.diagnosis.check_required_apis", return_value=[])
    @patch("scripts.diagnosis.check_billing_tristate", return_value="enabled")
    @patch("scripts.diagnosis.run_cmd")
    @patch("scripts.diagnosis.check_application_default_credentials", return_value=(True, "u@e.com"))
    def test_timestamp_skip_when_fresh_and_not_failed(self, mock_adc, mock_run, mock_bill, mock_apis):
        fresh_ts = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        with patch("scripts.pipeline.load_state", return_value={
            "timestamp": fresh_ts, "deployment_state": "deployed"
        }):
            from scripts.diagnosis import run_pre_apply_checks
            result = run_pre_apply_checks(_make_config(), only_stage=None)
        mock_bill.assert_not_called()
        mock_apis.assert_not_called()
        assert result is True

    @patch("scripts.diagnosis.check_required_apis", return_value=[])
    @patch("scripts.diagnosis.check_billing_tristate", return_value="enabled")
    @patch("scripts.diagnosis.run_cmd")
    @patch("scripts.diagnosis.check_application_default_credentials", return_value=(True, "u@e.com"))
    def test_no_skip_when_state_failed(self, mock_adc, mock_run, mock_bill, mock_apis):
        fresh_ts = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        mock_run.return_value = MagicMock(returncode=0, stdout="my-project")
        with patch("scripts.pipeline.load_state", return_value={
            "timestamp": fresh_ts, "deployment_state": "failed"
        }):
            from scripts.diagnosis import run_pre_apply_checks
            run_pre_apply_checks(_make_config(), only_stage=None)
        mock_bill.assert_called()

    @patch("scripts.diagnosis.check_required_apis", return_value=[])
    @patch("scripts.diagnosis.check_billing_tristate", return_value="enabled")
    @patch("scripts.diagnosis.run_cmd")
    @patch("scripts.diagnosis.check_application_default_credentials", return_value=(True, "u@e.com"))
    def test_no_skip_when_only_stage_set(self, mock_adc, mock_run, mock_bill, mock_apis):
        fresh_ts = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        mock_run.return_value = MagicMock(returncode=0, stdout="my-project")
        with patch("scripts.pipeline.load_state", return_value={
            "timestamp": fresh_ts, "deployment_state": "deployed"
        }):
            from scripts.diagnosis import run_pre_apply_checks
            run_pre_apply_checks(_make_config(), only_stage="k8s_apply")
        mock_bill.assert_called()

    @patch("scripts.diagnosis.check_required_apis", return_value=[])
    @patch("scripts.diagnosis.check_billing_tristate", return_value="enabled")
    @patch("scripts.diagnosis.run_cmd")
    @patch("scripts.diagnosis.check_application_default_credentials", return_value=(True, "u@e.com"))
    def test_no_skip_when_timestamp_absent(self, mock_adc, mock_run, mock_bill, mock_apis):
        mock_run.return_value = MagicMock(returncode=0, stdout="my-project")
        with patch("scripts.pipeline.load_state", return_value={"deployment_state": "deployed"}):
            from scripts.diagnosis import run_pre_apply_checks
            run_pre_apply_checks(_make_config(), only_stage=None)
        mock_bill.assert_called()


class TestDiagnoseStageFailure:
    def _make_cfg(self, project="proj", region="us-central1", zone="us-central1-a"):
        cfg = MagicMock()
        cfg.get.side_effect = lambda k, *a: {
            "gcp_project_id": project, "region": region, "zone": zone
        }.get(k, a[0] if a else None)
        return cfg

    @patch("scripts.diagnosis.check_application_default_credentials", return_value=(False, None))
    def test_adc_invalid_returns_adc_hint_only(self, mock_adc):
        from scripts.diagnosis import diagnose_stage_failure
        hints = diagnose_stage_failure(self._make_cfg(), "terraform_init")
        assert any("gcloud auth application-default login" in h for h in hints)
        assert len(hints) == 1

    @patch("scripts.diagnosis.check_required_apis", return_value=[])
    @patch("scripts.diagnosis.check_billing_tristate", return_value="disabled")
    @patch("scripts.diagnosis.run_cmd")
    @patch("scripts.diagnosis.check_application_default_credentials", return_value=(True, "u@e.com"))
    def test_billing_disabled_adds_hint(self, mock_adc, mock_run, mock_bill, mock_apis):
        mock_run.return_value = MagicMock(returncode=0, stdout="")
        from scripts.diagnosis import diagnose_stage_failure
        hints = diagnose_stage_failure(self._make_cfg(), "terraform_init")
        assert any("billing" in h.lower() for h in hints)

    @patch("scripts.diagnosis.check_required_apis", return_value=[])
    @patch("scripts.diagnosis.check_billing_tristate", return_value="unknown")
    @patch("scripts.diagnosis.run_cmd")
    @patch("scripts.diagnosis.check_application_default_credentials", return_value=(True, "u@e.com"))
    def test_billing_unknown_no_billing_hint(self, mock_adc, mock_run, mock_bill, mock_apis):
        mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="no bucket")
        from scripts.diagnosis import diagnose_stage_failure
        hints = diagnose_stage_failure(self._make_cfg(), "terraform_init")
        assert not any("billing" in h.lower() for h in hints)

    @patch("scripts.diagnosis.check_required_apis", return_value=[])
    @patch("scripts.diagnosis.check_billing_tristate", return_value="enabled")
    @patch("scripts.diagnosis.run_cmd")
    @patch("scripts.diagnosis.check_application_default_credentials", return_value=(True, "u@e.com"))
    def test_ansible_run_no_vms(self, mock_adc, mock_run, mock_bill, mock_apis):
        mock_run.return_value = MagicMock(returncode=0, stdout="")
        from scripts.diagnosis import diagnose_stage_failure
        hints = diagnose_stage_failure(self._make_cfg(), "ansible_run")
        assert any("terraform_apply" in h for h in hints)

    @patch("scripts.diagnosis.check_required_apis", return_value=[])
    @patch("scripts.diagnosis.check_billing_tristate", return_value="enabled")
    @patch("scripts.diagnosis.run_cmd")
    @patch("scripts.diagnosis.check_application_default_credentials", return_value=(True, "u@e.com"))
    def test_ansible_run_vm_filter_uses_labels(self, mock_adc, mock_run, mock_bill, mock_apis):
        mock_run.return_value = MagicMock(returncode=0, stdout="")
        from scripts.diagnosis import diagnose_stage_failure
        diagnose_stage_failure(self._make_cfg(), "ansible_run")
        call_args = str(mock_run.call_args)
        assert "labels.env=voipbin" in call_args
        assert "tags.items" not in call_args

    @patch("scripts.diagnosis.check_required_apis", return_value=[])
    @patch("scripts.diagnosis.check_billing_tristate", return_value="enabled")
    @patch("scripts.diagnosis.run_cmd")
    @patch("scripts.diagnosis.check_application_default_credentials", return_value=(True, "u@e.com"))
    def test_k8s_apply_describe_uses_zone(self, mock_adc, mock_run, mock_bill, mock_apis):
        mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="not found")
        from scripts.diagnosis import diagnose_stage_failure
        hints = diagnose_stage_failure(self._make_cfg(zone="us-central1-a"), "k8s_apply")
        call_args = str(mock_run.call_args)
        assert "--zone" in call_args
        assert "us-central1-a" in call_args

    @patch("scripts.diagnosis.check_required_apis", return_value=[])
    @patch("scripts.diagnosis.check_billing_tristate", return_value="enabled")
    @patch("scripts.diagnosis.check_quotas")
    @patch("scripts.diagnosis.run_cmd")
    @patch("scripts.diagnosis.check_application_default_credentials", return_value=(True, "u@e.com"))
    def test_quota_hints_formatted_correctly(self, mock_adc, mock_run, mock_quotas, mock_bill, mock_apis):
        from scripts.gcp import QuotaResult
        mock_quotas.return_value = [
            QuotaResult(metric="CPUS", available=2, required=8, ok=False, description="vCPUs"),
        ]
        mock_run.return_value = MagicMock(returncode=0, stdout="")
        from scripts.diagnosis import diagnose_stage_failure
        hints = diagnose_stage_failure(self._make_cfg(), "terraform_apply")
        assert any("CPUS" in h and "2" in h and "8" in h for h in hints)


class TestDetectOs:
    def test_darwin_returns_macos(self):
        with patch("platform.system", return_value="Darwin"):
            from scripts.diagnosis import _detect_os
            assert _detect_os() == "macos"

    def test_ubuntu_quoted_id(self, tmp_path):
        os_release = tmp_path / "os-release"
        os_release.write_text('ID="ubuntu"\nVERSION_ID="22.04"\n')
        with patch("platform.system", return_value="Linux"), \
             patch("builtins.open", return_value=open(os_release)):
            from scripts.diagnosis import _detect_os
            assert _detect_os() == "debian"

    def test_rhel_id(self, tmp_path):
        os_release = tmp_path / "os-release"
        os_release.write_text('ID=rhel\n')
        with patch("platform.system", return_value="Linux"), \
             patch("builtins.open", return_value=open(os_release)):
            from scripts.diagnosis import _detect_os
            assert _detect_os() == "rhel"

    def test_missing_os_release_returns_linux(self):
        with patch("platform.system", return_value="Linux"), \
             patch("builtins.open", side_effect=OSError):
            from scripts.diagnosis import _detect_os
            assert _detect_os() == "linux"


class TestGetOsInstallHint:
    def test_ansible_all_platforms_auto_run(self):
        from scripts.diagnosis import get_os_install_hint
        for os_name in ("macos", "debian", "rhel", "linux"):
            with patch("scripts.diagnosis._detect_os", return_value=os_name):
                steps, can_auto = get_os_install_hint("ansible")
            assert can_auto is True
            assert any("pip3" in s for s in steps)

    def test_gcloud_linux_display_only(self):
        from scripts.diagnosis import get_os_install_hint
        with patch("scripts.diagnosis._detect_os", return_value="debian"):
            steps, can_auto = get_os_install_hint("gcloud")
        assert can_auto is False

    def test_gcloud_macos_auto_run(self):
        from scripts.diagnosis import get_os_install_hint
        with patch("scripts.diagnosis._detect_os", return_value="macos"):
            steps, can_auto = get_os_install_hint("gcloud")
        assert can_auto is True

    def test_sops_linux_display_only(self):
        from scripts.diagnosis import get_os_install_hint
        with patch("scripts.diagnosis._detect_os", return_value="debian"):
            _, can_auto = get_os_install_hint("sops")
        assert can_auto is False

    def test_sops_macos_auto_run(self):
        from scripts.diagnosis import get_os_install_hint
        with patch("scripts.diagnosis._detect_os", return_value="macos"):
            _, can_auto = get_os_install_hint("sops")
        assert can_auto is True

    def test_kubectl_all_auto_run(self):
        from scripts.diagnosis import get_os_install_hint
        with patch("scripts.diagnosis._detect_os", return_value="debian"):
            _, can_auto = get_os_install_hint("kubectl")
        assert can_auto is True


class TestOfferToolInstall:
    @patch("scripts.diagnosis.check_tool_exists", return_value=True)
    @patch("scripts.diagnosis.run_cmd")
    @patch("scripts.diagnosis.confirm", return_value=True)
    @patch("scripts.diagnosis.print_fix")
    def test_auto_run_success_returns_true(self, mock_fix, mock_confirm, mock_run, mock_check):
        mock_run.return_value = MagicMock(returncode=0)
        with patch("scripts.diagnosis._detect_os", return_value="macos"), \
             patch("scripts.diagnosis.get_os_install_hint", return_value=(["brew install sops"], True)):
            from scripts.diagnosis import offer_tool_install
            assert offer_tool_install("sops") is True

    @patch("scripts.diagnosis.check_tool_exists", return_value=False)
    @patch("scripts.diagnosis.run_cmd")
    @patch("scripts.diagnosis.confirm", return_value=True)
    @patch("scripts.diagnosis.print_fix")
    @patch("scripts.diagnosis.print_error")
    def test_auto_run_path_not_updated_returns_false(
        self, mock_err, mock_fix, mock_confirm, mock_run, mock_check
    ):
        mock_run.return_value = MagicMock(returncode=0)
        with patch("scripts.diagnosis.get_os_install_hint", return_value=(["brew install x"], True)):
            from scripts.diagnosis import offer_tool_install
            assert offer_tool_install("x") is False
            mock_err.assert_called()

    @patch("scripts.diagnosis.print_fix")
    @patch("scripts.diagnosis.print_error")
    def test_display_only_returns_false(self, mock_err, mock_fix):
        with patch("scripts.diagnosis.get_os_install_hint",
                   return_value=(["https://example.com"], False)):
            from scripts.diagnosis import offer_tool_install
            assert offer_tool_install("gcloud") is False
