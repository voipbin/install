"""Tests for scripts/pipeline.py — checkpoint save/load and stage ordering."""

import sys
from pathlib import Path
from unittest.mock import MagicMock, call, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import yaml

from scripts.pipeline import (
    APPLY_STAGES,
    DESTROY_STATE_DETACH,
    STAGE_LABELS,
    STAGE_RUNNERS,
    _initial_stages_state,
    clear_state,
    load_state,
    save_state,
)


class TestCheckpointSaveLoad:
    def test_save_and_load(self, tmp_path, monkeypatch):
        state_file = tmp_path / ".voipbin-state.yaml"
        monkeypatch.setattr("scripts.pipeline.STATE_FILE", state_file)

        state = {
            "deployment_state": "applying",
            "last_stage": "terraform_apply",
            "stages": {
                "terraform_init": "complete",
                "terraform_apply": "running",
                "ansible_run": "pending",
                "k8s_apply": "pending",
            },
        }
        save_state(state)

        assert state_file.exists()
        loaded = load_state()
        assert loaded["deployment_state"] == "applying"
        assert loaded["last_stage"] == "terraform_apply"
        assert loaded["stages"]["terraform_init"] == "complete"
        assert "timestamp" in loaded

    def test_load_empty(self, tmp_path, monkeypatch):
        state_file = tmp_path / ".voipbin-state.yaml"
        monkeypatch.setattr("scripts.pipeline.STATE_FILE", state_file)
        loaded = load_state()
        assert loaded == {}

    def test_clear_state(self, tmp_path, monkeypatch):
        state_file = tmp_path / ".voipbin-state.yaml"
        monkeypatch.setattr("scripts.pipeline.STATE_FILE", state_file)

        save_state({"deployment_state": "deployed"})
        assert state_file.exists()

        clear_state()
        assert not state_file.exists()

    def test_clear_state_no_file(self, tmp_path, monkeypatch):
        state_file = tmp_path / ".voipbin-state.yaml"
        monkeypatch.setattr("scripts.pipeline.STATE_FILE", state_file)
        # Should not raise
        clear_state()

    def test_save_adds_timestamp(self, tmp_path, monkeypatch):
        state_file = tmp_path / ".voipbin-state.yaml"
        monkeypatch.setattr("scripts.pipeline.STATE_FILE", state_file)

        save_state({"deployment_state": "applying"})
        loaded = load_state()
        assert "timestamp" in loaded
        assert loaded["timestamp"]  # Not empty

    def test_roundtrip_preserves_all_fields(self, tmp_path, monkeypatch):
        state_file = tmp_path / ".voipbin-state.yaml"
        monkeypatch.setattr("scripts.pipeline.STATE_FILE", state_file)

        original = {
            "deployment_state": "deployed",
            "last_stage": "k8s_apply",
            "stages": _initial_stages_state(),
            "custom_field": "test_value",
        }
        save_state(original)
        loaded = load_state()
        assert loaded["deployment_state"] == "deployed"
        assert loaded["custom_field"] == "test_value"
        assert loaded["last_stage"] == "k8s_apply"

    def test_load_invalid_yaml_returns_empty(self, tmp_path, monkeypatch):
        state_file = tmp_path / ".voipbin-state.yaml"
        monkeypatch.setattr("scripts.pipeline.STATE_FILE", state_file)
        state_file.write_text("not_a_dict_value")
        loaded = load_state()
        assert loaded == {}


class TestStageOrdering:
    def test_apply_stages_order(self):
        # PR-R reordered: k8s_apply + reconcile_k8s_outputs now precede ansible_run
        # so kamailio's .env can be rendered with harvested k8s LB IPs.
        # PR-Z inserts cert_provision between reconcile_k8s_outputs and ansible_run
        # so kamailio's TLS certs are issued before the playbook deploys them.
        expected = (
            "terraform_init",
            "reconcile_imports",
            "terraform_apply",
            "reconcile_outputs",
            "k8s_apply",
            "reconcile_k8s_outputs",
            "cert_provision",
            "ansible_run",
        )
        assert APPLY_STAGES == expected

    def test_all_stages_have_runners(self):
        for stage in APPLY_STAGES:
            assert stage in STAGE_RUNNERS, f"Missing runner for {stage}"

    def test_all_stages_have_labels(self):
        for stage in APPLY_STAGES:
            assert stage in STAGE_LABELS, f"Missing label for {stage}"

    def test_initial_stages_all_pending(self):
        stages = _initial_stages_state()
        for stage in APPLY_STAGES:
            assert stages[stage] == "pending"

    def test_initial_stages_contains_all(self):
        stages = _initial_stages_state()
        assert set(stages.keys()) == set(APPLY_STAGES)


