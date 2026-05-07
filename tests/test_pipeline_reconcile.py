"""Tests that terraform_reconcile is wired into the pipeline correctly."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.pipeline import APPLY_STAGES, STAGE_LABELS, STAGE_RUNNERS, _run_terraform_reconcile


class TestPipelineStageOrder:
    def test_terraform_reconcile_is_in_apply_stages(self):
        assert "terraform_reconcile" in APPLY_STAGES

    def test_terraform_reconcile_after_init_and_before_apply(self):
        stages = list(APPLY_STAGES)
        init_idx = stages.index("terraform_init")
        reconcile_idx = stages.index("terraform_reconcile")
        apply_idx = stages.index("terraform_apply")
        assert init_idx < reconcile_idx < apply_idx

    def test_terraform_reconcile_has_label(self):
        assert "terraform_reconcile" in STAGE_LABELS

    def test_terraform_reconcile_has_runner(self):
        assert STAGE_RUNNERS.get("terraform_reconcile") is _run_terraform_reconcile
