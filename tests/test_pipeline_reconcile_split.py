"""Tests for PR-A — pipeline reconcile split (imports + outputs).

Covers:
  - APPLY_STAGES order + STAGE_RUNNERS/STAGE_LABELS wiring.
  - reconcile_outputs no-op behaviour with empty FIELD_MAP.
  - reconcile_outputs validator / already-set guards.
  - Backward-compat `reconcile` alias.
  - load_state() legacy `terraform_reconcile` migration table (complete/failed/
    running/pending) + idempotency + unknown-key preservation.
  - --stage shim: runtime expansion + CLI surface.
  - reconcile_outputs precondition (requires terraform_apply complete).
  - diagnosis hints recognize both split stage names.
  - Resume skips completed stages.
  - tf_outputs flows into reconcile_outputs runner.

All tests use pure mocks via monkeypatch; no subprocess or filesystem I/O
beyond tmp_path.
"""

import sys
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts import terraform_reconcile
from scripts.pipeline import (
    APPLY_STAGES,
    DEPRECATION_MESSAGE_RECONCILE,
    STAGE_LABELS,
    STAGE_RUNNERS,
    _migrate_legacy_reconcile_state,
    _run_reconcile_imports,
    _run_reconcile_outputs,
    load_state,
    run_pipeline,
    save_state,
)


# ---------------------------------------------------------------------------
# 1. APPLY_STAGES order
# ---------------------------------------------------------------------------

class TestApplyStagesOrder:
    def test_apply_stages_order(self):
        """Stages: init → imports → apply → outputs → k8s → reconcile_k8s → ansible.
        PR-R reordered k8s_apply + reconcile_k8s_outputs to precede ansible_run."""
        assert APPLY_STAGES == (
            "terraform_init",
            "reconcile_imports",
            "terraform_apply",
            "reconcile_outputs",
            "k8s_apply",
            "reconcile_k8s_outputs",
            "cert_provision",
            "ansible_run",
        )

    def test_reconcile_imports_before_apply(self):
        i = APPLY_STAGES.index("reconcile_imports")
        a = APPLY_STAGES.index("terraform_apply")
        assert i < a

    def test_reconcile_outputs_after_apply(self):
        a = APPLY_STAGES.index("terraform_apply")
        o = APPLY_STAGES.index("reconcile_outputs")
        assert a < o


# ---------------------------------------------------------------------------
# 2. STAGE_RUNNERS keys
# ---------------------------------------------------------------------------

class TestStageRunnersKeys:
    def test_stage_runners_keys(self):
        assert "reconcile_imports" in STAGE_RUNNERS
        assert "reconcile_outputs" in STAGE_RUNNERS
        assert "terraform_reconcile" not in STAGE_RUNNERS

    def test_stage_labels_present(self):
        assert "reconcile_imports" in STAGE_LABELS
        assert "reconcile_outputs" in STAGE_LABELS
        assert "terraform_reconcile" not in STAGE_LABELS


# ---------------------------------------------------------------------------
# 3-5. reconcile_outputs (FIELD_MAP) behaviour
# ---------------------------------------------------------------------------

