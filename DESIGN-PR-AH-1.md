# DESIGN-PR-AH-1: Fix test_diagnosis state file isolation

## Problem

Two tests in `tests/test_diagnosis.py` fail when a real `.voipbin-state.yaml`
exists with `deployment_state: deployed` and a recent timestamp:

- `test_billing_disabled_returns_false`
- `test_missing_api_returns_false`

Both tests mock `check_billing_tristate` and `check_required_apis` to simulate
failure conditions, expecting `run_pre_apply_checks()` to return `False`.
However, `run_pre_apply_checks()` (lines 126–136 of `scripts/diagnosis.py`)
contains an early-exit path:

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

When the real state file has `deployment_state: deployed` and a timestamp within
24 hours, `load_state()` returns that real state, the early-exit fires, and the
function returns `True` before reaching the mocked checks — making the tests
non-deterministic and environment-dependent.

## Root Cause

The two failing tests do not patch `scripts.pipeline.load_state` (the correct
patch target per the comment in diagnosis.py line 108). They rely on an implicit
assumption that the state file either does not exist or has stale data.

## Decision

Fix the **tests**, not the production code. The production early-exit logic is
correct behaviour.

Each of the two failing tests must patch `scripts.pipeline.load_state` to return
a state that does NOT trigger the early-exit. The appropriate state for tests
that want to exercise checks 2–4 is one that forces the check path: either
`deployment_state: "failed"` or an absent/stale timestamp.

Use `deployment_state: "failed"` — it is the most direct and least fragile:
any state with `"failed"` always runs checks 2–4 regardless of timestamp.

## Change

**File:** `tests/test_diagnosis.py`

For `test_billing_disabled_returns_false` and `test_missing_api_returns_false`,
add a `patch("scripts.pipeline.load_state", return_value={"deployment_state": "failed"})`
decorator so `load_state()` returns a failed state and checks 2–4 are not
skipped.

Example for `test_billing_disabled_returns_false`:

```python
@patch("scripts.diagnosis.check_required_apis", return_value=[])
@patch("scripts.diagnosis.check_billing_tristate", return_value="disabled")
@patch("scripts.diagnosis.run_cmd")
@patch("scripts.diagnosis.check_application_default_credentials", return_value=(True, "u@e.com"))
@patch("scripts.pipeline.load_state", return_value={"deployment_state": "failed"})
def test_billing_disabled_returns_false(self, mock_state, mock_adc, mock_run, mock_bill, mock_apis):
    mock_run.return_value = MagicMock(returncode=0, stdout="my-project")
    from scripts.diagnosis import run_pre_apply_checks
    assert run_pre_apply_checks(_make_config()) is False
```

Same pattern for `test_missing_api_returns_false`.

Note: `@patch` decorators are applied bottom-up, so `mock_state` is the first
argument after `self`.

## Tests

The fix IS the tests. After the patch:
- Both tests must pass in an environment with a fresh deployed state file.
- Both tests must pass in an environment with no state file.
- All 9 existing `TestRunPreApplyChecks` tests must pass.

## Risk

Minimal. Test-only change. No production code touched.

## Alternatives Rejected

- **Delete `.voipbin-state.yaml` before each test run:** Invasive, breaks
  other tests, not the right contract.
- **Modify `run_pre_apply_checks` to accept injectable `load_state`:** Over-
  engineering for a test isolation issue; the existing patch mechanism is the
  intended approach (documented in the source code comment at line 108).
