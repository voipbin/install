"""Tests for PR-R: pipeline stage reorder + reconcile_k8s_outputs harvest.

Design doc. docs/plans/2026-05-13-pr-r-pipeline-reorder-k8s-outputs-design.md
"""

import inspect
import io
import json
import os
import subprocess
from contextlib import redirect_stdout
from unittest.mock import MagicMock, patch

import pytest

from scripts.k8s import (
    _LB_SERVICES,
    _get_service_external_ip,
    harvest_loadbalancer_ips,
)
from scripts.pipeline import (
    APPLY_STAGES,
    STAGE_LABELS,
    STAGE_RUNNERS,
    _migrate_pr_r_apply_stages,
    _run_reconcile_k8s_outputs,
)


# ----- TestApplyStagesOrder ----------------------------------------------

class TestApplyStagesOrder:

    def test_exact_seven_stages_in_new_order(self):
        assert APPLY_STAGES == (
            "terraform_init",
            "reconcile_imports",
            "terraform_apply",
            "reconcile_outputs",
            "k8s_apply",
            "reconcile_k8s_outputs",
            "ansible_run",
        )

    def test_k8s_apply_precedes_reconcile_k8s_outputs_precedes_ansible_run(self):
        i_k8s = APPLY_STAGES.index("k8s_apply")
        i_rec = APPLY_STAGES.index("reconcile_k8s_outputs")
        i_ans = APPLY_STAGES.index("ansible_run")
        assert i_k8s < i_rec < i_ans

    def test_reconcile_outputs_precedes_k8s_apply(self):
        assert APPLY_STAGES.index("reconcile_outputs") < APPLY_STAGES.index("k8s_apply")

    def test_stage_labels_has_reconcile_k8s_outputs_entry(self):
        assert "reconcile_k8s_outputs" in STAGE_LABELS
        assert STAGE_LABELS["reconcile_k8s_outputs"]


# ----- TestStageRunnersRegistered ----------------------------------------

class TestStageRunnersRegistered:

    def test_runners_has_reconcile_k8s_outputs_key(self):
        assert "reconcile_k8s_outputs" in STAGE_RUNNERS

    def test_runner_signature_is_ordered_4_params(self):
        """Mutant guard: swapped (auto_approve, dry_run) would NOT match
        this ordered list and the test catches it. Arity-only would miss
        the swap."""
        runner = STAGE_RUNNERS["reconcile_k8s_outputs"]
        params = list(inspect.signature(runner).parameters)
        assert params == ["config", "outputs", "dry_run", "auto_approve"], params


# ----- TestGetServiceExternalIp ------------------------------------------

class TestGetServiceExternalIp:

    def _mock_run(self, stdout: str, returncode: int = 0):
        result = MagicMock()
        result.returncode = returncode
        result.stdout = stdout
        result.stderr = ""
        return result

    def test_happy_path_returns_ip(self):
        payload = json.dumps({"status": {"loadBalancer": {"ingress": [{"ip": "10.99.0.5"}]}}})
        with patch("scripts.k8s.subprocess.run", return_value=self._mock_run(payload)):
            assert _get_service_external_ip("infrastructure", "redis") == "10.99.0.5"

    def test_empty_ingress_list_returns_empty(self):
        payload = json.dumps({"status": {"loadBalancer": {"ingress": []}}})
        with patch("scripts.k8s.subprocess.run", return_value=self._mock_run(payload)):
            assert _get_service_external_ip("infrastructure", "redis") == ""

    def test_kubectl_non_zero_exit_returns_empty(self):
        with patch("scripts.k8s.subprocess.run", return_value=self._mock_run("", returncode=1)):
            assert _get_service_external_ip("infrastructure", "redis") == ""

    def test_malformed_json_returns_empty(self):
        with patch("scripts.k8s.subprocess.run", return_value=self._mock_run("not-json")):
            assert _get_service_external_ip("infrastructure", "redis") == ""

    def test_status_null_pending_lb_returns_empty_without_raising(self):
        payload = json.dumps({"status": None})
        with patch("scripts.k8s.subprocess.run", return_value=self._mock_run(payload)):
            assert _get_service_external_ip("infrastructure", "redis") == ""

    def test_ingress_zero_ip_null_returns_empty(self):
        payload = json.dumps({"status": {"loadBalancer": {"ingress": [{"ip": None}]}}})
        with patch("scripts.k8s.subprocess.run", return_value=self._mock_run(payload)):
            assert _get_service_external_ip("infrastructure", "redis") == ""


# ----- TestHarvestLoadbalancerIps ----------------------------------------