class TestReconcileOutputs:
    def test_reconcile_outputs_noop_empty_field_map(self, monkeypatch):
        """With empty FIELD_MAP, outputs() returns True and writes nothing."""
        monkeypatch.setattr(terraform_reconcile, "FIELD_MAP", [])
        config = MagicMock()
        ok = terraform_reconcile.outputs(config, {"some_key": "value"})
        assert ok is True
        config.set.assert_not_called()
        config.save.assert_not_called()

    def test_reconcile_outputs_skips_when_config_already_set(self, monkeypatch):
        """If config slot is already populated, outputs() does NOT overwrite."""
        mapping = terraform_reconcile.TfOutputFieldMapping(
            tf_key="kamailio_ip",
            cfg_key="kamailio_ip",
        )
        monkeypatch.setattr(terraform_reconcile, "FIELD_MAP", [mapping])
        config = MagicMock()
        # Already set
        config.get.return_value = "10.0.0.5"
        ok = terraform_reconcile.outputs(config, {"kamailio_ip": "10.0.0.99"})
        assert ok is True
        config.set.assert_not_called()
        config.save.assert_not_called()

    def test_reconcile_outputs_validates_value(self, monkeypatch):
        """Validator returning False causes the field to be skipped + warned."""
        mapping = terraform_reconcile.TfOutputFieldMapping(
            tf_key="ip",
            cfg_key="ip",
            validator=lambda v: v.startswith("10."),
        )
        monkeypatch.setattr(terraform_reconcile, "FIELD_MAP", [mapping])
        warnings: list[str] = []
        monkeypatch.setattr(
            "scripts.terraform_reconcile.print_warning",
            lambda msg: warnings.append(msg),
        )
        config = MagicMock()
        config.get.return_value = None  # cfg slot empty
        ok = terraform_reconcile.outputs(config, {"ip": "bogus-not-an-ip"})
        assert ok is True
        config.set.assert_not_called()
        assert any("Invalid output" in m for m in warnings)

    def test_reconcile_outputs_writes_when_unset_and_valid(self, monkeypatch):
        """Sanity: valid value into empty slot triggers set+save."""
        mapping = terraform_reconcile.TfOutputFieldMapping(
            tf_key="ip",
            cfg_key="ip",
        )
        monkeypatch.setattr(terraform_reconcile, "FIELD_MAP", [mapping])
        config = MagicMock()
        config.get.return_value = None
        ok = terraform_reconcile.outputs(config, {"ip": "10.0.0.1"})
        assert ok is True
        config.set.assert_called_once_with("ip", "10.0.0.1")
        config.save.assert_called_once()


# ---------------------------------------------------------------------------
# 6. reconcile() alias still dispatches to imports()
# ---------------------------------------------------------------------------

class TestReconcileAlias:
    def test_reconcile_alias_points_to_imports(self):
        """The `reconcile` symbol is bound to `imports` at module load."""
        assert terraform_reconcile.reconcile is terraform_reconcile.imports


# ---------------------------------------------------------------------------
# 7-12. State migration
# ---------------------------------------------------------------------------

