"""Tests for scripts/terraform_reconcile.py."""

import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.terraform import TERRAFORM_DIR  # noqa: E402
from scripts.terraform_reconcile import build_registry, check_exists_in_gcp, import_resource  # noqa: E402


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


class TestBuildRegistryServiceAccounts:
    def _make_config(self):
        from scripts.config import InstallerConfig
        cfg = InstallerConfig()
        cfg.set_many({
            "gcp_project_id": "my-project",
            "region": "us-central1",
            "zone": "us-central1-a",
            "kamailio_count": 1,
            "rtpengine_count": 1,
        })
        return cfg

    def test_registry_is_a_list(self):
        cfg = self._make_config()
        result = build_registry(cfg)
        assert isinstance(result, list)

    def test_each_entry_has_required_keys(self):
        cfg = self._make_config()
        for entry in build_registry(cfg):
            assert "tf_address" in entry, f"Missing tf_address in {entry}"
            assert "description" in entry, f"Missing description in {entry}"
            assert "gcloud_check" in entry, f"Missing gcloud_check in {entry}"
            assert "import_id" in entry, f"Missing import_id in {entry}"

    def test_includes_cloudsql_proxy_sa(self):
        cfg = self._make_config()
        addresses = {e["tf_address"] for e in build_registry(cfg)}
        assert "google_service_account.sa_cloudsql_proxy" in addresses

    def test_includes_all_four_service_accounts(self):
        cfg = self._make_config()
        addresses = {e["tf_address"] for e in build_registry(cfg)}
        assert "google_service_account.sa_gke_nodes" in addresses
        assert "google_service_account.sa_kamailio" in addresses
        assert "google_service_account.sa_rtpengine" in addresses

    def test_includes_kms_key_ring_and_crypto_key(self):
        cfg = self._make_config()
        addresses = {e["tf_address"] for e in build_registry(cfg)}
        assert "google_kms_key_ring.voipbin_sops" in addresses
        assert "google_kms_crypto_key.voipbin_sops_key" in addresses

    def test_sa_import_id_uses_project(self):
        cfg = self._make_config()
        entry = next(e for e in build_registry(cfg) if e["tf_address"] == "google_service_account.sa_cloudsql_proxy")
        assert "my-project" in entry["import_id"]
        assert "sa-voipbin-cloudsql-proxy" in entry["import_id"]

    def test_kms_key_ring_before_crypto_key(self):
        cfg = self._make_config()
        addresses = [e["tf_address"] for e in build_registry(cfg)]
        ring_idx = addresses.index("google_kms_key_ring.voipbin_sops")
        key_idx = addresses.index("google_kms_crypto_key.voipbin_sops_key")
        assert ring_idx < key_idx, "KMS key ring must be imported before crypto key"