class TestPipelineDiagnosis:
    @patch("scripts.pipeline.print_fix")
    @patch("scripts.pipeline.diagnose_stage_failure", return_value=["hint1"])
    def test_diagnosis_called_on_failure(self, mock_diag, mock_fix):
        """After a stage fails, diagnose_stage_failure is called and print_fix renders hints."""
        from scripts.pipeline import run_pipeline
        config = MagicMock()
        config.get.return_value = "my-project"
        # Make terraform_init fail
        with patch("scripts.pipeline.STAGE_RUNNERS", {"terraform_init": MagicMock(return_value=False)}), \
             patch("scripts.pipeline.load_state", return_value={}), \
             patch("scripts.pipeline.save_state"):
            run_pipeline(config, only_stage="terraform_init")
        mock_diag.assert_called_once_with(config, "terraform_init")
        mock_fix.assert_called_once_with("Likely causes", ["hint1"])

    @patch("scripts.pipeline.diagnose_stage_failure")
    def test_no_diagnosis_on_success(self, mock_diag):
        from scripts.pipeline import run_pipeline
        config = MagicMock()
        with patch("scripts.pipeline.STAGE_RUNNERS", {"terraform_init": MagicMock(return_value=True)}), \
             patch("scripts.pipeline.load_state", return_value={}), \
             patch("scripts.pipeline.save_state"):
            run_pipeline(config, only_stage="terraform_init")
        mock_diag.assert_not_called()


class TestRunPipelineStateClobber:
    """Regression guard for PR-AF: run_pipeline's outer save_state must not
    clobber sub-key mutations (e.g. cert_state) written by a runner that calls
    save_state internally."""

    def test_runner_cert_state_survives_outer_save(self, tmp_path, monkeypatch):
        """T1: Simulate _run_cert_provision writing cert_state to disk inside
        the runner. The outer run_pipeline save_state must not clobber it.

        Uses a real temp STATE_FILE (not mocked save_state) so YAML round-trips
        are exercised and a false-passing test cannot hide the regression."""
        from scripts.pipeline import run_pipeline, load_state, save_state

        state_file = tmp_path / ".voipbin-state.yaml"
        monkeypatch.setattr("scripts.pipeline.STATE_FILE", state_file)

        def fake_cert_runner(config, outputs, dry_run, auto_approve):
            inner_state = load_state()
            inner_state["cert_state"] = {
                "actual_mode": "self_signed",
                "ca_fingerprint_sha256": "AA:BB:CC",
            }
            save_state(inner_state)
            return True

        config = MagicMock()
        config.get.return_value = "voipbin-install-dev"

        monkeypatch.setattr("scripts.pipeline.STAGE_RUNNERS",
                            {"cert_provision": fake_cert_runner})
        monkeypatch.setattr("scripts.pipeline.terraform_output", lambda c: {})

        ok = run_pipeline(config, only_stage="cert_provision", auto_approve=True)

        assert ok, "run_pipeline must return True when runner succeeds"
        final = load_state()
        assert final.get("cert_state") is not None, (
            "cert_state was clobbered by run_pipeline's outer save_state — PR-AF regression"
        )
        assert final["cert_state"].get("actual_mode") == "self_signed", (
            "cert_state content was corrupted"
        )

    def test_stage_marked_complete_after_reload(self, tmp_path, monkeypatch):
        """T2: After the PR-AF reload, the stage must still be marked 'complete'
        in state.yaml — the reload must not prevent stage status persistence."""
        from scripts.pipeline import run_pipeline, load_state, save_state

        state_file = tmp_path / ".voipbin-state.yaml"
        monkeypatch.setattr("scripts.pipeline.STATE_FILE", state_file)

        def fake_runner(config, outputs, dry_run, auto_approve):
            inner_state = load_state()
            inner_state["cert_state"] = {"actual_mode": "self_signed"}
            save_state(inner_state)
            return True

        config = MagicMock()
        config.get.return_value = "voipbin-install-dev"

        monkeypatch.setattr("scripts.pipeline.STAGE_RUNNERS",
                            {"cert_provision": fake_runner})
        monkeypatch.setattr("scripts.pipeline.terraform_output", lambda c: {})

        run_pipeline(config, only_stage="cert_provision", auto_approve=True)

        final = load_state()
        assert final.get("stages", {}).get("cert_provision") == "complete", (
            "cert_provision stage must be marked 'complete' in state after PR-AF reload"
        )