class TestStateMigration:
    def test_state_migration_complete(self):
        state = {"stages": {"terraform_reconcile": "complete"}}
        out = _migrate_legacy_reconcile_state(state)
        assert "terraform_reconcile" not in out["stages"]
        assert out["stages"]["reconcile_imports"] == "complete"
        assert out["stages"]["reconcile_outputs"] == "pending"

    def test_state_migration_failed(self):
        state = {"stages": {"terraform_reconcile": "failed"}}
        out = _migrate_legacy_reconcile_state(state)
        assert out["stages"]["reconcile_imports"] == "failed"
        assert out["stages"]["reconcile_outputs"] == "pending"

    def test_state_migration_running(self):
        """`running` (interrupted) maps to failed → operator re-runs."""
        state = {"stages": {"terraform_reconcile": "running"}}
        out = _migrate_legacy_reconcile_state(state)
        assert out["stages"]["reconcile_imports"] == "failed"
        assert out["stages"]["reconcile_outputs"] == "pending"

    def test_state_migration_pending(self):
        state = {"stages": {"terraform_reconcile": "pending"}}
        out = _migrate_legacy_reconcile_state(state)
        assert out["stages"]["reconcile_imports"] == "pending"
        assert out["stages"]["reconcile_outputs"] == "pending"

    def test_state_migration_unknown_legacy_value(self):
        """Unknown legacy value falls through to default (pending, pending)."""
        state = {"stages": {"terraform_reconcile": "weird-status"}}
        out = _migrate_legacy_reconcile_state(state)
        assert "terraform_reconcile" not in out["stages"]
        assert out["stages"]["reconcile_imports"] == "pending"
        assert out["stages"]["reconcile_outputs"] == "pending"

    def test_state_migration_idempotent(self):
        state = {"stages": {"terraform_reconcile": "complete"}}
        once = _migrate_legacy_reconcile_state(state)
        twice = _migrate_legacy_reconcile_state(once)
        assert twice == once
        assert "terraform_reconcile" not in twice["stages"]

    def test_state_migration_preserves_unknown_keys(self):
        state = {"stages": {
            "terraform_reconcile": "complete",
            "my_custom_stage": "complete",
            "terraform_init": "complete",
        }}
        out = _migrate_legacy_reconcile_state(state)
        assert out["stages"]["my_custom_stage"] == "complete"
        assert out["stages"]["terraform_init"] == "complete"
        assert "terraform_reconcile" not in out["stages"]

    def test_state_migration_both_keys_new_wins(self):
        """If both legacy and new keys exist, legacy is dropped and new keys win."""
        state = {"stages": {
            "terraform_reconcile": "complete",
            "reconcile_imports": "failed",
            "reconcile_outputs": "complete",
        }}
        out = _migrate_legacy_reconcile_state(state)
        assert "terraform_reconcile" not in out["stages"]
        assert out["stages"]["reconcile_imports"] == "failed"
        assert out["stages"]["reconcile_outputs"] == "complete"

    def test_state_migration_via_load_state(self, tmp_path, monkeypatch):
        """End-to-end: legacy state file loads with migration applied."""
        state_file = tmp_path / ".voipbin-state.yaml"
        monkeypatch.setattr("scripts.pipeline.STATE_FILE", state_file)
        save_state({
            "deployment_state": "deployed",
            "stages": {
                "terraform_init": "complete",
                "terraform_reconcile": "complete",
                "terraform_apply": "complete",
            },
        })
        loaded = load_state()
        assert "terraform_reconcile" not in loaded["stages"]
        assert loaded["stages"]["reconcile_imports"] == "complete"
        assert loaded["stages"]["reconcile_outputs"] == "pending"


# ---------------------------------------------------------------------------
# 13-14. --stage legacy shim
# ---------------------------------------------------------------------------

class TestLegacyShim:
    def test_only_stage_legacy_shim_runtime(self, tmp_path, monkeypatch):
        """run_pipeline(only_stage='terraform_reconcile') runs BOTH new stages."""
        state_file = tmp_path / ".voipbin-state.yaml"
        monkeypatch.setattr("scripts.pipeline.STATE_FILE", state_file)
        # Pre-existing state with terraform_apply complete (so reconcile_outputs
        # precondition is satisfied if hit; we only care that both run).
        save_state({
            "stages": {
                "terraform_init": "complete",
                "reconcile_imports": "pending",
                "terraform_apply": "complete",
                "reconcile_outputs": "pending",
                "ansible_run": "pending",
                "k8s_apply": "pending",
            },
        })

        calls = []
        fake_runners = {
            "reconcile_imports": MagicMock(return_value=True, side_effect=lambda *a, **k: calls.append("reconcile_imports") or True),
            "reconcile_outputs": MagicMock(return_value=True, side_effect=lambda *a, **k: calls.append("reconcile_outputs") or True),
        }
        warnings: list[str] = []
        monkeypatch.setattr("scripts.pipeline.print_warning", lambda m: warnings.append(m))
        monkeypatch.setattr("scripts.pipeline.STAGE_RUNNERS", fake_runners)
        monkeypatch.setattr("scripts.pipeline.terraform_output", lambda c: {})

        config = MagicMock()
        ok = run_pipeline(config, only_stage="terraform_reconcile")
        assert ok is True
        assert calls == ["reconcile_imports", "reconcile_outputs"]
        assert any("deprecated" in m.lower() for m in warnings)

    def test_only_stage_legacy_shim_cli(self, tmp_path, monkeypatch):
        """CLI surface still accepts the deprecated --stage terraform_reconcile."""
        from click.testing import CliRunner
        from scripts.cli import apply as apply_cmd

        monkeypatch.setattr("scripts.pipeline.STATE_FILE", tmp_path / ".voipbin-state.yaml")
        mock_run = MagicMock(return_value=True)
        monkeypatch.setattr("scripts.commands.apply.run_pipeline", mock_run)
        monkeypatch.setattr("scripts.commands.apply.run_pre_apply_checks", lambda *a, **k: True)
        monkeypatch.setattr("scripts.commands.apply.confirm", lambda *a, **k: True)

        fake_cfg = MagicMock()
        fake_cfg.exists.return_value = True
        fake_cfg.validate.return_value = []
        fake_cfg.get.return_value = "demo"
        monkeypatch.setattr("scripts.commands.apply.InstallerConfig", lambda: fake_cfg)

        runner = CliRunner()
        result = runner.invoke(apply_cmd, ["--stage", "terraform_reconcile", "--auto-approve"])
        assert result.exit_code == 0, result.output
        # CLI must pass the legacy stage name through; shim expansion lives inside
        # run_pipeline itself.
        assert mock_run.called
        assert mock_run.call_args.kwargs.get("only_stage") == "terraform_reconcile"


