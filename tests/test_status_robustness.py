"""Tests for voipbin-install status command robustness.

Covers graceful degradation when Terraform state backend is unavailable
(e.g. immediately after destroy when the GCS bucket is gone).
"""

from __future__ import annotations

from unittest.mock import patch

import pytest


class TestPrintTerraformStatusRobustness:
    """Ensure _print_terraform_status never raises — only warns."""

    def test_exception_shows_warning_not_traceback(self):
        """If terraform_resource_count raises any Exception, status prints a
        warning (with the exception message) instead of propagating the traceback."""
        from scripts.commands.status import _print_terraform_status

        dummy_config = {}

        with patch(
            "scripts.commands.status.terraform_resource_count",
            side_effect=Exception("GCS backend unavailable"),
        ), patch("scripts.commands.status.print_warning") as mock_warn:
            _print_terraform_status(dummy_config)

        assert mock_warn.called, "_print_terraform_status must call print_warning on exception"
        warn_msg = mock_warn.call_args[0][0]
        assert "GCS backend unavailable" in warn_msg, (
            "Warning message must include the exception text for debugging"
        )

    def test_exception_does_not_raise(self):
        """Must not propagate the exception to the caller."""
        from scripts.commands.status import _print_terraform_status

        dummy_config = {}
        with patch(
            "scripts.commands.status.terraform_resource_count",
            side_effect=Exception("backend gone"),
        ):
            try:
                _print_terraform_status(dummy_config)
            except Exception as exc:
                pytest.fail(
                    f"_print_terraform_status raised {type(exc).__name__}: {exc} "
                    "instead of handling gracefully."
                )

    def test_negative_return_value_does_not_raise(self):
        """returncode != 0 path: terraform_resource_count returns -1 — must not raise."""
        from scripts.commands.status import _print_terraform_status

        dummy_config = {}
        with patch(
            "scripts.commands.status.terraform_resource_count",
            return_value=-1,
        ):
            _print_terraform_status(dummy_config)

    def test_zero_resources_does_not_raise(self):
        """Empty state: terraform_resource_count returns 0 — must not raise."""
        from scripts.commands.status import _print_terraform_status

        dummy_config = {}
        with patch(
            "scripts.commands.status.terraform_resource_count",
            return_value=0,
        ):
            _print_terraform_status(dummy_config)

    def test_positive_resources_does_not_raise(self):
        """Normal path: terraform_resource_count returns a positive int — must not raise."""
        from scripts.commands.status import _print_terraform_status

        dummy_config = {}
        with patch(
            "scripts.commands.status.terraform_resource_count",
            return_value=42,
        ):
            _print_terraform_status(dummy_config)


class TestBuildJsonStatusRobustness:
    """Ensure _build_json_status never raises even when terraform state is unavailable."""

    def test_exception_returns_minus_one_for_count(self):
        """If terraform_resource_count raises, _build_json_status returns -1
        for terraform_resource_count instead of propagating the exception."""
        from scripts.commands.status import _build_json_status

        dummy_config = {}
        dummy_state = {}
        with patch(
            "scripts.commands.status.terraform_resource_count",
            side_effect=Exception("GCS bucket deleted"),
        ), patch("scripts.commands.status.k8s_cluster_status", return_value={}), \
           patch("scripts.commands.status.k8s_status", return_value={}):
            result = _build_json_status(dummy_config, dummy_state)

        assert result["terraform_resource_count"] == -1, (
            "_build_json_status must return -1 for terraform_resource_count "
            "when terraform_resource_count raises (--json path must not traceback)."
        )

    def test_exception_does_not_raise(self):
        """_build_json_status must not propagate any terraform exception."""
        from scripts.commands.status import _build_json_status

        dummy_config = {}
        dummy_state = {}
        with patch(
            "scripts.commands.status.terraform_resource_count",
            side_effect=Exception("backend gone"),
        ), patch("scripts.commands.status.k8s_cluster_status", return_value={}), \
           patch("scripts.commands.status.k8s_status", return_value={}):
            try:
                _build_json_status(dummy_config, dummy_state)
            except Exception as exc:
                pytest.fail(
                    f"_build_json_status raised {type(exc).__name__}: {exc} "
                    "instead of handling gracefully."
                )


