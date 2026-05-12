"""Tests for scripts/pipeline.py — checkpoint save/load and stage ordering."""

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import yaml

from scripts.pipeline import (
    APPLY_STAGES,
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
        expected = (
            "terraform_init",
            "reconcile_imports",
            "terraform_apply",
            "reconcile_outputs",
            "k8s_apply",
            "reconcile_k8s_outputs",
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