# ---------------------------------------------------------------------------
# 15. reconcile_outputs precondition
# ---------------------------------------------------------------------------

class TestReconcileOutputsPrecondition:
    def test_reconcile_outputs_precondition_apply_incomplete(self, tmp_path, monkeypatch):
        """Standalone --stage reconcile_outputs with apply incomplete → False."""
        state_file = tmp_path / ".voipbin-state.yaml"
        monkeypatch.setattr("scripts.pipeline.STATE_FILE", state_file)
        save_state({
            "stages": {
                "terraform_init": "complete",
                "reconcile_imports": "complete",
                "terraform_apply": "pending",
                "reconcile_outputs": "pending",
                "ansible_run": "pending",
                "k8s_apply": "pending",
            },
        })
        errors: list[str] = []
        monkeypatch.setattr("scripts.pipeline.print_error", lambda m: errors.append(m))
        config = MagicMock()
        ok = run_pipeline(config, only_stage="reconcile_outputs")
        assert ok is False
        assert any("terraform_apply" in m for m in errors)

    def test_reconcile_outputs_precondition_ok_when_apply_complete(
        self, tmp_path, monkeypatch
    ):
        state_file = tmp_path / ".voipbin-state.yaml"
        monkeypatch.setattr("scripts.pipeline.STATE_FILE", state_file)
        save_state({
            "stages": {
                "terraform_init": "complete",
                "reconcile_imports": "complete",
                "terraform_apply": "complete",
                "reconcile_outputs": "pending",
                "ansible_run": "pending",
                "k8s_apply": "pending",
            },
        })
        monkeypatch.setattr(
            "scripts.pipeline.STAGE_RUNNERS",
            {"reconcile_outputs": MagicMock(return_value=True)},
        )
        monkeypatch.setattr("scripts.pipeline.terraform_output", lambda c: {"k": "v"})
        config = MagicMock()
        ok = run_pipeline(config, only_stage="reconcile_outputs")
        assert ok is True


# ---------------------------------------------------------------------------
# 16. diagnosis recognizes split stages
# ---------------------------------------------------------------------------

class TestDiagnosisRecognizesSplitStages:
    def test_diagnosis_recognizes_split_stages(self, monkeypatch):
        """diagnose_stage_failure() runs the same TF-stage hints for both."""
        from scripts import diagnosis

        # Short-circuit ADC and billing checks to a stable state.
        monkeypatch.setattr(
            diagnosis, "check_application_default_credentials", lambda: (True, "")
        )
        monkeypatch.setattr(diagnosis, "check_billing_tristate", lambda p: "enabled")
        monkeypatch.setattr(diagnosis, "check_required_apis", lambda p: [])
        monkeypatch.setattr(diagnosis, "check_quotas", lambda p, r: [])

        # Cause the state-bucket check to "fail" so we get a deterministic hint.
        class _R:
            returncode = 1
            stdout = ""
            stderr = ""
        monkeypatch.setattr(diagnosis, "run_cmd", lambda *a, **k: _R())

        config = MagicMock()
        config.get.side_effect = lambda k, d=None: {
            "gcp_project_id": "p", "region": "us-east1",
        }.get(k, d)

        hints_imports = diagnosis.diagnose_stage_failure(config, "reconcile_imports")
        hints_outputs = diagnosis.diagnose_stage_failure(config, "reconcile_outputs")
        assert any("state bucket" in h for h in hints_imports)
        assert any("state bucket" in h for h in hints_outputs)


