"""PR-R1 regression. CLI --stage choices must stay in sync with APPLY_STAGES.

Background. PR-R added `reconcile_k8s_outputs` to `scripts/pipeline.APPLY_STAGES`
but forgot to update the `click.Choice([...])` literal in `scripts/cli.py::apply`.
Calling `voipbin-install apply --stage reconcile_k8s_outputs` was rejected by
argparse/click before reaching `cmd_apply`, so the new stage was un-invokable
from the CLI.

This regression test pins the invariant. Any new stage added to APPLY_STAGES
in the future must also appear in the CLI choices, and the choices list must
not drift from the pipeline tuple plus the documented `terraform_reconcile`
alias.
"""

from __future__ import annotations

import click

from scripts.cli import cli as _cli_group
from scripts.pipeline import APPLY_STAGES


def _get_apply_stage_choices() -> list[str]:
    apply_cmd = _cli_group.get_command(None, "apply")  # type: ignore[arg-type]
    assert apply_cmd is not None, "cli is missing the `apply` subcommand"
    for param in apply_cmd.params:
        if param.name == "stage":
            assert isinstance(param.type, click.Choice), (
                "apply --stage must be a click.Choice, "
                "otherwise the regression invariant cannot be enforced"
            )
            return list(param.type.choices)
    raise AssertionError("apply command has no --stage option")


class TestApplyStageChoicesContainAllPipelineStages:
    """Every stage in APPLY_STAGES must appear in --stage choices."""

    def test_every_pipeline_stage_is_a_valid_cli_choice(self) -> None:
        choices = _get_apply_stage_choices()
        missing = [s for s in APPLY_STAGES if s not in choices]
        assert not missing, (
            f"APPLY_STAGES has stages missing from cli.py --stage choices: {missing}. "
            f"Add them to the click.Choice([...]) list in scripts/cli.py::apply."
        )

    def test_reconcile_k8s_outputs_is_specifically_present(self) -> None:
        # Explicit. Catches a regression of the exact PR-R bug.
        choices = _get_apply_stage_choices()
        assert "reconcile_k8s_outputs" in choices, (
            "reconcile_k8s_outputs missing from --stage choices. "
            "This is the PR-R1 regression — see "
            "docs/plans/2026-05-13-pr-r-pipeline-reorder-k8s-outputs-design.md."
        )

    def test_terraform_reconcile_alias_preserved(self) -> None:
        # Documented compat alias. Must remain accepted by CLI.
        choices = _get_apply_stage_choices()
        assert "terraform_reconcile" in choices, (
            "Deprecated alias `terraform_reconcile` was removed from --stage. "
            "If this is intentional, update PR-A migration docs and tests."
        )

    def test_choices_are_subset_of_apply_stages_plus_alias(self) -> None:
        # No spurious stages. CLI must not advertise stages the pipeline cannot run.
        choices = _get_apply_stage_choices()
        allowed = set(APPLY_STAGES) | {"terraform_reconcile"}
        unknown = [s for s in choices if s not in allowed]
        assert not unknown, (
            f"cli.py --stage exposes stages not in APPLY_STAGES nor the documented alias: "
            f"{unknown}. Either remove them from cli.py or add them to pipeline.APPLY_STAGES."
        )

    def test_cli_choice_order_matches_pipeline_order(self) -> None:
        # Soft invariant. Help text reads top-to-bottom; matching order avoids
        # operator surprise. Alias trails the real stages.
        choices = _get_apply_stage_choices()
        real_choices = [c for c in choices if c != "terraform_reconcile"]
        assert real_choices == list(APPLY_STAGES), (
            f"cli.py --stage choices order drifted from APPLY_STAGES.\n"
            f"  cli.py: {real_choices}\n"
            f"  pipeline: {list(APPLY_STAGES)}"
        )
