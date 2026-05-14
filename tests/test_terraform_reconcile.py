"""Tests for scripts/terraform_reconcile.py."""

import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock

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

    def test_gcp_iam_permission_denied_with_may_not_exist_treated_as_not_found(self, monkeypatch):
        """GCP IAM returns PERMISSION_DENIED + 'or it may not exist' for absent
        service accounts. Must classify as (exists=False, check_ok=True) so the
        SA is excluded from imports rather than treated as an unverified conflict.
        Regression guard for PR-AE fix: wipeout-and-retest fresh-install P0."""
        monkeypatch.setattr(
            "scripts.terraform_reconcile.run_cmd",
            lambda *a, **kw: subprocess.CompletedProcess(
                [], 1, stdout="",
                stderr=(
                    "ERROR: (gcloud.iam.service-accounts.describe) "
                    "PERMISSION_DENIED: Permission 'iam.serviceAccounts.get' "
                    "denied on resource (or it may not exist)."
                ),
            ),
        )
        exists, ok = check_exists_in_gcp(
            ["gcloud", "iam", "service-accounts", "describe",
             "sa-voipbin-gke-nodes@voipbin-install-dev.iam.gserviceaccount.com"]
        )
        assert exists is False, "SA that does not exist must be reported as absent"
        assert ok is True, "check must be reported as succeeded (not unverified)"

    def test_gcp_iam_bare_permission_denied_stays_unverified(self, monkeypatch):
        """A PERMISSION_DENIED message WITHOUT 'or it may not exist' must NOT be
        classified as not-found — the resource may exist but caller lacks access.
        It must remain unverified (check_ok=False) so the conflict is surfaced.
        Regression guard for PR-AE: ensures the fix does not over-broaden to cover
        all permission errors on existing resources."""
        monkeypatch.setattr(
            "scripts.terraform_reconcile.run_cmd",
            lambda *a, **kw: subprocess.CompletedProcess(
                [], 1, stdout="",
                stderr="ERROR: PERMISSION_DENIED: Permission denied on resource.",
            ),
        )
        exists, ok = check_exists_in_gcp(
            ["gcloud", "iam", "service-accounts", "describe",
             "sa-existing@voipbin-install-dev.iam.gserviceaccount.com"]
        )
        assert exists is False, "resource not confirmed to exist"
        assert ok is False, "bare PERMISSION_DENIED must remain unverified"


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
            "env": "test",
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

    def test_excludes_cloudsql_proxy_sa(self):
        # PR #5a: cloudsql-proxy SA cleanup deferred to PR #5b. Reconcile
        # must NOT touch the SA — TF module still defines it.
        cfg = self._make_config()
        addresses = {e["tf_address"] for e in build_registry(cfg)}
        assert "google_service_account.sa_cloudsql_proxy" not in addresses

    def test_includes_all_three_service_accounts(self):
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
        entry = next(e for e in build_registry(cfg) if e["tf_address"] == "google_service_account.sa_gke_nodes")
        assert "my-project" in entry["import_id"]
        assert "sa-voipbin-gke-nodes" in entry["import_id"]

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
            "env": "test",
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
                    "fw_vm_ssh", "fw_kamailio_sip", "fw_rtpengine_control",
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

    def test_includes_cloud_sql_instance_and_pr_d2a_dbs_and_users(self):
        addresses = {e["tf_address"] for e in build_registry(self._make_config())}
        assert "google_sql_database_instance.voipbin" in addresses
        # PR-D2a: legacy `voipbin` database and `voipbin` user replaced with
        # per-app `bin_manager` + `asterisk` databases and the bin-manager,
        # asterisk, call-manager users. `kamailioro` is intentionally absent.
        assert "google_sql_database.voipbin_mysql_bin_manager" in addresses
        assert "google_sql_database.voipbin_mysql_asterisk" in addresses
        assert "google_sql_user.voipbin_mysql_bin_manager" in addresses
        assert "google_sql_user.voipbin_mysql_asterisk" in addresses
        assert "google_sql_user.voipbin_mysql_call_manager" in addresses
        assert "google_sql_user.voipbin_mysql_kamailioro" not in addresses
        # Legacy resources are gone from the registry.
        assert "google_sql_database.voipbin" not in addresses
        assert "google_sql_user.voipbin" not in addresses

    def test_sql_instance_before_database(self):
        entries = build_registry(self._make_config())
        addresses = [e["tf_address"] for e in entries]
        inst_idx = addresses.index("google_sql_database_instance.voipbin")
        db_idx = addresses.index("google_sql_database.voipbin_mysql_bin_manager")
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
        entry = next(
            e for e in entries
            if e["tf_address"] == "google_sql_user.voipbin_mysql_bin_manager"
        )
        # Format must be {project}/{instance}/{name}
        assert entry["import_id"] == "proj-abc/voipbin-mysql/bin-manager"


