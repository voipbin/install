"""Tests for scripts/pipeline.py — checkpoint save/load and stage ordering."""

import sys
from pathlib import Path

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
        expected = ("terraform_init", "terraform_reconcile", "terraform_apply", "ansible_run", "k8s_apply")
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