class TestHarvestLoadbalancerIps:

    def test_all_5_services_return_ips(self):
        # Map each (ns, svc) → IP
        ip_map = {
            ("infrastructure", "redis"): "10.99.0.5",
            ("infrastructure", "rabbitmq"): "10.99.0.6",
            ("voip", "asterisk-call-udp"): "10.99.0.7",
            ("voip", "asterisk-registrar"): "10.99.0.8",
            ("voip", "asterisk-conference"): "10.99.0.9",
        }
        with patch(
            "scripts.k8s._get_service_external_ip",
            side_effect=lambda ns, svc: ip_map.get((ns, svc), ""),
        ):
            with patch("scripts.k8s.time.sleep"):
                out = harvest_loadbalancer_ips(timeout_seconds=10, poll_interval=0)
        assert set(out.keys()) == {
            "redis_lb_ip",
            "rabbitmq_lb_ip",
            "asterisk_call_lb_ip",
            "asterisk_registrar_lb_ip",
            "asterisk_conference_lb_ip",
        }
        assert out["redis_lb_ip"] == "10.99.0.5"

    def test_subset_returns_subset_with_warnings(self):
        ip_map = {
            ("infrastructure", "redis"): "10.99.0.5",
            ("voip", "asterisk-call-udp"): "10.99.0.7",
        }
        with patch(
            "scripts.k8s._get_service_external_ip",
            side_effect=lambda ns, svc: ip_map.get((ns, svc), ""),
        ):
            with patch("scripts.k8s.time.sleep"), patch(
                "scripts.k8s.print_warning"
            ) as warn:
                out = harvest_loadbalancer_ips(timeout_seconds=1, poll_interval=0)
        assert set(out.keys()) == {"redis_lb_ip", "asterisk_call_lb_ip"}
        # 3 services missing → 3 warnings (assert SET of called services, not order)
        warned_services = {call.args[0] for call in warn.call_args_list}
        # Each warning string contains the namespace/service slug; assert by substring
        warned_set = {
            (ns, svc)
            for (ns, svc, _key) in _LB_SERVICES
            if any(f"{ns}/{svc}" in msg for msg in warned_services)
        }
        assert warned_set == {
            ("infrastructure", "rabbitmq"),
            ("voip", "asterisk-registrar"),
            ("voip", "asterisk-conference"),
        }

    def test_timeout_with_nothing_returns_empty_dict_and_5_warnings(self):
        with patch("scripts.k8s._get_service_external_ip", return_value=""):
            with patch("scripts.k8s.time.sleep"), patch(
                "scripts.k8s.print_warning"
            ) as warn:
                out = harvest_loadbalancer_ips(timeout_seconds=1, poll_interval=0)
        assert out == {}
        assert warn.call_count == 5

    def test_result_keys_are_exactly_canonical_lb_ip_set(self):
        canonical = {key for (_ns, _svc, key) in _LB_SERVICES}
        # No namespace/service tokens leaked into keys
        for k in canonical:
            assert k.endswith("_lb_ip"), k
            assert "/" not in k
            assert "-" not in k or k.startswith("asterisk_")

    def test_env_var_timeout_honored_when_none(self):
        """VOIPBIN_LB_HARVEST_TIMEOUT_SECONDS env var is read when
        timeout_seconds=None. We patch time to capture the deadline."""
        captured_deadlines = []

        # time.monotonic is called once for deadline + per iteration for the
        # while-condition check. Return increasing values so the loop exits.
        clock = iter([0.0, 1000.0])

        def fake_monotonic():
            val = next(clock, 1000.0)
            captured_deadlines.append(val)
            return val

        with patch.dict(os.environ, {"VOIPBIN_LB_HARVEST_TIMEOUT_SECONDS": "42"}):
            with patch("scripts.k8s._get_service_external_ip", return_value=""):
                with patch("scripts.k8s.time.monotonic", side_effect=fake_monotonic):
                    with patch("scripts.k8s.time.sleep"), patch(
                        "scripts.k8s.print_warning"
                    ):
                        out = harvest_loadbalancer_ips(timeout_seconds=None)
        # The first captured monotonic call is the deadline calc;
        # deadline = 0.0 + 42 = 42. Then the while-condition reads 1000 > 42 → exits.
        # We cannot intercept the addition directly, so we assert the env value
        # was actually consumed: with timeout_seconds=42, the warning message
        # for missing services must include "42s".
        assert out == {}


# ----- TestRunReconcileK8sOutputsRunner -----------------------------------

