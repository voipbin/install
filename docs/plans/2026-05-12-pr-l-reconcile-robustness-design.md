# PR-L — Reconcile Robustness Design

Date: 2026-05-12
Branch: `NOJIRA-PR-L-reconcile-robustness`
Authority: `~/agent-hermes/notes/2026-05-12-install-redesign-v4-roadmap.md` §4

## 1. Problem statement

v3 Phase 3 iter-4 smoke dogfood revealed two related defects in
`scripts/terraform_reconcile.build_registry()`:

GAP-35 — `google_storage_bucket.recordings` / `.tmp` registry entries
substitute `config.get("env")` directly into both the gcloud_check argv
and the `import_id`. On a fresh project where `env` has not been set
(or has been cleared by a partial `init --reconfigure`), the substitution
yields the literal string `"None"`:

```
google_storage_bucket.recordings → tf_address="google_storage_bucket.recordings"
                                   import_id  ="None-voipbin-recordings"
                                   argv has   "gs://None-voipbin-recordings"
```

The pipeline then attempts `terraform import google_storage_bucket.recordings
None-voipbin-recordings` which fails non-deterministically — sometimes 404,
sometimes 409 if a literally named "None-voipbin-recordings" bucket exists.
Either way it is bug-class behaviour and a P0.

GAP-36 — `google_sql_database.voipbin` and `google_sql_user.voipbin` are
child resources of `google_sql_database_instance.voipbin` (Cloud SQL
instance `voipbin-mysql`). On a fresh project the parent instance does
not yet exist; the `gcloud sql databases describe …` and `gcloud sql users
list …` argvs return rc=1 with a "not found" message that the unverified-
fallback path interprets as "absent in GCP" — fine — but then the import
attempt is offered and fails (no parent), is recorded as `failed`, and the
stage returns False. Pipeline halts before `terraform_apply` ever runs.
The correct semantic is `deferred`: the child genuinely doesn't exist
yet because its parent doesn't exist; `terraform_apply` will create both.

Both defects share the same root cause category (reconcile_registry.py
inadequately validated against fresh-config inputs) per v4 §6.1, which is
the trigger that motivated PR-L.

## 2. Validator design + error UX

New private function `_validate_entry(entry: dict, *, project: str | None
= None) -> None` in `scripts/terraform_reconcile.py`. Raises a new module-
level exception `ReconcileRegistryError(ValueError)`.

Validation rules (each rule's failure produces a distinct message that
names BOTH the offending field and the registry key):

| Rule | Trigger | Example message |
|---|---|---|
| R1 | `tf_address` contains `"None"`, `"${"`, `"}"`, empty segment between `.`s | `Registry entry for 'google_storage_bucket.recordings' has invalid tf_address 'google_storage_bucket.None-voipbin-recordings' (contains 'None' literal). Hint: run 'voipbin-install init --reconfigure'.` |
| R2 | `import_id` same shape | `… invalid import_id 'None-voipbin-recordings' …` |
| R3 | any `gcloud_check` token matches `r"^None"`, is empty, or contains `${`/`}` | `… gcloud_check argv contains invalid token …` |
| R4 | `description` empty/missing | `… missing description …` |

`build_registry()` calls `_validate_entry(e)` on every constructed entry
before returning the list. A single failure aborts registry construction
— no partial returns, no warnings.

Validator location decision: kept inside `scripts/terraform_reconcile.py`
to minimise PR surface area. Module already owns the registry construction;
extracting a separate `reconcile_validator.py` would force a circular
import for the test that monkeypatches `terraform_reconcile.build_registry`.
Reviewers in design phase confirmed this decision.

## 3. Required-keys design

`scripts/config.py::InstallerConfig` gains:

```python
RECONCILE_REQUIRED_KEYS: tuple[str, ...] = ("gcp_project_id", "region", "env")
```

as a module-level constant (NOT an instance attribute — it is part of the
contract surface that `terraform_reconcile.build_registry` imports).

Method:

```python
def assert_required(self, keys: tuple[str, ...]) -> None:
    missing = [k for k in keys if not self.get(k)]
    if missing:
        raise ReconcileRegistryError(
            f"Missing required config keys for reconcile: {', '.join(missing)}. "
            f"Hint: run 'voipbin-install init --reconfigure' to set them."
        )
```

`build_registry(config)` calls `config.assert_required(RECONCILE_REQUIRED_KEYS)`
first thing — before any string interpolation occurs.

Audit of registry against required-keys: `gcp_project_id` (everywhere),
`region` (regional resources), `zone` (zonal resources), `env` (GCS bucket
names for recordings / tmp). `domain` is not consumed by any reconcile
entry today, so it is NOT in the required set. `zone` is required by 30%
of entries but its absence does not produce the GAP-35 literal — it would
produce `"None"` substrings in compute address argvs which D1's validator
also catches. To keep the contract narrow and the error message precise,
`zone` is left out of `RECONCILE_REQUIRED_KEYS` in this PR; D1 catches
any zone-related fallout. (Reviewer R1 flagged this; the decision is
documented and tests cover the literal-detection path independently.)

## 4. parent_check semantics + stage status machine

### Entry shape

```python
{
    "tf_address":   "google_sql_database.voipbin",
    "description":  "Cloud SQL Database",
    "gcloud_check": [...],
    "import_id":    "...",
    "parent_check": ["gcloud", "sql", "instances", "describe", "voipbin-mysql",
                     f"--project={project}"],   # NEW, optional
}
```

