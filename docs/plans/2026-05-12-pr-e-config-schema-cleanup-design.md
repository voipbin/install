# PR-E — Config schema cleanup + diagnosis preflight realignment

**Date:** 2026-05-12
**Author:** Hermes (CPO)
**Status:** Design v1
**Repo:** `voipbin/install`
**Branch:** `NOJIRA-PR-E-config-schema-cleanup`
**Parent:** main `263eafc` (PR-A→PR-C all merged)
**Roadmap slot:** PR-E (Phase 3; depends on PR-C only)
**LOC estimate:** ~220 (per roadmap v3 §6)
**Gaps closed:** GAP-14 (`cloudsql_private_ip` operator field), GAP-21 (init.py dry-run "6 secrets" stale), A-9 (init.py dry-run text), A-10 (`warn_if_cloudsql_proxy_deployed` return value ignored).
**Newly discovered**: GAP-31 — `run_pre_apply_checks` calls `check_cloudsql_private_ip` BEFORE any stage runs, so fresh install always fails preflight on sentinel even though PR-C's `reconcile_outputs` would populate the value. Discovered during PR-C smoke dogfood 2026-05-12.

## 1. Context

After PR-C, the flow is:
1. `voipbin-install init` writes config with sentinel `cloudsql_private_ip = "cloudsql-private.invalid"`.
2. `voipbin-install apply` enters `run_pre_apply_checks` (scripts/diagnosis.py:95). The check `check_cloudsql_private_ip(config)` at line 130 rejects sentinel and returns False BEFORE `terraform_init` runs.
3. Therefore `reconcile_outputs` never executes; sentinel never replaced; fresh install impossible.

PR-C's FIELD_MAP entry (`cloudsql_mysql_private_ip` → `cloudsql_private_ip`) is correct but its consumer is gated upstream by a check that became logically obsolete once PR-C's auto-population was introduced.

PR-E removes the operator-supplied field per pchero decision #2 (Terraform output only), repositions the preflight to fire AFTER `reconcile_outputs` (i.e. just before `k8s_apply`), and cleans up the unrelated init.py/diagnosis.py drift items A-9 and A-10.

## 2. Scope

### 2.1 `scripts/diagnosis.py` — preflight reposition

Remove the early `check_cloudsql_private_ip(config)` call at `diagnosis.py:119-133` from `run_pre_apply_checks`. Move the same check into a new k8s_apply-stage guard.

**Where**: `scripts/pipeline.py::_run_k8s_apply` (currently at lines ~127-135). Add a precondition wrapper:

```python
def _run_k8s_apply(config, outputs, dry_run, auto_approve):
    # PR-C auto-populates cloudsql_private_ip via reconcile_outputs.
    # If it's still sentinel here, terraform_apply and/or reconcile_outputs
    # didn't run (operator may have skipped stages or terraform output empty).
    from scripts.preflight import check_cloudsql_private_ip, PreflightError
    try:
        check_cloudsql_private_ip(config)
    except PreflightError as exc:
        print_error(str(exc))
        return False
    if dry_run:
        return k8s_dry_run(config, outputs)
    return k8s_apply(config, outputs)
```

This relocates the sentinel guard from "before any stage" to "right before manifests are rendered" — the precise point where sentinel-in-config would cause damage. Fresh install (terraform_apply + reconcile_outputs both complete) sees real IP and passes.

