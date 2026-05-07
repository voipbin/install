"""Tests for scripts/terraform_reconcile.py."""

import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.config import InstallerConfig  # noqa: E402
from scripts.terraform import TERRAFORM_DIR  # noqa: E402
from scripts.terraform_reconcile import build_registry, check_exists_in_gcp, import_resource, reconcile  # noqa: E402


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


class TestBuildRegistryAllResources:
    def _make_config(self, kamailio_count=1, rtpengine_count=2):
        cfg = InstallerConfig()
        cfg.set_many({
            "gcp_project_id": "proj-abc",
            "region": "us-central1",
            "zone": "us-central1-a",
            "kamailio_count": kamailio_count,
            "rtpengine_count": rtpengine_count,
        })
        return cfg

    def test_includes_vpc_network(self):
        addresses = {e["tf_address"] for e in build_registry(self._make_config())}
        assert "google_compute_network.voipbin" in addresses

    def test_includes_subnetwork(self):
        addresses = {e["tf_address"] for e in build_registry(self._make_config())}
        assert "google_compute_subnetwork.voipbin_main" in addresses

    def test_includes_all_eight_firewall_rules(self):
        addresses = {e["tf_address"] for e in build_registry(self._make_config())}
        fw_rules = {"fw_allow_internal", "fw_gke_internal", "fw_healthcheck",
                    "fw_iap_ssh", "fw_kamailio_sip", "fw_rtpengine_control",
                    "fw_rtpengine_rtp", "fw_vm_to_infra"}
        for rule in fw_rules:
            assert f"google_compute_firewall.{rule}" in addresses, f"Missing firewall rule: {rule}"

    def test_includes_nat_ip_and_lb_addresses(self):
        addresses = {e["tf_address"] for e in build_registry(self._make_config())}
        assert "google_compute_address.nat_ip" in addresses
        assert "google_compute_address.kamailio_lb_external" in addresses
        assert "google_compute_address.kamailio_lb_internal" in addresses

    def test_rtpengine_addresses_expand_per_count(self):
        addresses = {e["tf_address"] for e in build_registry(self._make_config(rtpengine_count=2))}
        assert "google_compute_address.rtpengine[0]" in addresses
        assert "google_compute_address.rtpengine[1]" in addresses
        assert "google_compute_address.rtpengine[2]" not in addresses

    def test_kamailio_instances_expand_per_count(self):
        addresses = {e["tf_address"] for e in build_registry(self._make_config(kamailio_count=2))}
        assert "google_compute_instance.kamailio[0]" in addresses
        assert "google_compute_instance.kamailio[1]" in addresses

    def test_includes_cloud_sql_instance_database_and_user(self):
        addresses = {e["tf_address"] for e in build_registry(self._make_config())}
        assert "google_sql_database_instance.voipbin" in addresses
        assert "google_sql_database.voipbin" in addresses
        assert "google_sql_user.voipbin" in addresses

    def test_sql_instance_before_database(self):
        entries = build_registry(self._make_config())
        addresses = [e["tf_address"] for e in entries]
        inst_idx = addresses.index("google_sql_database_instance.voipbin")
        db_idx = addresses.index("google_sql_database.voipbin")
        assert inst_idx < db_idx

    def test_includes_gke_cluster_and_node_pool(self):
        addresses = {e["tf_address"] for e in build_registry(self._make_config())}
        assert "google_container_cluster.voipbin" in addresses
        assert "google_container_node_pool.voipbin" in addresses

    def test_gke_cluster_before_node_pool(self):
        entries = build_registry(self._make_config())
        addresses = [e["tf_address"] for e in entries]
        cluster_idx = addresses.index("google_container_cluster.voipbin")
        pool_idx = addresses.index("google_container_node_pool.voipbin")
        assert cluster_idx < pool_idx

    def test_includes_gcs_buckets(self):
        addresses = {e["tf_address"] for e in build_registry(self._make_config())}
        assert "google_storage_bucket.media" in addresses
        assert "google_storage_bucket.terraform_state" in addresses

    def test_does_not_include_excluded_types(self):
        addresses = {e["tf_address"] for e in build_registry(self._make_config())}
        for addr in addresses:
            assert not addr.startswith("google_project_service."), f"Excluded type found: {addr}"
            assert not addr.startswith("google_project_iam_member."), f"Excluded type found: {addr}"
            assert not addr.startswith("random_password."), f"Excluded type found: {addr}"
            assert not addr.startswith("time_sleep."), f"Excluded type found: {addr}"

    def test_rtpengine_instances_expand_per_count(self):
        addresses = {e["tf_address"] for e in build_registry(self._make_config(rtpengine_count=2))}
        assert "google_compute_instance.rtpengine[0]" in addresses
        assert "google_compute_instance.rtpengine[1]" in addresses
        assert "google_compute_instance.rtpengine[2]" not in addresses

    def test_sql_user_import_id_has_correct_format(self):
        entries = build_registry(self._make_config())
        entry = next(e for e in entries if e["tf_address"] == "google_sql_user.voipbin")
        # Format must be {project}/{instance}/{name}
        assert entry["import_id"] == "proj-abc/voipbin-mysql/voipbin"