class TestDestroyPipeline:
    """Tests for destroy_pipeline (T-AI-0 through T-AI-3b)."""

    def test_tai_0_destroy_state_detach_contents(self):
        """T-AI-0: DESTROY_STATE_DETACH contains exactly the four expected addresses in order.

        PR-AJ: google_compute_instance_group.kamailio removed — it is a plain
        unmanaged IG whose lifecycle is fully owned by terraform (destroyed before
        VPC in dependency graph). State-detaching it leaves a GCP orphan that
        conflicts with the next apply (wrongNetwork error).
        """
        assert DESTROY_STATE_DETACH == [
            "google_kms_crypto_key.voipbin_sops_key",
            "google_kms_key_ring.voipbin_sops",
            "google_storage_bucket.terraform_state",
            "google_container_node_pool.voipbin",
        ]
        assert "google_compute_instance_group.kamailio" not in DESTROY_STATE_DETACH, (
            "kamailio IG must NOT be in DESTROY_STATE_DETACH — state-detaching it "
            "leaves a GCP orphan causing wrongNetwork conflict on next apply."
        )

    def test_tai_1_state_rm_called_before_destroy(self, tmp_path, monkeypatch):
        """T-AI-1: destroy_pipeline calls terraform_state_rm before terraform_destroy."""
        from scripts.pipeline import destroy_pipeline

        state_file = tmp_path / ".voipbin-state.yaml"
        monkeypatch.setattr("scripts.pipeline.STATE_FILE", state_file)

        call_order = []

        def mock_state_rm(resources):
            call_order.append("terraform_state_rm")
            return True

        def mock_k8s_delete(config):
            call_order.append("k8s_delete")
            return True

        def mock_tf_destroy(config, auto_approve=False):
            call_order.append("terraform_destroy")
            return True

        monkeypatch.setattr("scripts.pipeline.terraform_state_rm", mock_state_rm)
        monkeypatch.setattr("scripts.pipeline.k8s_delete", mock_k8s_delete)
        monkeypatch.setattr("scripts.pipeline.terraform_destroy", mock_tf_destroy)
        monkeypatch.setattr("scripts.pipeline.TERRAFORM_DIR", tmp_path)

        config = MagicMock()
        destroy_pipeline(config, auto_approve=True)

        assert call_order.index("terraform_state_rm") < call_order.index("terraform_destroy"), (
            "terraform_state_rm must be called before terraform_destroy"
        )

    def test_tai_2_destroy_continues_when_state_rm_fails(self, tmp_path, monkeypatch):
        """T-AI-2: destroy_pipeline calls terraform_destroy even when terraform_state_rm returns False."""
        from scripts.pipeline import destroy_pipeline

        state_file = tmp_path / ".voipbin-state.yaml"
        monkeypatch.setattr("scripts.pipeline.STATE_FILE", state_file)

        tf_destroy_called = []

        def mock_state_rm(resources):
            return False  # State rm failed

        def mock_k8s_delete(config):
            return True

        def mock_tf_destroy(config, auto_approve=False):
            tf_destroy_called.append(True)
            return True  # destroy succeeds

        monkeypatch.setattr("scripts.pipeline.terraform_state_rm", mock_state_rm)
        monkeypatch.setattr("scripts.pipeline.k8s_delete", mock_k8s_delete)
        monkeypatch.setattr("scripts.pipeline.terraform_destroy", mock_tf_destroy)
        monkeypatch.setattr("scripts.pipeline.TERRAFORM_DIR", tmp_path)

        config = MagicMock()
        result = destroy_pipeline(config, auto_approve=True)

        assert tf_destroy_called, "terraform_destroy must be called even when state_rm returns False"
        assert result is True, "return value must be from terraform_destroy (True)"

    def test_tai_2_return_value_from_tf_destroy(self, tmp_path, monkeypatch):
        """T-AI-2 (failure variant): return value comes from terraform_destroy, not state_rm."""
        from scripts.pipeline import destroy_pipeline

        state_file = tmp_path / ".voipbin-state.yaml"
        monkeypatch.setattr("scripts.pipeline.STATE_FILE", state_file)

        monkeypatch.setattr("scripts.pipeline.terraform_state_rm", lambda r: False)
        monkeypatch.setattr("scripts.pipeline.k8s_delete", lambda c: True)
        monkeypatch.setattr("scripts.pipeline.terraform_destroy", lambda c, auto_approve=False: False)
        monkeypatch.setattr("scripts.pipeline.TERRAFORM_DIR", tmp_path)

        config = MagicMock()
        result = destroy_pipeline(config, auto_approve=True)
        assert result is False, "return value must be False when terraform_destroy returns False"

    def test_tai_3_errored_tfstate_removed_if_exists(self, tmp_path, monkeypatch):
        """T-AI-3 (file exists): errored.tfstate is removed after destroy."""
        from scripts.pipeline import destroy_pipeline

        state_file = tmp_path / ".voipbin-state.yaml"
        monkeypatch.setattr("scripts.pipeline.STATE_FILE", state_file)
        monkeypatch.setattr("scripts.pipeline.TERRAFORM_DIR", tmp_path)

        # Create the errored.tfstate file
        errored = tmp_path / "errored.tfstate"
        errored.write_text("{}")

        monkeypatch.setattr("scripts.pipeline.terraform_state_rm", lambda r: True)
        monkeypatch.setattr("scripts.pipeline.k8s_delete", lambda c: True)
        monkeypatch.setattr("scripts.pipeline.terraform_destroy", lambda c, auto_approve=False: True)

        config = MagicMock()
        destroy_pipeline(config, auto_approve=True)

        assert not errored.exists(), "errored.tfstate must be removed after destroy"

    def test_tai_3_no_error_when_errored_tfstate_absent(self, tmp_path, monkeypatch):
        """T-AI-3 (file absent): no error raised when errored.tfstate does not exist."""
        from scripts.pipeline import destroy_pipeline

        state_file = tmp_path / ".voipbin-state.yaml"
        monkeypatch.setattr("scripts.pipeline.STATE_FILE", state_file)
        monkeypatch.setattr("scripts.pipeline.TERRAFORM_DIR", tmp_path)

        monkeypatch.setattr("scripts.pipeline.terraform_state_rm", lambda r: True)
        monkeypatch.setattr("scripts.pipeline.k8s_delete", lambda c: True)
        monkeypatch.setattr("scripts.pipeline.terraform_destroy", lambda c, auto_approve=False: True)

        config = MagicMock()
        # Should not raise FileNotFoundError
        result = destroy_pipeline(config, auto_approve=True)
        assert result is True

    def test_tai_3b_destroy_failed_state_after_errored_cleanup(self, tmp_path, monkeypatch):
        """T-AI-3b: On failure path, deployment_state is 'destroy_failed' after errored.tfstate cleanup."""
        from scripts.pipeline import destroy_pipeline, load_state

        state_file = tmp_path / ".voipbin-state.yaml"
        monkeypatch.setattr("scripts.pipeline.STATE_FILE", state_file)
        monkeypatch.setattr("scripts.pipeline.TERRAFORM_DIR", tmp_path)

        # Create errored.tfstate
        errored = tmp_path / "errored.tfstate"
        errored.write_text("{}")

        monkeypatch.setattr("scripts.pipeline.terraform_state_rm", lambda r: True)
        monkeypatch.setattr("scripts.pipeline.k8s_delete", lambda c: True)
        monkeypatch.setattr("scripts.pipeline.terraform_destroy", lambda c, auto_approve=False: False)

        config = MagicMock()
        result = destroy_pipeline(config, auto_approve=True)

        assert result is False
        assert not errored.exists(), "errored.tfstate must be removed even on failure path"
        state = load_state()
        assert state.get("deployment_state") == "destroy_failed", (
            "deployment_state must be 'destroy_failed' on terraform_destroy failure"
        )
