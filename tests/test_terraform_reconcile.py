"""Tests for scripts/terraform_reconcile.py."""

import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.terraform import TERRAFORM_DIR  # noqa: E402
from scripts.terraform_reconcile import check_exists_in_gcp, import_resource  # noqa: E402


class TestCheckExistsInGcp:
    def test_returns_true_when_exit_zero(self, monkeypatch):
        monkeypatch.setattr(
            "scripts.terraform_reconcile.run_cmd",
            lambda *a, **kw: subprocess.CompletedProcess([], 0, stdout="{}", stderr=""),
        )
        exists, ok = check_exists_in_gcp(["gcloud", "iam", "service-accounts", "describe", "sa@proj.iam.gserviceaccount.com"])
        assert exists is True
        assert ok is True

    def test_returns_false_true_when_not_found_in_stderr(self, monkeypatch):
        monkeypatch.setattr(
            "scripts.terraform_reconcile.run_cmd",
            lambda *a, **kw: subprocess.CompletedProcess([], 1, stdout="", stderr="ERROR: (gcloud.iam.service-accounts.describe) NOT_FOUND: unknown service account"),
        )
        exists, ok = check_exists_in_gcp(["gcloud", "iam", "service-accounts", "describe", "missing@proj.iam.gserviceaccount.com"])
        assert exists is False
        assert ok is True

    def test_returns_false_false_when_permission_error(self, monkeypatch):
        monkeypatch.setattr(
            "scripts.terraform_reconcile.run_cmd",
            lambda *a, **kw: subprocess.CompletedProcess([], 1, stdout="", stderr="ERROR: (gcloud) PERMISSION_DENIED: Request had insufficient authentication scopes"),
        )
        exists, ok = check_exists_in_gcp(["gcloud", "iam", "service-accounts", "describe", "sa@proj.iam.gserviceaccount.com"])
        assert exists is False
        assert ok is False

    def test_treats_404_in_stderr_as_not_found(self, monkeypatch):
        monkeypatch.setattr(
            "scripts.terraform_reconcile.run_cmd",
            lambda *a, **kw: subprocess.CompletedProcess([], 1, stdout="", stderr="404 Resource not found"),
        )
        exists, ok = check_exists_in_gcp(["gcloud", "compute", "networks", "describe", "voipbin-vpc"])
        assert exists is False
        assert ok is True


class TestImportResource:
    def test_returns_true_on_success(self, monkeypatch):
        monkeypatch.setattr(
            "scripts.terraform_reconcile.run_cmd",
            lambda *a, **kw: subprocess.CompletedProcess([], 0, stdout="Import successful", stderr=""),
        )
        ok, err = import_resource(
            "google_compute_network.voipbin",
            "projects/proj/global/networks/voipbin-vpc",
            "proj-123",
        )
        assert ok is True
        assert err == ""

    def test_returns_false_with_error_on_failure(self, monkeypatch):
        monkeypatch.setattr(
            "scripts.terraform_reconcile.run_cmd",
            lambda *a, **kw: subprocess.CompletedProcess([], 1, stdout="", stderr="Error: resource not importable"),
        )
        ok, err = import_resource(
            "google_compute_network.voipbin",
            "projects/proj/global/networks/voipbin-vpc",
            "proj-123",
        )
        assert ok is False
        assert "not importable" in err

    def test_passes_project_id_var_to_command(self, monkeypatch):
        captured = {}
        def fake_run(cmd, **kw):
            captured["cmd"] = cmd
            return subprocess.CompletedProcess([], 0, stdout="", stderr="")
        monkeypatch.setattr("scripts.terraform_reconcile.run_cmd", fake_run)
        import_resource("google_compute_network.voipbin", "some/import/id", "my-project")
        assert "-var" in captured["cmd"]
        assert "project_id=my-project" in captured["cmd"]

    def test_passes_no_color_flag(self, monkeypatch):
        captured = {}
        def fake_run(cmd, **kw):
            captured["cmd"] = cmd
            captured["kw"] = kw
            return subprocess.CompletedProcess([], 0, stdout="", stderr="")
        monkeypatch.setattr("scripts.terraform_reconcile.run_cmd", fake_run)
        import_resource("google_compute_network.voipbin", "some/import/id", "proj")
        assert "-no-color" in captured["cmd"]
        assert captured["kw"].get("cwd") == TERRAFORM_DIR
