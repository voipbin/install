"""PR-Y regression: pipeline LB IP rehydration must override terraform's
empty-string placeholders with the harvested values from state.yaml.

Background: PR-R/PR-T2 (May 13 2026) chain stores k8s LoadBalancer
externalIPs in `state.yaml["k8s_outputs"]` after the reconcile_k8s_outputs
stage. When the operator later runs `voipbin-install apply --stage
ansible_run` separately, run_pipeline() rehydrates those values into the
`tf_outputs` dict that the ansible runner consumes.

The PR-R rehydration code used `tf_outputs.setdefault(k, v)`. That is
correct when terraform_output() does NOT emit the key — but the install
repo's `terraform/outputs.tf` declares:

    output "redis_lb_ip"    { value = google_compute_address.redis_lb.address }
    output "rabbitmq_lb_ip" { value = google_compute_address.rabbitmq_lb.address }

BEFORE the LoadBalancer Service is provisioned (or when the address
resource references a Service whose IP is still empty), `terraform output`
returns the literal empty string. The key EXISTS in terraform_outputs
with value "", so setdefault() does nothing and the persisted real
value (e.g. "10.0.0.8") never reaches the ansible runner. Kamailio then
CrashLoops at the container entrypoint with
"ERROR: Missing required environment variables: REDIS_CACHE_ADDRESS".

This surfaced in v6 dogfood iteration #6 (May 13 2026) on real GKE.

PR-Y replaces setdefault with truthy-override semantics: persisted state
is the authoritative source of truth for any LB IP it carries; the
terraform placeholder is a fallback that loses to non-empty persisted.

Tests below pin the contract so a future refactor cannot silently
reintroduce setdefault().
"""

from __future__ import annotations

import re
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest


REPO = Path(__file__).resolve().parent.parent
PIPELINE_PY = REPO / "scripts" / "pipeline.py"


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


# ============================================================================
# Source-level: the setdefault pattern must NOT be reintroduced.
# ============================================================================


class TestPipelineRehydrationSource:
    def test_setdefault_not_used_in_k8s_rehydration(self):
        """A future refactor that 'simplifies' the rehydration loop back
        to setdefault() would silently regress dogfood iteration #6. Pin
        the absence in the CODE (not comments)."""
        text = _read(PIPELINE_PY)
        # Locate the rehydration block by its anchor comment.
        anchor = text.find("rehydrate persisted k8s LB IPs")
        assert anchor != -1, (
            "rehydration block anchor comment missing from pipeline.py; "
            "the block may have been removed or refactored without "
            "updating this regression test."
        )
        # Grab the next ~50 lines after the anchor — covers the loop body.
        block = text[anchor: anchor + 2000]
        # Strip comment lines so the assertion only inspects executable code.
        code_only_lines = [
            line for line in block.splitlines()
            if not line.lstrip().startswith("#")
        ]
        code_only = "\n".join(code_only_lines)
        assert "setdefault" not in code_only, (
            "PR-R's tf_outputs.setdefault(k, v) was the v6 iteration #6 "
            "bug: terraform_output() emits empty-string placeholders for "
            "LB IPs declared in outputs.tf before the LB is harvested, "
            "and setdefault preserved those empties, silently dropping "
            "the persisted real values. Use direct assignment guarded by "
            "`if v:` truthiness instead."
        )

    def test_truthy_guard_is_present(self):
        """The fix uses `if isinstance(...) and v:` to skip empty/None
        persisted values. Pin this specific shape — without the truthy
        guard, a future bug that writes "" into state.yaml.k8s_outputs
        would propagate empty values forward."""
        text = _read(PIPELINE_PY)
        anchor = text.find("rehydrate persisted k8s LB IPs")
        block = text[anchor: anchor + 2000]
        # Match: isinstance(...) and v: (any spacing, with the trailing
        # truthiness check on v).
        assert re.search(
            r"isinstance\(\s*k\s*,\s*str\s*\)\s+and\s+isinstance\(\s*v\s*,\s*str\s*\)\s+and\s+v\b",
            block,
        ), (
            "Truthy guard `and v` on the persisted value is missing. "
            "Empty-string persisted values must not overwrite tf_outputs."
        )