Also: delete `warn_if_cloudsql_proxy_deployed()` call at `diagnosis.py:137` and the helper function at `preflight.py:42-63` (anomaly A-10: return value ignored, post-PR #5a stale check). Operators with stale cloudsql-proxy Deployment from PR #4-era cluster should manually clean — documented in PR-J operator docs (already scheduled).

### 2.2 `config/schema.py` — remove operator field

Remove `cloudsql_private_ip` from `properties` and `required` lists in `config/schema.py`. `additionalProperties: False` is preserved.

`InstallerConfig._apply_defaults` (if it sets sentinel default): drop the `cloudsql_private_ip = "cloudsql-private.invalid"` default. Reconcile_outputs writes the field when Terraform output arrives; if it doesn't, the key is absent, and the relocated preflight (at k8s_apply time) catches it with the existing error message.

Backward compat: existing `config.yaml` files with the field present remain valid (additionalProperties is False but the value is just an extra key... actually NO, schema validation will reject). Migration:
- On `voipbin-install apply` startup, if `config.cloudsql_private_ip` exists, log a deprecation: "Field deprecated; now sourced from Terraform output. Operator value preserved as override for BYO-network installs."
- Treat operator value as override (skip reconcile_outputs FIELD_MAP overwrite when key already present — already implemented in PR-A's `outputs()` via `if not config.get(mapping.cfg_key)` guard at terraform_reconcile.py).

To keep existing schemas working: change `additionalProperties: False` only at the affected key, OR add `cloudsql_private_ip` to schema with `description: "DEPRECATED — auto-populated by Terraform. Operator override for BYO-network installs only."`. Use the latter — explicit deprecation field is more discoverable than silent removal.

### 2.3 `scripts/commands/init.py` — dry-run text + secret count (A-9, GAP-21)

`init.py:181-184` currently says:
```
4. Generate 6 secrets (jwt_key, cloudsql_password, redis_password,
   rabbitmq_user, rabbitmq_password, api_signing_key)
```

The real flow generates 31+ entries (per dogfood log earlier: `JWT_KEY, SSL_CERT_*, SSL_PRIVKEY_*, DEEPGRAM_API_KEY, ELEVENLABS_API_KEY, ...`). Replace with truthful summary:

```python
f"  4. Generate {_count_secrets()} encrypted secrets (DB passwords, JWT key, API keys, TLS certs)",
```

Where `_count_secrets()` returns the actual count from `secret_schema.py` (sum of `class=secret` entries plus init-generated TLS keys, currently 31 — verify at impl time).

### 2.4 `scripts/preflight.py` — remove `warn_if_cloudsql_proxy_deployed`

Delete `warn_if_cloudsql_proxy_deployed()` function entirely (lines 42-63). It's post-PR #5a stale, return value ignored at the only caller (`diagnosis.py:137`, also being removed in §2.1).

### 2.5 Tests — `tests/test_pr_e_config_cleanup.py` (~8 tests)

1. **`test_diagnosis_no_early_cloudsql_check`** — assert `scripts/diagnosis.py::run_pre_apply_checks` source does NOT call `check_cloudsql_private_ip`.
2. **`test_pipeline_k8s_apply_has_sentinel_guard`** — patch the pipeline runner, run `_run_k8s_apply` with sentinel config, assert it returns False without calling `k8s_apply`.
3. **`test_pipeline_k8s_apply_passes_with_real_ip`** — patch with `cloudsql_private_ip = "10.0.0.5"`, assert `_run_k8s_apply` proceeds to `k8s_apply`.
4. **`test_schema_cloudsql_private_ip_marked_deprecated`** — `config/schema.py` `cloudsql_private_ip` entry has description containing "DEPRECATED" (or similar). Not in `required`.
5. **`test_reconcile_outputs_skips_when_operator_override`** — config has operator-set `cloudsql_private_ip = "10.99.99.99"`, run reconcile_outputs with TF returning a different IP, assert config still has operator value (already enforced by `if not config.get(...)` guard; regression guard test).
6. **`test_warn_if_cloudsql_proxy_deployed_removed`** — `scripts/preflight.py` does NOT define `warn_if_cloudsql_proxy_deployed`.
7. **`test_init_dry_run_text_truthful`** — `scripts/commands/init.py` source has updated string with `_count_secrets()` or equivalent dynamic count; literal "Generate 6 secrets" is absent.
8. **`test_diagnosis_no_warn_call`** — `scripts/diagnosis.py` source does not call `warn_if_cloudsql_proxy_deployed`.

Update `tests/test_diagnosis.py`, `tests/test_preflight.py`, and **`tests/test_pr5a_cloudsql_removal.py::test_run_pre_apply_checks_invokes_cloudsql_preflight`**: remove/relocate any tests asserting `check_cloudsql_private_ip` is called from `run_pre_apply_checks` (those will fail post-PR-E). The PR-5a integration test gets rewritten to assert the call happens inside `_run_k8s_apply` instead.

### 2.6 Sentinel migration in `terraform_reconcile.outputs()`

`terraform_reconcile.FIELD_MAP` overwrite guard is currently `if not config.get(mapping.cfg_key)` (truthy check). The sentinel `"cloudsql-private.invalid"` is truthy → guard SKIPS overwrite → operator who upgraded from an old install carries stale sentinel forever and `_run_k8s_apply` preflight rejects.

Fix: extend the guard to also overwrite when the current value is the sentinel. Add to `scripts/terraform_reconcile.py:563`:

```python
current = config.get(mapping.cfg_key)
if not current or current == CLOUDSQL_PRIVATE_IP_SENTINEL:
    config.set(mapping.cfg_key, value)
    changed = True
```

(Import `CLOUDSQL_PRIVATE_IP_SENTINEL` from `scripts.preflight`. Only applies when the mapped field is `cloudsql_private_ip`; for other fields, sentinel check is no-op.)

Add 9th test: **`test_reconcile_outputs_overwrites_sentinel`** — config has sentinel, TF returns "10.0.0.7", assert config gets overwritten.

## 3. Out of scope

- Postgres-related preflight (PR-D1).
- DSN dummy-password cleanup (PR-D2).
- `verify` rewrite (PR-J).
- Operator-facing migration tooling for existing operators with sentinel in their config.yaml — covered by the "deprecated field still accepted as override" path; no separate migration command needed.

## 4. Migration

PR-E does not break any existing config.yaml. Operators who previously had `cloudsql_private_ip` in config.yaml (real IP or sentinel) keep working:

- Real IP: treated as override; reconcile_outputs honors operator value (PR-A `if not config.get(cfg_key)` guard preserves it).
- Sentinel: removed by next `init` cycle; if operator runs apply without re-init, reconcile_outputs writes real IP at terraform_apply time, sentinel overwritten on next config.save() invocation (verify the save path; `_apply_defaults` no longer sets sentinel so absent key is OK).

For fresh install on `voipbin-install-dev`:
1. `voipbin-install init --reconfigure` regenerates config.yaml WITHOUT `cloudsql_private_ip`.
2. `voipbin-install apply` runs terraform_init → reconcile_imports → terraform_apply → reconcile_outputs (populates cloudsql_private_ip from Terraform) → ansible_run → k8s_apply (sentinel guard sees real IP, passes).

## 5. Risks

- **Existing config.yaml with stale sentinel**: covered by §4 migration. If reconcile_outputs has never run (operator partial state), k8s_apply guard catches it loudly.
- **Schema validation strictness**: keeping `cloudsql_private_ip` as deprecated optional preserves back-compat. If we ever remove it entirely (PR-J?), a new migration step would auto-strip stale keys from config.yaml.
- **Preflight reposition test brittleness**: tests rely on parsing source code for "no call to X". Acceptable — same pattern PR-C used for `ansible_runner.py` dead-output cleanup verification.

## 6. Smoke dogfood (post-merge)

After PR-E merges, re-run `voipbin-install-dev` full smoke dogfood per roadmap §8:

1. `voipbin-install init --reconfigure --skip-api-enable --skip-quota-check` — regenerates config without `cloudsql_private_ip` field.
2. `voipbin-install apply --auto-approve` — 6 stages run end-to-end, ~30-60 min.
3. Verify `.voipbin-state.yaml` shows all 6 stages complete.
4. Verify `config.yaml` has `cloudsql_private_ip` populated by reconcile_outputs (real IPv4).
5. rag-manager CrashLoopBackoff expected (Postgres not yet provisioned — PR-D1).

Expected P0: 0. rag-manager CrashLoop is documented and tracked as PR-D1 territory.

If unexpected P0 surfaces, Gap Addendum Protocol §4 classifies. ≥2 P0 → roadmap §5 abort triggers.

## 7. Checklist

- [x] GAP-31 root-caused (preflight position) and addressed
- [x] Operator field removed; backward-compat via deprecated optional
- [x] init.py dry-run text uses dynamic secret count
- [x] `warn_if_cloudsql_proxy_deployed` removed (A-10)
- [x] Tests cover preflight reposition, schema change, operator override, dead code removal
- [x] Migration story for existing operators (override-respecting)
- [x] Smoke dogfood gate identified (post-merge, expects rag-manager CrashLoop)
- [ ] Design review iter 1
- [ ] Design review iter 2
