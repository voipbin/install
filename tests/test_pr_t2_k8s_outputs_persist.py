"""PR-T2 regression. run_pipeline must persist the reconcile_k8s_outputs
harvest into state.yaml.k8s_outputs as part of the SAME save_state call
that marks the stage complete.

Background. PR-R put the k8s_outputs persistence INSIDE
_run_reconcile_k8s_outputs via a separate load_state/save_state pair.
The main loop's subsequent save_state(state) — using the state dict it
loaded BEFORE the stage ran — then overwrote the disk file with a state
that had no k8s_outputs key, dropping the harvest. Operator-visible
symptom: `voipbin-install apply --stage ansible_run` invoked separately
after reconcile_k8s_outputs sees an empty hydration set and Kamailio's
env.j2 renders empty LB IP slots → CrashLoop.

PR-T2 moves the persistence into the main loop, right after stage marks
complete but inside the same save_state call. This test pins that
ordering and the merge semantics.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from scripts.pipeline import APPLY_STAGES, run_pipeline


def _state_with_only_reconcile_pending() -> dict:
    """State where every stage except reconcile_k8s_outputs is complete.
    Forces run_pipeline to execute exactly one stage."""
    stages = {s: "complete" for s in APPLY_STAGES}
    stages["reconcile_k8s_outputs"] = "pending"
    return {
        "deployment_state": "applying",
        "stages": stages,
    }


def _run_pipeline_with_one_reconcile_stage(
    harvested: dict[str, str],
    initial_state: dict,
) -> list:
    """Run run_pipeline with reconcile_k8s_outputs as the only pending stage
    and capture every save_state call. Returns the captured args list."""
    cfg = MagicMock()
    saved_states: list = []

    def _capture_save(s):
        # Deep-copy via dict() so later mutations on the same `state` dict
        # do not retroactively change earlier snapshots.
        import copy
        saved_states.append(copy.deepcopy(s))

    with patch(
        "scripts.pipeline.load_state", return_value=dict(initial_state)
    ), patch(
        "scripts.pipeline.save_state", side_effect=_capture_save
    ), patch(
        "scripts.k8s.harvest_loadbalancer_ips", return_value=harvested
    ), patch(
        "scripts.pipeline.terraform_output", return_value={}
    ):
        ok = run_pipeline(cfg, dry_run=False, auto_approve=True)
    assert ok is True, "run_pipeline returned False"
    return saved_states


class TestRunPipelinePersistsK8sOutputs:
    """The harvested LB IPs MUST be in state.yaml.k8s_outputs after
    run_pipeline returns, even when reconcile_k8s_outputs is the only
    stage executed in this CLI invocation."""

    def test_harvested_ips_land_in_final_saved_state(self):
        harvested = {
            "redis_lb_ip": "10.164.0.52",
            "rabbitmq_lb_ip": "10.164.0.55",
            "asterisk_call_lb_ip": "10.164.0.18",
            "asterisk_registrar_lb_ip": "10.164.0.31",
            "asterisk_conference_lb_ip": "10.164.0.26",
        }
        saved = _run_pipeline_with_one_reconcile_stage(
            harvested, _state_with_only_reconcile_pending()
        )
        assert saved, "save_state was never called"
        final = saved[-1]
        assert "k8s_outputs" in final, (
            "PR-T2 regression: final save_state has no k8s_outputs key. "
            "The main loop is not persisting harvest results."
        )
        assert final["k8s_outputs"] == harvested

    def test_stage_complete_and_k8s_outputs_in_same_save(self):
        """The save that marks reconcile_k8s_outputs=complete MUST also
        contain k8s_outputs. Otherwise a crash between the two writes
        would leave state with stage=complete but no IPs persisted."""
        harvested = {"redis_lb_ip": "10.164.0.52"}
        saved = _run_pipeline_with_one_reconcile_stage(
            harvested, _state_with_only_reconcile_pending()
        )
        # Find the save_state that flipped reconcile_k8s_outputs to complete.
        completes = [
            s for s in saved
            if s.get("stages", {}).get("reconcile_k8s_outputs") == "complete"
        ]
        assert completes, (
            "No save_state captured reconcile_k8s_outputs=complete"
        )
        assert completes[0]["k8s_outputs"] == harvested, (
            "PR-T2 regression: the stage-complete save and the k8s_outputs "
            "persistence are not on the same save_state call. Crash between "
            "them would leave inconsistent state."
        )

    def test_sentinel_key_not_leaked_into_state(self):
        """`__pr_t2_harvested_lb_ips__` is a private contract between the
        stage and the main loop. It MUST NOT appear in any persisted state
        or in tf_outputs as observed by downstream stages."""
        harvested = {"redis_lb_ip": "10.164.0.52"}
        saved = _run_pipeline_with_one_reconcile_stage(
            harvested, _state_with_only_reconcile_pending()
        )
        for s in saved:
            assert "__pr_t2_harvested_lb_ips__" not in s, (
                f"PR-T2 sentinel leaked into persisted state: {s}"
            )

    def test_partial_reharvest_preserves_prior_keys(self):
        """When state.yaml already has 5 k8s_outputs keys and this run
        re-harvests only 2, the merged result must still have all 5."""
        prior_state = _state_with_only_reconcile_pending()
        prior_state["k8s_outputs"] = {
            "redis_lb_ip": "old_redis",
            "rabbitmq_lb_ip": "old_rabbit",
            "asterisk_call_lb_ip": "old_call",
            "asterisk_registrar_lb_ip": "old_reg",
            "asterisk_conference_lb_ip": "old_conf",
        }
        new_harvest = {
            "redis_lb_ip": "new_redis",
            "rabbitmq_lb_ip": "new_rabbit",
        }
        saved = _run_pipeline_with_one_reconcile_stage(
            new_harvest, prior_state
        )
        final = saved[-1]["k8s_outputs"]
        # All 5 keys still present.
        assert set(final.keys()) == {
            "redis_lb_ip",
            "rabbitmq_lb_ip",
            "asterisk_call_lb_ip",
            "asterisk_registrar_lb_ip",
            "asterisk_conference_lb_ip",
        }, f"PR-T2 partial-reharvest dropped prior keys. Got: {final}"
        # Updated keys overridden.
        assert final["redis_lb_ip"] == "new_redis"
        # Prior keys preserved.
        assert final["asterisk_call_lb_ip"] == "old_call"

    def test_corrupted_prior_k8s_outputs_does_not_crash(self):
        """If state.yaml.k8s_outputs is somehow not a dict (manual edit,
        old format), the merge must coerce to {} and still write the new
        harvest rather than raising."""
        prior_state = _state_with_only_reconcile_pending()
        prior_state["k8s_outputs"] = "not-a-dict"  # corrupted
        saved = _run_pipeline_with_one_reconcile_stage(
            {"redis_lb_ip": "10.164.0.52"}, prior_state
        )
        final = saved[-1]["k8s_outputs"]
        assert isinstance(final, dict)
        assert final["redis_lb_ip"] == "10.164.0.52"


class TestRunPipelineDoesNotPersistOnNonReconcileStages:
    """The sentinel handling must be gated to reconcile_k8s_outputs only.
    Other stages must not touch k8s_outputs (would risk wiping the disk
    state when a future stage is added that re-uses the outputs dict)."""

    def test_terraform_init_save_does_not_add_k8s_outputs_key(self):
        # Pipeline state where only terraform_init is pending.
        stages = {s: "complete" for s in APPLY_STAGES}
        stages["terraform_init"] = "pending"
        initial = {"deployment_state": "applying", "stages": stages}

        cfg = MagicMock()
        saved: list = []

        def _cap(s):
            import copy
            saved.append(copy.deepcopy(s))

        with patch(
            "scripts.pipeline.load_state", return_value=dict(initial)
        ), patch(
            "scripts.pipeline.save_state", side_effect=_cap
        ), patch(
            "scripts.pipeline.terraform_init", return_value=True
        ), patch(
            "scripts.pipeline.terraform_output", return_value={}
        ):
            ok = run_pipeline(cfg, dry_run=False, auto_approve=True)
        assert ok is True
        for s in saved:
            # k8s_outputs should not be CREATED by a non-reconcile stage
            # save. (It may be absent or present with prior content; what
            # matters is no spurious key emerges.)
            assert s.get("k8s_outputs", None) in (None, {}), (
                f"Non-reconcile stage added k8s_outputs: {s.get('k8s_outputs')}"
            )