# ============================================================================
# Behavioral: drive run_pipeline through the rehydration code path and
# assert the resulting tf_outputs is what ansible would consume.
# ============================================================================


class TestPipelineRehydrationBehavior:
    """Drive the rehydration loop directly. We don't run the full
    run_pipeline (that requires a full state machine + mocked stage
    runners); we extract and call the rehydration logic via a minimal
    harness. The source-level tests above pin the implementation; these
    pin the outcome."""

    def _rehydrate(self, tf_outputs: dict, k8s_outputs: dict) -> dict:
        """Replicate pipeline.py rehydration loop semantics so the test
        pins the OUTCOME independent of where in pipeline.py the loop
        physically lives. If pipeline.py's loop diverges from this
        reference, the source-level test catches the structural drift
        and the behavioral test catches the semantic regression."""
        # Direct copy of pipeline.py:391-415 fixed semantics.
        result = dict(tf_outputs)
        if isinstance(k8s_outputs, dict):
            for k, v in k8s_outputs.items():
                if isinstance(k, str) and isinstance(v, str) and v:
                    result[k] = v
        return result

    def test_persisted_value_overrides_empty_placeholder(self):
        """The bug case from v6 dogfood iteration #6: terraform output
        emits redis_lb_ip='' and rabbitmq_lb_ip='' while state.yaml has
        the real values. After rehydration, the real values MUST win."""
        tf = {"redis_lb_ip": "", "rabbitmq_lb_ip": "", "other": "stays"}
        k8s = {"redis_lb_ip": "10.0.0.8", "rabbitmq_lb_ip": "10.0.0.15"}
        out = self._rehydrate(tf, k8s)
        assert out["redis_lb_ip"] == "10.0.0.8", (
            f"persisted real redis IP must override empty terraform "
            f"placeholder; got {out['redis_lb_ip']!r}"
        )
        assert out["rabbitmq_lb_ip"] == "10.0.0.15"
        assert out["other"] == "stays", (
            "non-LB keys in tf_outputs must not be affected by rehydration"
        )

    def test_persisted_empty_does_not_overwrite_real_terraform_value(self):
        """Defense-in-depth: if state.yaml somehow holds an empty string
        (e.g. stale post-destroy state) and terraform emits a REAL value,
        the empty must NOT silently overwrite. Truthy guard pins this."""
        tf = {"redis_lb_ip": "192.168.1.1"}  # hypothetical: TF has real value
        k8s = {"redis_lb_ip": ""}  # state.yaml is stale/empty
        out = self._rehydrate(tf, k8s)
        assert out["redis_lb_ip"] == "192.168.1.1", (
            "empty persisted value must not overwrite a real terraform "
            "value; truthy guard `and v` is the safeguard."
        )

    def test_persisted_key_absent_keeps_terraform_value(self):
        """No persisted entry for a key → terraform's value (even if "")
        is unchanged. This was the original setdefault behavior for the
        non-LB case; truthy override preserves it."""
        tf = {"asterisk_call_lb_ip": ""}
        k8s = {}  # not yet harvested
        out = self._rehydrate(tf, k8s)
        assert out == {"asterisk_call_lb_ip": ""}, (
            f"missing persisted key must not synthesize a value; got {out!r}"
        )

    def test_persisted_key_absent_in_tf_outputs_gets_added(self):
        """If terraform_output() doesn't even declare the key (e.g.
        outputs.tf doesn't list it), the persisted value populates it."""
        tf: dict[str, str] = {}
        k8s = {"asterisk_call_lb_ip": "10.0.0.10"}
        out = self._rehydrate(tf, k8s)
        assert out["asterisk_call_lb_ip"] == "10.0.0.10"

    def test_non_string_persisted_value_is_ignored(self):
        """Defensive: a malformed state.yaml with None or list values for
        an LB IP must not crash the loop or propagate garbage."""
        tf = {"redis_lb_ip": ""}
        k8s = {"redis_lb_ip": None, "rabbitmq_lb_ip": ["x"]}
        out = self._rehydrate(tf, k8s)
        # Both malformed entries skipped; tf placeholder stays.
        assert out["redis_lb_ip"] == ""
        assert "rabbitmq_lb_ip" not in out

    def test_non_dict_persisted_state_is_tolerated(self):
        """A YAML state file corrupted to a list/string under k8s_outputs
        must not crash the rehydration loop."""
        # The implementation does `isinstance(persisted_k8s, dict)` first.
        # If we pass a non-dict it should be a no-op.
        out = self._rehydrate({"a": "b"}, [])  # type: ignore[arg-type]
        assert out == {"a": "b"}
        out2 = self._rehydrate({"a": "b"}, "broken")  # type: ignore[arg-type]
        assert out2 == {"a": "b"}


