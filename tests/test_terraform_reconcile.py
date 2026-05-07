"""Tests for scripts/terraform_reconcile.py."""

import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.terraform_reconcile import check_exists_in_gcp  # noqa: E402


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