class TestReconcile:
    def _make_config(self):
        cfg = InstallerConfig()
        cfg.set_many({
            "gcp_project_id": "proj",
            "region": "us-central1",
            "zone": "us-central1-a",
            "kamailio_count": 1,
            "rtpengine_count": 1,
        })
        return cfg

    def test_returns_true_when_no_conflicts(self, monkeypatch):
        monkeypatch.setattr("scripts.terraform_reconcile.terraform_state_list", lambda cfg: set())
        monkeypatch.setattr("scripts.terraform_reconcile.check_exists_in_gcp", lambda cmd: (False, True))
        assert reconcile(self._make_config()) is True

    def test_returns_true_when_all_in_state(self, monkeypatch):
        cfg = self._make_config()
        all_addresses = {e["tf_address"] for e in build_registry(cfg)}
        monkeypatch.setattr("scripts.terraform_reconcile.terraform_state_list", lambda c: all_addresses)
        call_count = {"n": 0}
        def fake_check(cmd):
            call_count["n"] += 1
            return (False, True)
        monkeypatch.setattr("scripts.terraform_reconcile.check_exists_in_gcp", fake_check)
        result = reconcile(cfg)
        assert result is True
        assert call_count["n"] == 0

    def test_skips_resources_already_in_state(self, monkeypatch):
        monkeypatch.setattr(
            "scripts.terraform_reconcile.terraform_state_list",
            lambda cfg: {"google_service_account.sa_cloudsql_proxy"},
        )
        checked = []
        def fake_check(cmd):
            checked.append(cmd)
            return (False, True)
        monkeypatch.setattr("scripts.terraform_reconcile.check_exists_in_gcp", fake_check)
        monkeypatch.setattr("scripts.terraform_reconcile.confirm", lambda msg, default=True: False)
        reconcile(self._make_config())
        checked_for_sa = any("sa-voipbin-cloudsql-proxy" in str(c) for c in checked)
        assert not checked_for_sa, "Should not check resources already in state"

    def test_returns_false_when_user_declines(self, monkeypatch):
        monkeypatch.setattr("scripts.terraform_reconcile.terraform_state_list", lambda cfg: set())
        monkeypatch.setattr("scripts.terraform_reconcile.check_exists_in_gcp", lambda cmd: (True, True))
        monkeypatch.setattr("scripts.terraform_reconcile.confirm", lambda msg, default=True: False)
        assert reconcile(self._make_config()) is False

    def test_returns_false_when_import_fails(self, monkeypatch):
        monkeypatch.setattr("scripts.terraform_reconcile.terraform_state_list", lambda cfg: set())
        monkeypatch.setattr("scripts.terraform_reconcile.check_exists_in_gcp", lambda cmd: (True, True))
        monkeypatch.setattr("scripts.terraform_reconcile.confirm", lambda msg, default=True: True)
        monkeypatch.setattr("scripts.terraform_reconcile.import_resource", lambda *a, **kw: (False, "import error"))
        assert reconcile(self._make_config()) is False

    def test_returns_true_when_all_imports_succeed(self, monkeypatch):
        monkeypatch.setattr("scripts.terraform_reconcile.terraform_state_list", lambda cfg: set())
        monkeypatch.setattr("scripts.terraform_reconcile.check_exists_in_gcp", lambda cmd: (True, True))
        monkeypatch.setattr("scripts.terraform_reconcile.confirm", lambda msg, default=True: True)
        monkeypatch.setattr("scripts.terraform_reconcile.import_resource", lambda *a, **kw: (True, ""))
        assert reconcile(self._make_config()) is True