# ============================================================================
# Integration: run_pipeline reaches the ansible stage with rehydrated
# tf_outputs. Mocks downstream stage runner so we can inspect the
# tf_outputs it receives.
# ============================================================================


class TestRunPipelineIntegration:
    def test_ansible_runner_receives_rehydrated_outputs(self, tmp_path, monkeypatch):
        """The smoking-gun test: simulate the v6 iteration #6 state
        (terraform output emits empty redis/rabbitmq, state.yaml has
        real values) and confirm the ansible stage runner is called
        with the REAL values, not the empties."""
        from scripts import pipeline

        # Pretend all earlier stages complete; only ansible_run is to_run.
        fake_state = {
            "deployment_state": "applying",
            "stages": {
                "terraform_init": "complete",
                "reconcile_imports": "complete",
                "terraform_apply": "complete",
                "reconcile_outputs": "complete",
                "k8s_apply": "complete",
                "reconcile_k8s_outputs": "complete",
                "ansible_run": "pending",
            },
            "k8s_outputs": {
                "redis_lb_ip": "10.0.0.8",
                "rabbitmq_lb_ip": "10.0.0.15",
                "asterisk_call_lb_ip": "10.0.0.10",
            },
        }
        fake_tf_outputs = {
            # Empty placeholders (the v6 #6 bug condition).
            "redis_lb_ip": "",
            "rabbitmq_lb_ip": "",
            "kamailio_internal_lb_ip": "10.0.0.2",
        }

        captured = {}

        def fake_ansible_runner(config, tf_outputs, dry_run, auto_approve):
            captured["tf_outputs"] = dict(tf_outputs)
            return True

        # Patch the state loader and terraform_output to return our fakes.
        monkeypatch.setattr(pipeline, "load_state", lambda: fake_state)
        monkeypatch.setattr(pipeline, "save_state", lambda s: None)
        monkeypatch.setattr(pipeline, "terraform_output", lambda c: dict(fake_tf_outputs))
        # Replace the ansible_run stage runner.
        original_runners = pipeline.STAGE_RUNNERS.copy()
        pipeline.STAGE_RUNNERS["ansible_run"] = fake_ansible_runner
        try:
            cfg = MagicMock()
            cfg.get = MagicMock(return_value="test")
            # Drive run_pipeline. dry_run=False, auto_approve=True so the
            # ansible runner is actually called (mocked above).
            pipeline.run_pipeline(
                cfg, only_stage="ansible_run", dry_run=False, auto_approve=True,
            )
        finally:
            pipeline.STAGE_RUNNERS.clear()
            pipeline.STAGE_RUNNERS.update(original_runners)

        assert "tf_outputs" in captured, (
            "fake ansible runner was not called; run_pipeline may have "
            "halted before reaching the ansible stage. Check that the "
            "rehydration code path runs even when only one stage is "
            "selected via --stage."
        )
        rehydrated = captured["tf_outputs"]
        assert rehydrated.get("redis_lb_ip") == "10.0.0.8", (
            f"ansible runner received redis_lb_ip="
            f"{rehydrated.get('redis_lb_ip')!r}, expected 10.0.0.8. "
            "Rehydration failed to override terraform's empty placeholder."
        )
        assert rehydrated.get("rabbitmq_lb_ip") == "10.0.0.15"
        assert rehydrated.get("asterisk_call_lb_ip") == "10.0.0.10", (
            "key absent from terraform_outputs (asterisk_call_lb_ip) "
            "must be populated from persisted state."
        )
        # Keys not in k8s_outputs must remain untouched.
        assert rehydrated.get("kamailio_internal_lb_ip") == "10.0.0.2"