class TestStatusSkipsGKEWhenNotDeployed:
    """GKE/Pods/VMs must not be queried when deployment_state != deployed."""

    def _make_state(self, deployment_state: str) -> dict:
        return {"deployment_state": deployment_state, "timestamp": "", "stages": {}}

    def test_gke_not_called_when_not_deployed(self):
        """_print_gke_status must NOT be called when deployment_state != deployed."""
        from scripts.commands.status import cmd_status
        from scripts.config import InstallerConfig

        state = self._make_state("destroyed")
        with patch("scripts.commands.status.InstallerConfig") as mock_cfg_cls, \
             patch("scripts.commands.status.load_state", return_value=state), \
             patch("scripts.commands.status.terraform_resource_count", return_value=0), \
             patch("scripts.commands.status._print_gke_status") as mock_gke, \
             patch("scripts.commands.status._print_pod_status") as mock_pod, \
             patch("scripts.commands.status._print_vm_status") as mock_vm:
            mock_cfg = mock_cfg_cls.return_value
            mock_cfg.exists.return_value = True
            mock_cfg.load.return_value = None
            mock_cfg.get.side_effect = lambda k, default="": {"gcp_project_id": "proj", "region": "r", "domain": "d"}.get(k, default)
            cmd_status(as_json=False)

        mock_gke.assert_not_called()
        mock_pod.assert_not_called()
        mock_vm.assert_not_called()

    def test_gke_called_when_deployed(self):
        """_print_gke_status MUST be called when deployment_state == deployed."""
        from scripts.commands.status import cmd_status

        state = self._make_state("deployed")
        with patch("scripts.commands.status.InstallerConfig") as mock_cfg_cls, \
             patch("scripts.commands.status.load_state", return_value=state), \
             patch("scripts.commands.status.terraform_resource_count", return_value=5), \
             patch("scripts.commands.status._print_gke_status") as mock_gke, \
             patch("scripts.commands.status._print_pod_status") as mock_pod, \
             patch("scripts.commands.status._print_vm_status") as mock_vm:
            mock_cfg = mock_cfg_cls.return_value
            mock_cfg.exists.return_value = True
            mock_cfg.load.return_value = None
            mock_cfg.get.side_effect = lambda k, default="": {"gcp_project_id": "proj", "region": "r", "domain": "d"}.get(k, default)
            cmd_status(as_json=False)

        mock_gke.assert_called_once()
        mock_pod.assert_called_once()
        mock_vm.assert_called_once()

    @pytest.mark.parametrize("live_state", ["failed", "destroy_failed", "destroying"])
    def test_gke_called_when_live_resource_state(self, live_state):
        """GKE/Pods/VMs must be queried for states where infra may still be alive."""
        from scripts.commands.status import cmd_status

        state = self._make_state(live_state)
        with patch("scripts.commands.status.InstallerConfig") as mock_cfg_cls, \
             patch("scripts.commands.status.load_state", return_value=state), \
             patch("scripts.commands.status.terraform_resource_count", return_value=0), \
             patch("scripts.commands.status._print_gke_status") as mock_gke, \
             patch("scripts.commands.status._print_pod_status") as mock_pod, \
             patch("scripts.commands.status._print_vm_status") as mock_vm:
            mock_cfg = mock_cfg_cls.return_value
            mock_cfg.exists.return_value = True
            mock_cfg.load.return_value = None
            mock_cfg.get.side_effect = lambda k, default="": {"gcp_project_id": "proj", "region": "r", "domain": "d"}.get(k, default)
            cmd_status(as_json=False)

        mock_gke.assert_called_once()
        mock_pod.assert_called_once()
        mock_vm.assert_called_once()

    def test_json_skips_gke_when_not_deployed(self):
        """_build_json_status must omit gke_cluster/pods keys when not deployed."""
        from scripts.commands.status import _build_json_status

        state = self._make_state("destroyed")
        with patch("scripts.commands.status.terraform_resource_count", return_value=0), \
             patch("scripts.commands.status.k8s_cluster_status") as mock_gke, \
             patch("scripts.commands.status.k8s_status") as mock_pod:
            result = _build_json_status({}, state)

        mock_gke.assert_not_called()
        mock_pod.assert_not_called()
        assert "gke_cluster" not in result
        assert "pods" not in result

    def test_json_includes_gke_when_deployed(self):
        """_build_json_status must include gke_cluster/pods keys when deployed."""
        from scripts.commands.status import _build_json_status

        state = self._make_state("deployed")
        with patch("scripts.commands.status.terraform_resource_count", return_value=5), \
             patch("scripts.commands.status.k8s_cluster_status", return_value={"status": "ok"}), \
             patch("scripts.commands.status.k8s_status", return_value={"pods": []}):
            result = _build_json_status({}, state)

        assert "gke_cluster" in result
        assert "pods" in result