class TestReconcile:
    def _make_config(self):
        cfg = InstallerConfig()
        cfg.set_many({
            "gcp_project_id": "proj",
            "region": "us-central1",
            "zone": "us-central1-a",
            "env": "test",
            "kamailio_count": 1,
            "rtpengine_count": 1,
        })
        return cfg

    def test_returns_false_when_project_id_missing(self, monkeypatch):
        cfg = InstallerConfig()
        cfg.set_many({"region": "us-central1", "zone": "us-central1-a", "kamailio_count": 1, "rtpengine_count": 1})
        assert reconcile(cfg) is False

    def test_returns_true_when_no_conflicts(self, monkeypatch):
        monkeypatch.setattr("scripts.terraform_reconcile.terraform_state_list", lambda cfg: set())
        monkeypatch.setattr("scripts.terraform_reconcile.check_exists_in_gcp", lambda cmd: (False, True))
        assert reconcile(self._make_config()) is True

    def test_unverifiable_resources_are_included_in_import_prompt(self, monkeypatch):
        # check_ok=False must NOT silently skip — it must be offered for import
        # to prevent 409 errors when gcloud checks fail (permission/API error)
        monkeypatch.setattr("scripts.terraform_reconcile.terraform_state_list", lambda cfg: set())
        monkeypatch.setattr("scripts.terraform_reconcile.check_exists_in_gcp", lambda cmd: (False, False))
        confirmed = {"called": False}
        def fake_confirm(msg, default=True):
            confirmed["called"] = True
            return False
        monkeypatch.setattr("scripts.terraform_reconcile.confirm", fake_confirm)
        reconcile(self._make_config())
        assert confirmed["called"], "confirm() must be called even when all checks fail"

    def test_unverifiable_resources_are_imported_when_user_approves(self, monkeypatch):
        monkeypatch.setattr("scripts.terraform_reconcile.terraform_state_list", lambda cfg: set())
        monkeypatch.setattr("scripts.terraform_reconcile.check_exists_in_gcp", lambda cmd: (False, False))
        monkeypatch.setattr("scripts.terraform_reconcile.confirm", lambda msg, default=True: True)
        import_calls = []
        def fake_import(tf_address, import_id, project_id):
            import_calls.append(tf_address)
            return True, ""
        monkeypatch.setattr("scripts.terraform_reconcile.import_resource", fake_import)
        result = reconcile(self._make_config())
        assert result is True
        assert len(import_calls) > 0, "import_resource must be called for unverifiable resources"

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
            lambda cfg: {"google_service_account.sa_gke_nodes"},
        )
        checked = []
        def fake_check(cmd):
            checked.append(cmd)
            return (False, True)
        monkeypatch.setattr("scripts.terraform_reconcile.check_exists_in_gcp", fake_check)
        monkeypatch.setattr("scripts.terraform_reconcile.confirm", lambda msg, default=True: False)
        reconcile(self._make_config())
        checked_for_sa = any("sa-voipbin-gke-nodes" in str(c) for c in checked)
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

    def test_unverifiable_resource_import_failure_returns_false(self, monkeypatch):
        # If a resource can't be gcloud-verified AND terraform import fails,
        # reconcile must return False (not silently pass and let apply 409)
        monkeypatch.setattr("scripts.terraform_reconcile.terraform_state_list", lambda cfg: set())
        monkeypatch.setattr("scripts.terraform_reconcile.check_exists_in_gcp", lambda cmd: (False, False))
        monkeypatch.setattr("scripts.terraform_reconcile.confirm", lambda msg, default=True: True)
        monkeypatch.setattr("scripts.terraform_reconcile.import_resource", lambda *a, **kw: (False, "resource not found"))
        assert reconcile(self._make_config()) is False

    def test_mixed_verified_and_unverified_counts(self, monkeypatch):
        # Some resources verified (True, True), some unverified (False, False) —
        # both should appear in conflicts and both warning branches should fire
        monkeypatch.setattr("scripts.terraform_reconcile.terraform_state_list", lambda cfg: set())
        call_n = {"n": 0}
        def alternating_check(cmd):
            call_n["n"] += 1
            return (True, True) if call_n["n"] % 2 == 0 else (False, False)
        monkeypatch.setattr("scripts.terraform_reconcile.check_exists_in_gcp", alternating_check)
        warnings: list[str] = []
        monkeypatch.setattr("scripts.terraform_reconcile.print_warning", lambda msg: warnings.append(msg))
        monkeypatch.setattr("scripts.terraform_reconcile.confirm", lambda msg, default=True: False)
        result = reconcile(self._make_config())
        assert result is False
        assert any("exist in GCP" in w for w in warnings), "verified-count warning must fire"
        assert any("could not be verified" in w for w in warnings), "unverified-count warning must fire"

    def test_fresh_install_sa_gcp_iam_permission_denied_not_treated_as_conflict(self, monkeypatch):
        """Regression guard for PR-AE: on a fresh install where SAs do not yet exist,
        GCP IAM returns PERMISSION_DENIED + 'or it may not exist'. reconcile must
        return True without attempting import of SA resources.

        Before PR-AE, check_exists_in_gcp returned (False, False) for this stderr,
        causing SA entries to be treated as unverified conflicts, which then failed
        terraform import with 'Cannot import non-existent remote object'.

        NOTE: patches run_cmd (not check_exists_in_gcp) so that _NOT_FOUND_PHRASES
        is actually exercised — required for synthetic injection proof to be valid."""
        import subprocess as sp

        def fake_run_cmd(argv, **kwargs):
            # Simulate: SA gcloud check → PERMISSION_DENIED with "or it may not exist"
            if "service-accounts" in argv and "describe" in argv:
                return sp.CompletedProcess(
                    argv, 1, stdout="",
                    stderr=(
                        "ERROR: (gcloud.iam.service-accounts.describe) "
                        "PERMISSION_DENIED: Permission 'iam.serviceAccounts.get' "
                        "denied on resource (or it may not exist)."
                    ),
                )
            # All other resource checks: standard not-found
            return sp.CompletedProcess(argv, 1, stdout="", stderr="ERROR: Resource not found.")

        monkeypatch.setattr("scripts.terraform_reconcile.terraform_state_list", lambda cfg: set())
        monkeypatch.setattr("scripts.terraform_reconcile.run_cmd", fake_run_cmd)

        result = reconcile(self._make_config(), auto_approve=True)
        assert result is True, (
            "reconcile must return True when all registry candidates are absent (fresh install)"
        )