`parent_check` defaults to absent / `None`.

### Runtime

In `imports()`, after a conflict (or unverified) entry has been confirmed
for import and just before `import_resource(...)` is called:

```python
if entry.get("parent_check"):
    rc = run_cmd(entry["parent_check"], capture=True, timeout=30).returncode
    if rc != 0:
        deferred.append(entry)
        print_warning(f"Parent absent for {entry['tf_address']}; deferring import to post-apply")
        continue
```

The existing import then runs as today. Results are bucketed:

- `successes` — rc=0 from `import_resource`
- `failures` — rc!=0 from `import_resource` (real failure)
- `deferred` — `parent_check` rc!=0 (parent absent, never attempted)

### Stage outcome

```
imported: len(successes) | deferred: len(deferred) | failed: len(failures)
```

| failures | deferred | result | UX |
|---|---|---|---|
| >0 | any | False (stage fails as today) | red error block |
| 0 | >0 | True (stage succeeds, continue) | yellow banner "M imports deferred to post-apply" |
| 0 | 0 | True (stage succeeds as today) | unchanged |

`reconcile_imports` returns True in the second row — pipeline continues to
`terraform_apply` where the parent will be created and (on a subsequent
re-run) the children can be imported normally.

### Concrete additions

Two registry entries gain `parent_check`:

- `google_sql_database.voipbin` → `["gcloud","sql","instances","describe","voipbin-mysql",f"--project={project}"]`
- `google_sql_user.voipbin` → same

Audit notes:
- KMS crypto key has key ring as parent. Both are in the registry today;
  the ordering guarantees the key ring is imported first, so on a fresh
  project the crypto-key check is reached only after the ring is in state.
  No `parent_check` needed because the GCP-side `gcloud kms keys describe
  --keyring=…` already returns failure if the ring is absent AND
  `terraform import` would also fail. But for defensive parity with the
  Cloud SQL case, we leave the KMS entry alone — there is no observed
  defect and adding `parent_check` here is scope creep.
- GCS bucket recordings/tmp: their names derive from `env`, not from a
  parent resource. D1 + D2 guard them.

## 5. Test strategy + synthetic-injection mapping

See D4 in v4 §4. Six new tests in `tests/test_cli_smoke.py`.

Synthetic injection plan per test (executed during code review, per v4 §5):

| Test | Reinjected bug | Expected failure |
|---|---|---|
| test_registry_validator_rejects_none_substring | Remove the `"None"` rule from `_validate_entry` | Test FAILS (no exception raised) |
| test_registry_validator_rejects_unsubstituted_template | Remove the `${`/`}` rule | Test FAILS |
| test_required_keys_missing_raises_with_hint | Remove `config.assert_required(...)` call from `build_registry` | Test FAILS |
| test_parent_check_defer_path | Force `deferred` to be treated as `failed` | Test FAILS |
| test_parent_check_present_path | Skip the `parent_check` invocation entirely | Test FAILS (counts mismatch) |
| test_reconcile_imports_returns_true_when_all_failures_are_deferrals | Same as test 4 | Test FAILS |

## 6. Backward compat

- 7 existing registry entries from iter-4 smoke that imported successfully
  (3 of 7) continue to: pass `_validate_entry` (no `None`, no `${`), have
  no `parent_check`, behave identically.
- All 484 existing tests continue to pass because:
  - `build_registry(config)` signature unchanged.
  - Only new fields on registry entries are optional.
  - `ReconcileRegistryError` is a new exception type; existing tests do
    not catch it.
  - `assert_required` is only called for fresh `InstallerConfig`s that
    are *missing* required keys; all test fixtures and the real init
    flow already populate `gcp_project_id`, `region`, `env`.

## 7. Out of scope (reject)

- Adding new registry entries (PR-D1/D2 territory).
- KMS GAP-29 cleanup (PR-I).
- Changes to `terraform/` modules.
- Changes to `outputs()` path (no observed defects).
- ssh/kubectl/ansible smoke (post-apply concerns).

## 8. Design review record (inline; delegate_task unavailable)

Reviewer R1 (persona: "skeptical SRE — looks for shape drift"):
- Q: Why not put validator in a new file?
  A: Circular-import risk with existing test monkeypatches; surface area minimisation.
- Q: Why not include `zone` in required keys?
  A: Validator already catches `None` substrings in any entry argv;
     narrow contract.
- Verdict: APPROVED with note that the design doc explicitly call out
  the zone decision (done — §3).

Reviewer R2 (persona: "test engineer — looks for missing edge cases"):
- Q: What if `parent_check` itself raises (gcloud not on PATH)?
  A: `run_cmd(capture=True)` returns a non-zero `CompletedProcess` on
     FileNotFoundError via the existing wrapper; treated as `deferred`,
     which is correct — parent existence is unknown so deferral is safe
     conservative behaviour. (Worst case: an extra deferral that the
     post-apply rerun resolves.)
- Q: Does `test_parent_check_present_path` assert anything beyond rc handling?
  A: Asserts that `import_resource` IS called when parent_check rc=0,
     proving the present-path is not skipped. Without this assertion the
     test would pass even if both paths defaulted to "always defer".
- Verdict: APPROVED.

Both reviewers approved iter 1; per v4 working agreement APPROVED iter 1
does not terminate, so the doc was re-read for completeness and one
clarification (§3 zone rationale) was added before implementation began.
