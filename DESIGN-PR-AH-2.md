# DESIGN-PR-AH-2: Fix test_diagnosis state file isolation

## Problem

Four tests in `tests/test_diagnosis.py::TestRunPreApplyChecks` are
non-deterministic and environment-dependent due to missing `load_state` mock:

**Currently failing (visibly broken):**
- `test_billing_disabled_returns_false`
- `test_missing_api_returns_false`

**Currently passing vacuously (latent bug):**
- `test_all_pass_returns_true`
- `test_billing_unknown_continues`

All four tests mock `check_billing_tristate` and/or `check_required_apis` to
exercise specific code paths in `run_pre_apply_checks()`. However, the function
contains an early-exit path (lines 126–136 of `scripts/diagnosis.py`):

```python
if only_stage is None:
    state = load_state()
    ts_str = state.get("timestamp")
    deploy_state = state.get("deployment_state", "")
    if ts_str and deploy_state != "failed":
        try:
            ts = datetime.fromisoformat(ts_str)
            if datetime.now(timezone.utc) - ts < timedelta(hours=24):
                return True  # checks 2-4 skipped
```

When a real `.voipbin-state.yaml` exists with `deployment_state: deployed` and a
recent timestamp, `load_state()` reads it, the condition fires, and the function
returns `True` without executing the mocked checks:

- Tests expecting `False` (billing disabled, missing API) → **fail** (get `True`)
- Tests expecting `True` (all pass, billing unknown) → **pass by accident**,
  never exercising the billing/API code paths their names advertise

## Root Cause

Tests that want to exercise Checks 2–4 must suppress the early-exit by patching
`load_state`. `diagnosis.py` line 108 documents the correct patch target:
*"Tests must patch 'scripts.pipeline.load_state', not 'scripts.diagnosis.load_state'."*

The four affected tests do not apply this patch.

## Decision

Fix the **tests**, not the production code. The production early-exit logic is
correct behaviour (avoids redundant GCP calls on recent clean deploys).

All four affected tests must patch `scripts.pipeline.load_state` to return a
state that does NOT trigger the early-exit. The cleanest state for this purpose
is `{"deployment_state": "failed"}` — no `timestamp` key — which bypasses the
early-exit on two independent conditions:
1. `ts_str = None` → `if ts_str` is False → early-exit does not fire
2. `deploy_state == "failed"` → even if a timestamp were added later, the
   `deploy_state != "failed"` guard would still block the early-exit

Using `"failed"` is the more robust choice because it remains correct even if
the mock is later updated to include a timestamp.

## Change

**File:** `tests/test_diagnosis.py`

Add `@patch("scripts.pipeline.load_state", return_value={"deployment_state": "failed"})`
to all four affected tests. This decorator must be the **bottom-most** (last
listed before the `def`) so it injects as the first positional argument after
`self` (Python stacks `@patch` bottom-up).

Affected tests and their updated signatures:

```python
@patch("scripts.pipeline.load_state", return_value={"deployment_state": "failed"})
@patch("scripts.diagnosis.check_required_apis", return_value=[])
@patch("scripts.diagnosis.check_billing_tristate", return_value="disabled")
@patch("scripts.diagnosis.run_cmd")
@patch("scripts.diagnosis.check_application_default_credentials", return_value=(True, "u@e.com"))
def test_billing_disabled_returns_false(self, mock_adc, mock_run, mock_bill, mock_apis, mock_state):
    ...

@patch("scripts.pipeline.load_state", return_value={"deployment_state": "failed"})
@patch("scripts.diagnosis.check_required_apis", return_value=[])
@patch("scripts.diagnosis.check_billing_tristate", return_value="unknown")
@patch("scripts.diagnosis.run_cmd")
@patch("scripts.diagnosis.check_application_default_credentials", return_value=(True, "u@e.com"))
def test_billing_unknown_continues(self, mock_adc, mock_run, mock_bill, mock_apis, mock_state):
    ...

@patch("scripts.pipeline.load_state", return_value={"deployment_state": "failed"})
@patch("scripts.diagnosis.check_required_apis", return_value=[])
@patch("scripts.diagnosis.check_billing_tristate", return_value="enabled")
@patch("scripts.diagnosis.run_cmd")
@patch("scripts.diagnosis.check_application_default_credentials", return_value=(True, "u@e.com"))
def test_all_pass_returns_true(self, mock_adc, mock_run, mock_bill, mock_apis, mock_state):
    ...

@patch("scripts.pipeline.load_state", return_value={"deployment_state": "failed"})
@patch("scripts.diagnosis.check_required_apis", return_value=["sqladmin.googleapis.com"])
@patch("scripts.diagnosis.check_billing_tristate", return_value="enabled")
@patch("scripts.diagnosis.run_cmd")
@patch("scripts.diagnosis.check_application_default_credentials", return_value=(True, "u@e.com"))
def test_missing_api_returns_false(self, mock_adc, mock_run, mock_bill, mock_apis, mock_state):
    ...
```

Note on `test_all_pass_returns_true`: the current test body already has a `mock_apis`
parameter. Adding the `load_state` patch simply appends `mock_state` as the final
positional argument; no other signature adjustment is needed.

## Tests

The fix IS the tests. After the patch:
- All four tests must pass deterministically regardless of whether
  `.voipbin-state.yaml` exists, its age, or its `deployment_state`.
- All 9 existing `TestRunPreApplyChecks` tests must pass.
- No production code changes.

## Risk

Minimal. Test-only change. The fix makes each test exercise the code path its
name advertises, rather than relying on file-system state.

## Alternatives Rejected

- **Delete `.voipbin-state.yaml` in setUp:** Invasive, would break other tests
  that depend on the file being present.
- **Inject `load_state` as a parameter to `run_pre_apply_checks`:** Over-
  engineering; the existing `@patch` mechanism is the intended approach and is
  already documented in source.
- **Only fix the 2 visibly failing tests:** Incomplete — leaves `test_all_pass`
  and `test_billing_unknown` passing vacuously without testing their intended
  code paths.