# ---------------------------------------------------------------------------
# T-AG: outputs() overwrite-authoritative behaviour (PR-AG)
# ---------------------------------------------------------------------------

class TestOutputsOverwriteAuthoritative:
    def test_tag_ag1_overwrites_non_sentinel_value(self, monkeypatch):
        """T-AG-1: outputs() with a pre-existing non-sentinel IP value → field IS overwritten."""
        import scripts.terraform_reconcile as terraform_reconcile
        mapping = terraform_reconcile.TfOutputFieldMapping(
            tf_key="cloudsql_mysql_private_ip",
            cfg_key="cloudsql_private_ip",
        )
        monkeypatch.setattr(terraform_reconcile, "FIELD_MAP", [mapping])
        config = MagicMock()
        config.get.return_value = "10.99.99.99"  # pre-existing non-sentinel value
        ok = terraform_reconcile.outputs(config, {"cloudsql_mysql_private_ip": "10.0.0.5"})
        assert ok is True
        config.set.assert_called_once_with("cloudsql_private_ip", "10.0.0.5")
        config.save.assert_called_once()

    def test_tag_ag2_noop_when_value_identical(self, monkeypatch):
        """T-AG-2: outputs() when TF value equals current value → changed is False, config.save() not called."""
        import scripts.terraform_reconcile as terraform_reconcile
        mapping = terraform_reconcile.TfOutputFieldMapping(
            tf_key="cloudsql_mysql_private_ip",
            cfg_key="cloudsql_private_ip",
        )
        monkeypatch.setattr(terraform_reconcile, "FIELD_MAP", [mapping])
        config = MagicMock()
        config.get.return_value = "10.0.0.5"  # same value as TF output
        ok = terraform_reconcile.outputs(config, {"cloudsql_mysql_private_ip": "10.0.0.5"})
        assert ok is True
        config.set.assert_not_called()
        config.save.assert_not_called()

    def test_tag_ag3_skips_when_tf_value_empty(self, monkeypatch):
        """T-AG-3: outputs() when TF value is empty/None → field is skipped."""
        import scripts.terraform_reconcile as terraform_reconcile
        mapping = terraform_reconcile.TfOutputFieldMapping(
            tf_key="cloudsql_mysql_private_ip",
            cfg_key="cloudsql_private_ip",
        )
        monkeypatch.setattr(terraform_reconcile, "FIELD_MAP", [mapping])
        config = MagicMock()
        config.get.return_value = None

        # Test with None value
        ok = terraform_reconcile.outputs(config, {"cloudsql_mysql_private_ip": None})
        assert ok is True
        config.set.assert_not_called()
        config.save.assert_not_called()

        # Test with empty string value
        ok = terraform_reconcile.outputs(config, {"cloudsql_mysql_private_ip": ""})
        assert ok is True
        config.set.assert_not_called()
        config.save.assert_not_called()
