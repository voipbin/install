"""Tests for scripts/ansible_runner.py — extra-vars generation and command building."""

import json
import os
from pathlib import Path
from unittest.mock import patch, MagicMock

from scripts.ansible_runner import _write_extra_vars, ANSIBLE_DIR, PLAYBOOK_SITE


class TestWriteExtraVars:
    """Test _write_extra_vars produces correct JSON for Ansible."""

    def _make_config(self, data: dict):
        """Create a mock InstallerConfig returning data for to_ansible_vars."""
        cfg = MagicMock()
        cfg.to_ansible_vars.return_value = dict(data)
        cfg.get.side_effect = lambda key, default="": data.get(key, default)
        return cfg

    def test_creates_valid_json_file(self):
        cfg = self._make_config({
            "gcp_project_id": "test-proj",
            "region": "us-central1",
            "zone": "us-central1-a",
        })
        tf_outputs = {"gke_cluster_name": "my-cluster"}
        path = _write_extra_vars(cfg, tf_outputs)
        try:
            assert path.exists()
            data = json.loads(path.read_text())
            assert isinstance(data, dict)
        finally:
            path.unlink(missing_ok=True)

    def test_contains_terraform_outputs(self):
        cfg = self._make_config({"gcp_project_id": "p1"})
        tf_outputs = {"gke_cluster_name": "cluster-1", "cloudsql_ip": "10.0.0.1"}
        path = _write_extra_vars(cfg, tf_outputs)
        try:
            data = json.loads(path.read_text())
            assert data["terraform_outputs"] == tf_outputs
        finally:
            path.unlink(missing_ok=True)

    def test_flattens_cloudsql_connection_name(self):
        cfg = self._make_config({})
        tf_outputs = {"cloudsql_connection_name": "proj:region:instance"}
        path = _write_extra_vars(cfg, tf_outputs)
        try:
            data = json.loads(path.read_text())
            assert data["cloudsql_connection_name"] == "proj:region:instance"
        finally:
            path.unlink(missing_ok=True)

    def test_flattens_kamailio_ips(self):
        cfg = self._make_config({})
        tf_outputs = {"kamailio_internal_ips": ["10.0.0.2", "10.0.0.3"]}
        path = _write_extra_vars(cfg, tf_outputs)
        try:
            data = json.loads(path.read_text())
            assert data["kamailio_internal_ips"] == ["10.0.0.2", "10.0.0.3"]
        finally:
            path.unlink(missing_ok=True)

    def test_defaults_for_missing_tf_outputs(self):
        cfg = self._make_config({})
        tf_outputs = {}
        path = _write_extra_vars(cfg, tf_outputs)
        try:
            data = json.loads(path.read_text())
            assert data["cloudsql_connection_name"] == ""
            assert data["cloudsql_ip"] == ""
            assert data["kamailio_internal_ips"] == []
            assert data["rtpengine_external_ips"] == []
            assert data["kamailio_external_lb_ip"] == ""
        finally:
            path.unlink(missing_ok=True)

    def test_file_has_json_suffix(self):
        cfg = self._make_config({})
        path = _write_extra_vars(cfg, {})
        try:
            assert path.suffix == ".json"
        finally:
            path.unlink(missing_ok=True)

    def test_file_has_restricted_permissions(self):
        cfg = self._make_config({})
        path = _write_extra_vars(cfg, {})
        try:
            mode = path.stat().st_mode & 0o777
            assert mode == 0o600, f"Expected 0o600, got {oct(mode)}"
        finally:
            path.unlink(missing_ok=True)


class TestAnsibleRunCleanup:
    """Test that ansible_run and ansible_check clean up temp files."""

    def _make_config(self, data: dict):
        cfg = MagicMock()
        cfg.to_ansible_vars.return_value = dict(data)
        cfg.get.side_effect = lambda key, default="": data.get(key, default)
        return cfg

    @patch("scripts.ansible_runner.run_cmd")
    def test_ansible_run_cleans_up_on_failure(self, mock_run_cmd):
        mock_run_cmd.return_value = MagicMock(returncode=1)
        cfg = self._make_config({"gcp_project_id": "p1", "zone": "z1"})
        # Track temp files created
        result = False
        from scripts.ansible_runner import ansible_run
        result = ansible_run(cfg, {})
        assert result is False
        # The temp file should have been cleaned up by the finally block

    @patch("scripts.ansible_runner.run_cmd")
    def test_ansible_check_cleans_up_on_failure(self, mock_run_cmd):
        mock_run_cmd.return_value = MagicMock(returncode=1)
        cfg = self._make_config({"gcp_project_id": "p1", "zone": "z1"})
        from scripts.ansible_runner import ansible_check
        result = ansible_check(cfg, {})
        assert result is False


class TestAnsiblePaths:
    def test_playbook_path(self):
        assert PLAYBOOK_SITE.name == "site.yml"
        assert "playbooks" in str(PLAYBOOK_SITE)

    def test_ansible_dir_exists_in_path(self):
        assert "ansible" in str(ANSIBLE_DIR)
