# PR-A — Pipeline reconcile split + apply-first ordering

**Date:** 2026-05-12
**Author:** Hermes (CPO)
**Status:** Design v2 (incorporates iter 1+2 review)
**Repo:** `voipbin/install`
**Branch:** `NOJIRA-PR-A-pipeline-reconcile-split`
**Parent:** main `3e9e746` (PR #5a merged)
**Roadmap slot:** PR-A (Phase 1, critical-path, no infra change)
**LOC estimate:** ~700 (revised up from 650 to absorb cli.py + diagnosis.py + README + extended tests)

## 1. Context

Roadmap v3 decision #5: split `terraform_reconcile` into two stages.
- **`reconcile_imports`** runs BEFORE `terraform_apply`. Detects GCP resources not in Terraform state, imports them. Prevents 409 conflicts on resume.
- **`reconcile_outputs`** runs AFTER `terraform_apply`. Reads `terraform output`, auto-populates selected `config.yaml` slots (no fields in PR-A; PRs C/D/G append).

In PR-A the implementation of `reconcile_outputs` is a no-op skeleton: function exists, wired into `STAGE_RUNNERS`, runs after apply, with `FIELD_MAP = []`. PRs C/D/G add field mappings.

## 2. Scope

### 2.1 `scripts/pipeline.py`
- New `APPLY_STAGES`:
  ```python
  APPLY_STAGES = (
      "terraform_init",
      "reconcile_imports",
      "terraform_apply",
      "reconcile_outputs",
      "ansible_run",
      "k8s_apply",
  )
  ```
- Add `_run_reconcile_imports`, `_run_reconcile_outputs` runners. Both share the existing 4-arg signature `(config, tf_outputs, dry_run, auto_approve)`.
- `_run_reconcile_outputs` early-returns `True` on `dry_run` (mirrors existing pattern).
- **Precondition check stays in `run_pipeline()`, NOT in the runner** (resolves iter v2-1 blocker — runner signature has no access to `stages` dict or `only_stage`). Added right after `to_run` is computed (around line 191):
  ```python
  if (
      only_stage == "reconcile_outputs"
      and stages.get("terraform_apply") != "complete"
  ):
      print_error(
          "reconcile_outputs requires terraform_apply to be complete first."
      )
      return False
  ```
- Delete `_run_terraform_reconcile`.
- **Replace pipeline.py:29 import**: `from scripts.terraform_reconcile import reconcile as _terraform_reconcile` → `from scripts.terraform_reconcile import imports as _terraform_imports, outputs as _terraform_outputs` (or symbol names matching runner usage).
- Update `STAGE_RUNNERS`:
  ```python
  "reconcile_imports": _run_reconcile_imports,
  "reconcile_outputs": _run_reconcile_outputs,
  ```
- Update `STAGE_LABELS`:
  ```python
  "reconcile_imports": "Terraform Reconcile (Imports)",
  "reconcile_outputs": "Terraform Reconcile (Outputs)",
  ```
- `tf_outputs` flow unchanged — existing collection at `pipeline.py:204-205` and `:238-239` already supplies outputs to `reconcile_outputs`.

### 2.2 `scripts/terraform_reconcile.py`
- Rename top-level `reconcile()` to `imports()`. Keep `reconcile = imports` alias (bind-time only — do NOT monkeypatch `imports` and expect `reconcile` to follow) for backward compat with `tests/test_terraform_reconcile.py`.
- Add `outputs(config, tf_outputs)`:
  ```python
  from dataclasses import dataclass, field
  from typing import Any, Callable

  @dataclass(frozen=True)
  class TfOutputFieldMapping:
      tf_key: str
      cfg_key: str
      validator: Callable[[Any], bool] = lambda v: True

  # PRs C/D/G append entries; PR-A ships empty.
  FIELD_MAP: list[TfOutputFieldMapping] = []

  def outputs(config: InstallerConfig, tf_outputs: dict[str, Any]) -> bool:
      if not FIELD_MAP:
          print_step("[dim]No outputs to populate (no fields registered yet).[/dim]")
          return True
      changed = False
      for mapping in FIELD_MAP:
          value = tf_outputs.get(mapping.tf_key)
          if value is None or value == "":
              continue
          if not mapping.validator(value):
              print_warning(f"Invalid output for {mapping.tf_key}: {value!r}; skipping.")
              continue
          if not config.get(mapping.cfg_key):
              config.set(mapping.cfg_key, value)
              changed = True
      if changed:
          config.save()
          print_success("Updated config.yaml from Terraform outputs.")
      else:
          print_step("[dim]All output-derived config fields already set; no changes.[/dim]")
      return True
  ```
- Forward-compat dataclass shape (per iter-2 NIT-3): avoids PR-C refactor diff.

### 2.3 `scripts/cli.py`
- Update `click.Choice` for `--stage` flag (cli.py:48-58): add `reconcile_imports`, `reconcile_outputs`. Keep `terraform_reconcile` in the choice list (deprecation shim — pipeline.py expands to both new stages).

### 2.4 `scripts/pipeline.py` (--stage shim)
- In `run_pipeline()` legacy-name handling:
  ```python
  if only_stage == "terraform_reconcile":
      print_warning(DEPRECATION_MESSAGE_RECONCILE)
      requested = ["reconcile_imports", "reconcile_outputs"]
  elif only_stage:
      if only_stage not in STAGE_RUNNERS:
          print_error(f"Unknown stage: {only_stage}")
          return False
      requested = [only_stage]
  else:
      requested = list(APPLY_STAGES)
  ```
- `DEPRECATION_MESSAGE_RECONCILE` (exact text):
  ```
  ⚠  --stage terraform_reconcile is deprecated.
     The reconcile stage was split into two:
       • reconcile_imports  (BEFORE terraform_apply — imports drifted GCP resources)
       • reconcile_outputs  (AFTER  terraform_apply — auto-populates config.yaml)
     Running both for backward compatibility. This shim is scheduled for
     removal in install-redesign PR-J. Update scripts to use the new names.
  ```

### 2.5 `scripts/diagnosis.py`
- `diagnose_stage_failure()` stage-name tuples at `:216` (`("terraform_init", "terraform_reconcile", "terraform_apply")`) and `:232` (`("terraform_reconcile", "terraform_apply")`): **replace** `"terraform_reconcile"` with `"reconcile_imports"` and `"reconcile_outputs"`. Since the CLI shim expands the legacy name before pipeline runs, diagnosis never sees `"terraform_reconcile"` at runtime.

### 2.6 State-file migration (`load_state()`)

Migration table (legacy → new):

| Legacy `terraform_reconcile` value | After migration |
|---|---|
| `complete` | `reconcile_imports: complete`, `reconcile_outputs: pending` |
| `failed`   | `reconcile_imports: failed`,   `reconcile_outputs: pending` |
| `running`  | `reconcile_imports: failed`,   `reconcile_outputs: pending` (interrupted treated as failed; idempotent re-run) |
| `pending`  | `reconcile_imports: pending`,  `reconcile_outputs: pending` |
| `<missing>`| no-op (fresh install or already migrated) |
| `<other-unknown-key>` | preserved as-is; pipeline ignores non-APPLY_STAGES keys |

**Post-migration cleanup**: after expansion, the legacy `terraform_reconcile` key is **deleted** from `state["stages"]`. If both legacy and new keys are present (e.g. operator hand-edited), new keys take precedence and legacy is dropped.

Idempotency: second `load_state()` call after migration sees no `terraform_reconcile` key → no-op.

### 2.7 Tests (`tests/test_pipeline_reconcile_split.py` + updates)

New file `tests/test_pipeline_reconcile_split.py` (≥12 tests):

1. **`test_apply_stages_order`** — order assertions.
2. **`test_stage_runners_keys`** — has both new, lacks old.
3. **`test_reconcile_outputs_noop_empty_field_map`** — returns True, logs "no fields registered".
4. **`test_reconcile_outputs_skips_when_config_already_set`** — does not overwrite operator-set values.
5. **`test_reconcile_outputs_validates_value`** — invalid value via validator → skipped + warning.
6. **`test_reconcile_imports_alias`** — `reconcile()` still callable, dispatches to `imports()`.
7. **`test_state_migration_complete`** — legacy `complete` → mapped per table.
8. **`test_state_migration_failed`** — legacy `failed` → mapped.
9. **`test_state_migration_running`** — legacy `running` → mapped.
10. **`test_state_migration_pending`** — legacy `pending` → mapped.
11. **`test_state_migration_idempotent`** — second call no-op.
12. **`test_state_migration_preserves_unknown_keys`** — hand-edited custom stages survive.
13. **`test_only_stage_legacy_shim_runtime`** — `run_pipeline(only_stage="terraform_reconcile")` runs both stages + deprecation log.
14. **`test_only_stage_legacy_shim_cli`** — `CliRunner().invoke(apply, ["--stage", "terraform_reconcile"])` exits 0.
15. **`test_reconcile_outputs_precondition_apply_incomplete`** — standalone `--stage reconcile_outputs` with apply incomplete → returns False with clear error.
16. **`test_diagnosis_recognizes_split_stages`** — `diagnose_stage_failure()` returns hints for both new stage names.
17. **`test_pipeline_flow_skips_completed`** — resume behavior.
18. **`test_tf_outputs_passed_to_reconcile_outputs`** — runner receives the tf_outputs dict.

Updates to existing:
- `tests/test_pipeline.py` — rename mocks of `terraform_reconcile.reconcile` → `terraform_reconcile.imports`; update hardcoded stage tuples (line ~103).
- `tests/test_pipeline_reconcile.py` — **delete and replace** (imports soon-deleted `_run_terraform_reconcile`).
- `tests/test_terraform_reconcile.py:11` — passes via alias, no change.

Mock strategy: pure mocks via `monkeypatch`, matching existing pattern in `test_terraform_reconcile.py`. No real subprocess.

### 2.8 Documentation
- `README.md:292` — the manual-staging cookbook currently reads `init → reconcile → apply → ansible_run`. Post-PR-A correct sequence has 5 steps: `init → reconcile_imports → apply → reconcile_outputs → ansible_run`. Update the example accordingly, or replace with pipeline-flow guidance and a pointer to the new "Pipeline stages" subsection.
- Add a "Pipeline stages" subsection to README near the Architecture section. Skeleton:
  ```
  ## Pipeline stages

  `voipbin-install apply` runs six stages in order:

  1. **terraform_init** — initialize Terraform backend + providers.
  2. **reconcile_imports** — detect GCP resources outside Terraform state and import them. Prevents 409 conflicts on resume.
  3. **terraform_apply** — provision/update GCP infrastructure.
  4. **reconcile_outputs** — read Terraform outputs, auto-populate select config.yaml fields (e.g. private IPs).
  5. **ansible_run** — configure VoIP VMs.
  6. **k8s_apply** — deploy Kubernetes workloads.

  Run individual stages via `voipbin-install apply --stage <name>`.
  ```
- `docs/plans/2026-05-08-terraform-reconcile.md` — add a 2-line top banner: `> **Superseded by PR-A (2026-05-12)** — the single `terraform_reconcile` stage is now split into `reconcile_imports` (before apply) + `reconcile_outputs` (after apply).` Historical; aids future archaeology.
- `docs/operations/`: skim. No specific known references; tasked to implementation.

## 3. Out of scope

- Field auto-population logic (PRs C/D/G).
- `init.py` dry-run text fix (A-9, PR-E).
- `warn_if_cloudsql_proxy_deployed` removal (A-10, PR-E).
- Any infra/Terraform changes.

## 4. Risks

- **State-file edge cases**: `running` and unknown-key cases covered in §2.6 table + tests.
- **CLI Choice list**: addressed in §2.3 — `terraform_reconcile` stays in Choice as deprecation alias.
- **`reconcile_outputs` write failure**: `config.save()` IOError halts pipeline AFTER infra exists. Operator re-runs apply; state shows `reconcile_outputs: failed`. Documented in deprecation/error message.
- **`terraform_output()` returning partial dict**: `if value is None or value == ""` guard handles missing keys; validator catches malformed values (PRs C/D/G concern; ground laid here).
- **Test count grew**: ≥18 new + ~13 updates. LOC estimate revised to ~700.

## 5. Test plan summary

≥18 new tests + ~13 existing test updates. Existing 370 → target ≥376 passing (370 − 4 deleted + 18 new − some merged into existing files ≈ 376-384).

## 6. Smoke dogfood (post-merge)

Per roadmap v3 §7 on `voipbin-install-dev`:
- billing ✓, APIs enabled ✓ (compute, container, sqladmin, servicenetworking, dns, cloudkms, storage, secretmanager, iam, cloudresourcemanager).
- `voipbin-install init` → `voipbin-install apply` end-to-end.
- Verify new `.voipbin-state.yaml` shows 6 stages, `reconcile_outputs: complete` (no-op success).
- No destroy. ~30 min.

## 7. Checklist

- [x] All iter 1 blockers absorbed (B1 cli.py, B2 diagnosis.py, B3 README, B4 running state, B5 test file delete-and-replace, B6 STAGE_LABELS, B7 dual-site shim)
- [x] All iter 2 blockers absorbed (CR-1 click.Choice, CR-2 migration edge cases)
- [x] Nits absorbed (deprecation text, no-op log, dataclass forward-compat, precondition check, test count floor, README mandate)
- [x] Mock-only test strategy documented
- [x] dogfood-readiness state confirmed
- [ ] Design review iter 1 (v2)
- [ ] Design review iter 2 (v2)