# ---------------------------------------------------------------------------
# 17. Resume skips completed stages
# ---------------------------------------------------------------------------

class TestPipelineFlowSkipsCompleted:
    def test_pipeline_flow_skips_completed(self, tmp_path, monkeypatch):
        state_file = tmp_path / ".voipbin-state.yaml"
        monkeypatch.setattr("scripts.pipeline.STATE_FILE", state_file)
        save_state({
            "stages": {
                "terraform_init":          "complete",
                "reconcile_imports":       "complete",
                "terraform_apply":         "complete",
                "reconcile_outputs":       "pending",
                "k8s_apply":               "pending",
                "reconcile_k8s_outputs":   "pending",
                "cert_provision":          "pending",
                "ansible_run":             "pending",
            },
        })

        called: list[str] = []

        def make_runner(name):
            def _r(*a, **k):
                called.append(name)
                return True
            return _r

        monkeypatch.setattr(
            "scripts.pipeline.STAGE_RUNNERS",
            {s: make_runner(s) for s in APPLY_STAGES},
        )
        monkeypatch.setattr("scripts.pipeline.terraform_output", lambda c: {})

        config = MagicMock()
        ok = run_pipeline(config)
        assert ok is True
        # Only the not-yet-complete stages should have been executed.
        assert called == [
            "reconcile_outputs",
            "k8s_apply",
            "reconcile_k8s_outputs",
            "cert_provision",
            "ansible_run",
        ]


# ---------------------------------------------------------------------------
# 18. tf_outputs flow to reconcile_outputs runner
# ---------------------------------------------------------------------------

class TestTfOutputsPassedToReconcileOutputs:
    def test_tf_outputs_passed_to_reconcile_outputs(self, monkeypatch):
        """The reconcile_outputs runner forwards tf_outputs to terraform_reconcile.outputs."""
        received = {}

        def fake_outputs(config, tf_outputs):
            received["cfg"] = config
            received["tf"] = tf_outputs
            return True

        monkeypatch.setattr("scripts.pipeline._terraform_outputs", fake_outputs)
        cfg = MagicMock()
        ok = _run_reconcile_outputs(cfg, {"kamailio_ip": "10.0.0.5"}, False, False)
        assert ok is True
        assert received["cfg"] is cfg
        assert received["tf"] == {"kamailio_ip": "10.0.0.5"}

    def test_reconcile_outputs_dry_run_early_returns(self, monkeypatch):
        """Dry-run short-circuits the runner without calling terraform_reconcile.outputs."""
        called = []
        monkeypatch.setattr(
            "scripts.pipeline._terraform_outputs",
            lambda c, t: called.append("nope") or True,
        )
        ok = _run_reconcile_outputs(MagicMock(), {}, True, False)
        assert ok is True
        assert called == []

    def test_reconcile_imports_dry_run_early_returns(self, monkeypatch):
        called = []
        monkeypatch.setattr(
            "scripts.pipeline._terraform_imports",
            lambda c: called.append("nope") or True,
        )
        ok = _run_reconcile_imports(MagicMock(), {}, True, False)
        assert ok is True
        assert called == []


# ---------------------------------------------------------------------------
# Sanity: deprecation message content
# ---------------------------------------------------------------------------

def test_deprecation_message_mentions_both_new_stages():
    assert "reconcile_imports" in DEPRECATION_MESSAGE_RECONCILE
    assert "reconcile_outputs" in DEPRECATION_MESSAGE_RECONCILE