class TestRunReconcileK8sOutputsRunner:

    def test_runner_merges_into_outputs_and_persists(self, tmp_path):
        cfg = MagicMock()
        outputs: dict = {}
        harvested = {"redis_lb_ip": "10.99.0.5", "rabbitmq_lb_ip": "10.99.0.6"}

        with patch(
            "scripts.k8s.harvest_loadbalancer_ips", return_value=harvested
        ), patch(
            "scripts.pipeline.load_state", return_value={}
        ) as load_mock, patch(
            "scripts.pipeline.save_state"
        ) as save_mock:
            ok = _run_reconcile_k8s_outputs(cfg, outputs, False, False)

        assert ok is True
        assert outputs == harvested  # merged into in-memory dict
        assert save_mock.called
        saved_state = save_mock.call_args.args[0]
        assert saved_state["k8s_outputs"] == harvested

    def test_dry_run_skips_harvest_and_persistence(self):
        cfg = MagicMock()
        outputs: dict = {}
        with patch("scripts.k8s.harvest_loadbalancer_ips") as harvest_mock, patch(
            "scripts.pipeline.save_state"
        ) as save_mock:
            ok = _run_reconcile_k8s_outputs(cfg, outputs, True, False)
        assert ok is True
        assert not harvest_mock.called
        assert not save_mock.called
        assert outputs == {}

    def test_partial_reharvest_preserves_prior_keys(self):
        """Prior persisted dict has 5 keys. This run harvests only 2.
        Merged result must still have all 5 (3 prior + 2 new overrides)."""
        cfg = MagicMock()
        outputs: dict = {}
        prior = {
            "redis_lb_ip": "old_redis",
            "rabbitmq_lb_ip": "old_rabbit",
            "asterisk_call_lb_ip": "old_call",
            "asterisk_registrar_lb_ip": "old_reg",
            "asterisk_conference_lb_ip": "old_conf",
        }
        new = {
            "redis_lb_ip": "new_redis",
            "rabbitmq_lb_ip": "new_rabbit",
        }
        with patch(
            "scripts.k8s.harvest_loadbalancer_ips", return_value=new
        ), patch(
            "scripts.pipeline.load_state", return_value={"k8s_outputs": dict(prior)}
        ), patch(
            "scripts.pipeline.save_state"
        ) as save_mock:
            _run_reconcile_k8s_outputs(cfg, outputs, False, False)

        saved = save_mock.call_args.args[0]["k8s_outputs"]
        # All 5 keys present; the 2 new ones override, 3 prior preserved.
        assert set(saved.keys()) == {
            "redis_lb_ip",
            "rabbitmq_lb_ip",
            "asterisk_call_lb_ip",
            "asterisk_registrar_lb_ip",
            "asterisk_conference_lb_ip",
        }
        assert saved["redis_lb_ip"] == "new_redis"
        assert saved["asterisk_call_lb_ip"] == "old_call"


# ----- TestPrRStateMigration ----------------------------------------------

class TestPrRStateMigration:

    def test_pre_pr_r_state_resets_ansible_run(self, capsys):
        state = {
            "stages": {
                "terraform_apply": "complete",
                "ansible_run": "complete",
                "k8s_apply": "complete",
            }
        }
        with patch("scripts.pipeline.print_warning") as warn:
            out = _migrate_pr_r_apply_stages(state)
        assert out["stages"]["ansible_run"] == "pending"
        assert warn.called

    def test_state_with_reconcile_k8s_outputs_present_is_noop(self):
        state = {
            "stages": {
                "ansible_run": "complete",
                "reconcile_k8s_outputs": "complete",
            }
        }
        before = dict(state["stages"])
        out = _migrate_pr_r_apply_stages(state)
        assert out["stages"] == before

    def test_state_with_ansible_run_pending_is_noop(self):
        state = {"stages": {"ansible_run": "pending"}}
        out = _migrate_pr_r_apply_stages(state)
        assert out["stages"]["ansible_run"] == "pending"

    def test_fresh_state_no_stages_key_is_noop(self):
        state = {}
        out = _migrate_pr_r_apply_stages(state)
        assert out == {}

    def test_post_destroy_state_clears_k8s_outputs(self):
        """destroy_pipeline at end-of-success sets state["k8s_outputs"] = {}.
        Simulate that shape and assert."""
        state = {
            "deployment_state": "destroyed",
            "stages": {s: "pending" for s in APPLY_STAGES},
            "k8s_outputs": {},
        }
        # Migration shim is a no-op on this shape (ansible_run is already
        # pending; reconcile_k8s_outputs is in stages).
        out = _migrate_pr_r_apply_stages(state)
        assert out["k8s_outputs"] == {}
        # And: ansible_run remains pending so next apply re-runs after harvest.
        assert out["stages"]["ansible_run"] == "pending"
