# DESIGN: PR-AF — pipeline run_pipeline state clobber fix

**Status:** Draft  
**Branch:** NOJIRA-PR-AF-pipeline-state-clobber-fix  
**Date:** 2026-05-14  
**Author:** Hermes (CPO)

---

## 1. Problem

`voipbin-install apply` succeeds through `cert_provision` but `ansible_run`
fails with:

```
cert_provision has not run or failed — cert_state is absent in state.yaml.
```

`state.yaml` shows `cert_provision: complete` but `cert_state` key is absent.

### Root cause: dual-writer state clobber

`_run_cert_provision` loads its own `state` object internally:

```python
def _run_cert_provision(config, outputs, dry_run, auto_approve):
    state = load_state()               # (A) own copy
    cert_state = dict(state.get("cert_state") or {})
    ...
    state["cert_state"] = cert_state
    save_state(state)                  # (B) saves cert_state ✓
    ...
    save_state(state)                  # (C) saves staging_materialized ✓
    return True
```

Back in `run_pipeline`, after the runner returns `True`:

```python
ok = runner(config, tf_outputs, dry_run, auto_approve)
# ok = True
stages[stage_name] = "complete"
state["stages"] = stages              # state = run_pipeline's OWN copy
save_state(state)                     # (D) CLOBBERS cert_state ← BUG
```

`run_pipeline`'s `state` object was loaded at the top of the function and
never received `cert_state`. `save_state(state)` at (D) overwrites the
`cert_state` that (B)/(C) correctly saved.

### Why only cert_provision is affected

Other stage runners (`_run_k8s_apply`, `_run_ansible_run`, etc.) do NOT call
`save_state` internally — they read/write their own state via dedicated fields
that `run_pipeline` also maintains (e.g. `k8s_outputs` is handled by
`run_pipeline` itself via the `__pr_t2_harvested_lb_ips__` sentinel). Only
`_run_cert_provision` calls `save_state` internally with sub-key mutations.

---

## 2. Fix design

### Option A (chosen): reload state after each runner

After `ok = runner(...)` returns True, reload `state` from disk before setting
`stages[stage_name] = "complete"` and calling `save_state`.

```python
ok = runner(config, tf_outputs, dry_run, auto_approve)

if not ok:
    # ... existing failure handling (unchanged)

# Reload state to pick up any sub-key mutations written by the runner.
# _run_cert_provision (and any future runner) calls save_state internally;
# without this reload the outer save_state would clobber those writes.
state = load_state()
stages = dict(state.get("stages") or {})
stages[stage_name] = "complete"
state["stages"] = stages
```

**Why Option A:** minimal, surgical, defensive. Any future runner that calls
`save_state` internally will also benefit. No function signature changes.

### Option B (rejected): pass `state` dict into every runner

Would require changing all 8 runner signatures and updating `STAGE_RUNNERS`.
Over-engineered for a single-bug fix.

### Option C (rejected): remove `save_state` from `_run_cert_provision`

Would require the runner to somehow communicate `cert_state` back to
`run_pipeline` (return value would need to be enriched). More invasive than
Option A.

---

## 3. Test design

### T1 — unit: cert_state survives run_pipeline stage loop save

Simulate the dual-writer scenario: runner saves `cert_state` to disk, then
`run_pipeline`'s post-runner `save_state` must NOT clobber it.

```python
def test_run_pipeline_does_not_clobber_runner_state_writes():
    """run_pipeline's post-runner save_state must not clobber sub-key
    mutations written by _run_cert_provision (or any runner that calls
    save_state internally). Regression guard for PR-AF."""
```

### T2 — integration: cert_state present in state.yaml after apply

After `_run_cert_provision` succeeds via `run_pipeline`, `load_state()` must
return a `cert_state` dict with `actual_mode` set.

### T3 — synthetic injection: revert the reload, confirm T1 fails

Remove the `state = load_state()` reload line and confirm T1 catches it.

---

## 4. Files changed

| File | Change |
|---|---|
| `scripts/pipeline.py` | Add `state = load_state()` + `stages = dict(state.get("stages") or {})` after successful runner return, before `stages[stage_name] = "complete"` |
| `tests/test_pipeline.py` | Add T1 + T2 |

---

## 5. Risks

- **None identified.** Reloading state after a runner is safe: the runner is
  single-threaded and the file is only written by this process. The reload
  picks up any in-runner `save_state` calls while discarding nothing (the
  outer `stages` mutation is applied on top of the reloaded state).
- **Stage ordering is preserved.** `stages[stage_name] = "complete"` is still
  set correctly after the reload.
- **k8s_outputs harvest unaffected.** The `__pr_t2_harvested_lb_ips__`
  sentinel pattern populates `tf_outputs` (in memory) before `save_state`;
  the reload still sees it via `tf_outputs.pop(...)` which happens after the
  reload.
