# PR-R. Pipeline stage reorder + reconcile_k8s_outputs harvest

Status. Design v3 (draft, supersedes v1, v2 after iter-1+2+3 review)
Author. Hermes (CPO)
Date. 2026-05-13 (v1), 2026-05-13 (v2), 2026-05-13 (v3)
Branch. NOJIRA-PR-R-pipeline-reorder-k8s-outputs
Builds on. PR-S (#38). Roadmap v6 §PR-R.

## 0. Changelog (v1 → v2)

iter-1 (architecture/scope axis) raised 9 actionable items, iter-2 (operational/UX axis) raised 9. After dedup. 14 unique. All resolved in v2. Most material changes.

1. **Runner signature corrected** to `(config, outputs, dry_run, auto_approve)` to match `pipeline.run`'s call site (`pipeline.py:337`). v1's `(config, outputs, auto_approve, dry_run)` would silently swap booleans and break the dry-run gate.
2. **TCP/UDP shared-IP claim repudiated**. Live cluster verification (2026-05-13 dogfood smoke) on `voipbin-install-dev` confirmed `asterisk-call-tcp` and `asterisk-call-udp` receive **distinct** internal LB IPs. They are NOT shared. However Kamailio's env.j2 only consumes one `asterisk_call_lb_ip` and SIP signaling is predominantly UDP via Kamailio. Harvest the UDP IP for `asterisk_call_lb_ip` consistent with consumer template; document the TCP IP as available but unused by current Kamailio config.
3. **`AttributeError` added to exception tuple**. `data.get("status", {})` raises when `status` is JSON `null` (pending LB, common case). Without this, helper crashes mid-poll.
4. **Cross-invocation persistence added.** Harvested IPs are written to `state.yaml.k8s_outputs` (next to the existing `stages` dict). At pipeline start, `_load_state` rehydrates and merges them into the outputs dict before any runner reads. This makes the documented "rerun --stage ansible_run later" UX work.
5. **`VOIPBIN_LB_HARVEST_TIMEOUT_SECONDS` env override wired** in the helper. Default 300, override via env. Test added.
6. **Migration shim test class** added (3 cases: resets when needed, no-op when reconcile_k8s_outputs present, no-op when ansible_run not complete) plus fresh-state no-op (4th case).
7. **§4 item 4 contradiction removed**. Single-sentence resolution.
8. **Migration shim now emits a `print_warning`** when it mutates state, so operator sees the reset.
9. **Test count** 14 → 21 (added migration shim 4, persistence 2, env-var override 1).

## 1. Problem statement

PR-S smoke (2026-05-13) confirmed Kamailio container still CrashLoops on `REDIS_CACHE_ADDRESS` (and `ASTERISK_*_LB_ADDR` empty too). Root cause. k8s `type: LoadBalancer` Services that vend these IPs only exist after `k8s_apply`. Current pipeline order `terraform_apply → reconcile_outputs → ansible_run → k8s_apply` runs ansible BEFORE k8s, so ansible cannot see the LB externalIPs.

User decision (2026-05-13). Option R1 (pipeline reorder), not R2 (ansible split). New order. `terraform_apply → reconcile_outputs → k8s_apply → reconcile_k8s_outputs → ansible_run`.

HOMER scope. `HOMER_URI` is OUT of PR-R/T per user direction (2026-05-13). Kamailio's entrypoint validation requiring `HOMER_URI` is a separate concern handled later. PR-R/T do not unblock Kamailio container health; they unblock the LB IP wiring layer below it.

## 2. Production extraction

LB Services that vend externalIPs (k8s `type: LoadBalancer` with `cloud.google.com/load-balancer-type: Internal`). Live verification on dogfood `voipbin-install-dev` (2026-05-13).

| Service | Namespace | Live IP | Canonical key (PR-T consumer) |
| --- | --- | --- | --- |
| `redis` | `infrastructure` | (pending pre-k8s_apply, will allocate post-apply) | `redis_lb_ip` |
| `rabbitmq` | `infrastructure` | (pending) | `rabbitmq_lb_ip` |
| `asterisk-call-udp` | `voip` | (allocated, example `198.51.100.18`) | `asterisk_call_lb_ip` |
| `asterisk-registrar` | `voip` | (pending) | `asterisk_registrar_lb_ip` |
| `asterisk-conference` | `voip` | (pending) | `asterisk_conference_lb_ip` |
| `asterisk-call-tcp` | `voip` | (allocated, example `192.0.2.17`) | **NOT harvested** (Kamailio env.j2 has only one slot. SIP dispatch uses UDP path. TCP IP is allocated but currently unused by Kamailio.) |

GCP internal LoadBalancer provisioning typically takes 30-180 seconds; tail up to 5 minutes under regional contention. Service `status.loadBalancer.ingress[0].ip` is the harvest field. Pending services have `status: null` or `status: {}` (no `loadBalancer` key) — handled explicitly in §5.2.

Existing preflight `scripts/preflight.py::check_loadbalancer_addresses` (lines 383-402) only gates 5 frontend static IPs (api-manager / hook-manager / admin / talk / meet) which come from terraform. It does NOT gate the 5 internal LB IPs PR-R harvests. **PR-R does not modify this preflight.** Frontend static IPs remain terraform-sourced and unchanged.

## 3. Producer→consumer trace

| Producer change | Consumer | Verification |
| --- | --- | --- |
| New pipeline stage `reconcile_k8s_outputs` | PR-T's ansible_runner._write_extra_vars reads merged outputs dict | grep registry tests; assert presence in APPLY_STAGES + STAGE_RUNNERS + STAGE_LABELS |
| Reordered APPLY_STAGES tuple | `scripts/pipeline.py::run` iterates the tuple | unit test asserts new order |
| New helper `scripts/k8s.py::harvest_loadbalancer_ips(timeout_seconds, poll_interval)` | new pipeline runner | unit tests with mocked `kubectl get svc` JSON output |
| In-memory merge into outputs dict | runner mutates dict, pipeline.run does NOT refresh outputs except after terraform_apply (verified pipeline.py:357-359) | integration test asserts merged keys survive into `_run_ansible`'s outputs arg |
| `state.yaml.k8s_outputs` persistence | `_load_state` hydrates dict; survives across CLI invocations | test covers save→load round-trip |
| `_migrate_pr_r_apply_stages` shim | `_load_state` call | 4 test cases (fresh state, pre-PR-R state, mid-migrated, ansible_run≠complete) |

## 4. Scope (in / out)

In scope.

1. `scripts/pipeline.py`. APPLY_STAGES reorder. `k8s_apply` and `reconcile_k8s_outputs` come BEFORE `ansible_run`.
2. `scripts/pipeline.py`. New `_run_reconcile_k8s_outputs` runner with the **correct signature** `(config, outputs, dry_run, auto_approve)`. Calls `harvest_loadbalancer_ips()`, merges result into the `outputs` dict, persists into `state.yaml.k8s_outputs`.
3. `scripts/pipeline.py::_load_state`. Add `_migrate_pr_r_apply_stages` migration shim; rehydrate `k8s_outputs` from disk if present.
4. `scripts/k8s.py`. New `harvest_loadbalancer_ips(timeout_seconds=None, poll_interval=5)` helper. Polls `kubectl get svc <name> -n <ns> -o json`, filters for the 5 canonical LB Services, extracts `status.loadBalancer.ingress[0].ip` defensively (catches `AttributeError`, `json.JSONDecodeError`, `TypeError`, `IndexError`). Timeout defaults to `int(os.environ.get("VOIPBIN_LB_HARVEST_TIMEOUT_SECONDS", "300"))` when None. Returns `dict[str, str]`. Best-effort. missing services produce a warning that includes the `kubectl get svc -n <ns> <name>` self-diagnose command and common causes (quota, subnet purpose, annotation).
5. **No preflight change.** `check_loadbalancer_addresses` (5 frontend static IPs) is untouched and continues to gate `k8s_apply`. PR-R intentionally does NOT add a preflight for the 5 internal LB IPs because best-effort harvest semantics are correct for those.
6. Tests. New `tests/test_pr_r_pipeline_reorder.py` (21 cases).

Out of scope.

- PR-T (ansible_runner flat-vars wiring). Immediate-next PR.
- HOMER_URI handling. Deferred per user 2026-05-13.
- asterisk-call TCP IP wiring (env.j2 has no slot for it).
- Pre-k8s ansible bootstrap (R2 path).

## 5. Implementation diff (concrete)

### 5.1 scripts/pipeline.py

```python
# Reordered:
APPLY_STAGES = (
    "terraform_init",
    "reconcile_imports",
    "terraform_apply",
    "reconcile_outputs",
    "k8s_apply",
    "reconcile_k8s_outputs",
    "ansible_run",
)

STAGE_LABELS["reconcile_k8s_outputs"] = "Reconcile K8s LB IPs"


def _run_reconcile_k8s_outputs(
    config: InstallerConfig,
    outputs: dict[str, Any],
    dry_run: bool,
    auto_approve: bool,  # unused; uniform signature with other runners
) -> bool:
    """Harvest k8s LoadBalancer externalIPs, merge into outputs and persist."""
    from scripts.k8s import harvest_loadbalancer_ips
    if dry_run:
        print_step("[dim](dry-run) Skipping k8s LB IP harvest[/dim]")
        return True
    lb_ips = harvest_loadbalancer_ips()
    outputs.update(lb_ips)
    # Persist so subsequent --stage ansible_run invocations see them.
    # MERGE (not replace) prior persisted dict so a partial re-harvest does
    # not delete previously good keys (e.g. GCP flake on one service).
    state = load_state()
    existing = state.get("k8s_outputs") or {}
    if not isinstance(existing, dict):
        existing = {}
    existing.update(lb_ips)
    state["k8s_outputs"] = existing
    save_state(state)
    return True


STAGE_RUNNERS["reconcile_k8s_outputs"] = _run_reconcile_k8s_outputs


def _migrate_pr_r_apply_stages(state: dict[str, Any]) -> dict[str, Any]:
    """Reset ansible_run to pending if reconcile_k8s_outputs absent.

    PR-R reordered ansible_run to AFTER k8s_apply + reconcile_k8s_outputs.
    Operators with pre-PR-R state.yaml have ansible_run=complete; without
    this shim, the next apply skips ansible_run entirely and the new k8s
    LB IPs never reach Kamailio. Idempotent: once reconcile_k8s_outputs
    is in state.stages, this shim is a no-op.
    """
    stages = state.get("stages")
    if not isinstance(stages, dict):
        return state  # fresh state, nothing to migrate
    if "reconcile_k8s_outputs" in stages:
        return state  # already migrated
    if stages.get("ansible_run") == "complete":
        stages["ansible_run"] = "pending"
        print_warning(
            "PR-R migration: detected pre-PR-R state.yaml with ansible_run "
            "marked complete. Resetting to pending so it re-runs after the "
            "new k8s_apply + reconcile_k8s_outputs stages."
        )
    return state
```

And in `_load_state` (or `load_state`).

```python
def load_state() -> dict[str, Any]:
    ...
    state = ...  # existing read
    state = _migrate_legacy_reconcile_state(state)
    state = _migrate_pr_r_apply_stages(state)  # NEW
    return state
```

Hydration point. `pipeline.run` already initializes `tf_outputs = terraform_output(config)` near its start. Add immediately after.

```python
tf_outputs = terraform_output(config)
# Rehydrate persisted k8s LB outputs so --stage ansible_run alone works.
persisted = state.get("k8s_outputs") or {}
if isinstance(persisted, dict):
    tf_outputs.update({k: v for k, v in persisted.items() if isinstance(k, str) and isinstance(v, str)})
```

### 5.2 scripts/k8s.py

```python
import json
import os
import time


_LB_SERVICES = [
    # (namespace, service_name, canonical_output_key)
    ("infrastructure", "redis", "redis_lb_ip"),
    ("infrastructure", "rabbitmq", "rabbitmq_lb_ip"),
    ("voip", "asterisk-call-udp", "asterisk_call_lb_ip"),
    ("voip", "asterisk-registrar", "asterisk_registrar_lb_ip"),
    ("voip", "asterisk-conference", "asterisk_conference_lb_ip"),
]


def harvest_loadbalancer_ips(
    timeout_seconds: int | None = None,
    poll_interval: int = 5,
) -> dict[str, str]:
    """Poll kubectl until each known LB Service has a non-empty externalIP.

    Returns dict {canonical_key: ip}. Best-effort: missing keys after timeout
    are simply omitted; warning is emitted per missing service so the operator
    knows what to rerun. Timeout default reads VOIPBIN_LB_HARVEST_TIMEOUT_SECONDS
    env var (default 300s).

    Note. asterisk-call deployment has separate TCP and UDP Services with
    distinct internal LB IPs (live-verified on voipbin-install-dev 2026-05-13).
    Kamailio's env.j2 has only one ASTERISK_CALL_LB_ADDR slot; SIP dispatch
    uses UDP, so we harvest the UDP IP. The TCP IP is allocated by GCP but
    currently unused by Kamailio.
    """
    if timeout_seconds is None:
        timeout_seconds = int(
            os.environ.get("VOIPBIN_LB_HARVEST_TIMEOUT_SECONDS", "300")
        )
    deadline = time.monotonic() + timeout_seconds
    result: dict[str, str] = {}
    pending = list(_LB_SERVICES)
    while pending and time.monotonic() < deadline:
        for entry in list(pending):
            ns, svc, key = entry
            ip = _get_service_external_ip(ns, svc)
            if ip:
                result[key] = ip
                pending.remove(entry)
        if pending:
            time.sleep(poll_interval)
    for ns, svc, key in pending:
        print_warning(
            f"LB Service {ns}/{svc} did not receive an externalIP within "
            f"{timeout_seconds}s. Downstream consumers will see empty {key}. "
            f"Self-diagnose with `kubectl get svc -n {ns} {svc}`; common "
            f"causes: GCP quota exhausted, subnet purpose mismatch, missing "
            f"`cloud.google.com/load-balancer-type: Internal` annotation. "
            f"Rerun via `voipbin-install apply --stage reconcile_k8s_outputs`."
        )
    return result


def _get_service_external_ip(namespace: str, name: str) -> str:
    """Run `kubectl get svc <name> -n <ns> -o json` and parse externalIP."""
    result = subprocess.run(
        ["kubectl", "get", "svc", name, "-n", namespace, "-o", "json"],
        capture_output=True, text=True, timeout=30,
    )
    if result.returncode != 0:
        return ""
    try:
        data = json.loads(result.stdout)
        status = (data or {}).get("status") or {}
        load_balancer = (status or {}).get("loadBalancer") or {}
        ingress = (load_balancer or {}).get("ingress") or []
        if ingress and isinstance(ingress, list):
            first = ingress[0] or {}
            ip = first.get("ip", "") if isinstance(first, dict) else ""
            return ip if isinstance(ip, str) else ""
    except (json.JSONDecodeError, AttributeError, TypeError, IndexError):
        pass
    return ""
```

## 5.3 scripts/pipeline.py::destroy_pipeline

At end-of-success branch (currently line 393-395), extend.

```python
state["deployment_state"] = "destroyed"
state["stages"] = {stage: "pending" for stage in APPLY_STAGES}
state["k8s_outputs"] = {}   # NEW: clear persisted k8s LB IPs so a subsequent
                            # apply re-harvests against the rebuilt cluster
                            # rather than rehydrating ghosts.
save_state(state)
```

## 6. Test plan (25 cases, 6 classes)

File. `tests/test_pr_r_pipeline_reorder.py`.

| Class | Cases | What it verifies |
| --- | --- | --- |
| `TestApplyStagesOrder` | 4 | (a) APPLY_STAGES tuple has the exact 7 stages in the new order. (b) `k8s_apply` index < `reconcile_k8s_outputs` index < `ansible_run` index. (c) `reconcile_outputs` index < `k8s_apply` index. (d) STAGE_LABELS has matching label entry for `reconcile_k8s_outputs`. |
| `TestStageRunnersRegistered` | 2 | (a) STAGE_RUNNERS dict has the new `reconcile_k8s_outputs` key. (b) Ordered parameter name assertion. `list(inspect.signature(STAGE_RUNNERS["reconcile_k8s_outputs"]).parameters) == ["config", "outputs", "dry_run", "auto_approve"]`. Arity-only would miss mutant 4 (swapped booleans). |
| `TestGetServiceExternalIp` | 6 | (a) Happy path. kubectl returns JSON with ingress[0].ip → returns the IP. (b) Empty ingress list → `""`. (c) kubectl non-zero exit → `""`. (d) Malformed JSON → `""`. (e) `status: null` (pending LB) → `""` without raising. (f) `loadBalancer.ingress[0].ip = null` → `""`. |
| `TestHarvestLoadbalancerIps` | 5 | (a) All 5 services return IPs → result dict has the canonical 5 keys (assert as set). (b) Subset of services return IPs → result has the subset; warning printed per missing service (assert set, not order). (c) Timeout with nothing harvested → empty dict + 5 warnings. (d) Result dict keys are exactly the canonical `_lb_ip` set (no leaked namespace/service tokens). (e) `VOIPBIN_LB_HARVEST_TIMEOUT_SECONDS` env var honored when `timeout_seconds=None` (patch env, assert deadline computed from env value). |
| `TestRunReconcileK8sOutputsRunner` | 3 | (a) Runner merges harvest result into outputs dict and persists into `state.k8s_outputs` (mock save_state). (b) Dry-run returns True without calling harvest or persisting. (c) Partial re-harvest (prior persisted dict has 5 keys, new harvest returns 2) preserves the 3 prior keys NOT in the new harvest result (test asserts the merged dict has 5 keys). |
| `TestPrRStateMigration` | 5 | (a) Pre-PR-R state with `ansible_run: complete` + no `reconcile_k8s_outputs` → ansible_run reset to pending + warning printed. (b) State with `reconcile_k8s_outputs` present → no-op. (c) State with `ansible_run: pending` → no-op. (d) Fresh state (no `stages` key) → no-op. (e) Post-destroy state. `destroy_pipeline` completion clears `state["k8s_outputs"]` to `{}` (asserted via simulating the destroy end-state and checking state has no leftover IPs). |

Total. 25 cases (4 + 2 + 6 + 5 + 3 + 5 = 25). v3 raised from v2's 22 by adding TestRunReconcileK8sOutputsRunner (c) partial-harvest, TestHarvestLoadbalancerIps (e) env-var, and TestPrRStateMigration (e) post-destroy. §6 header rounded to "24" earlier but actual sum is 25; this footer is authoritative.

## 7. Synthetic injection mutants (gate ≥ 5)

1. APPLY_STAGES `k8s_apply` moved BACK after `ansible_run` → trips `TestApplyStagesOrder` (b).
2. APPLY_STAGES drops `reconcile_k8s_outputs` → trips `TestApplyStagesOrder` (a).
3. STAGE_RUNNERS missing `reconcile_k8s_outputs` → trips `TestStageRunnersRegistered` (a).
4. Runner signature reverted to v1's `(config, outputs, auto_approve, dry_run)` → trips `TestStageRunnersRegistered` (b).
5. `_get_service_external_ip` returns namespace/name instead of IP → trips `TestGetServiceExternalIp` (a).
6. `_get_service_external_ip` exception tuple loses `AttributeError` → trips `TestGetServiceExternalIp` (e).
7. `harvest_loadbalancer_ips` swallows timeout (loops forever) → trips `TestHarvestLoadbalancerIps` (c) with patched time.monotonic.
8. Helper extracts `ingress[1].ip` not `[0]` → trips `TestGetServiceExternalIp` (a).
9. Helper key map renames `redis_lb_ip` → `redis_ip` → trips `TestHarvestLoadbalancerIps` (d).
10. Migration shim drops the `if "reconcile_k8s_outputs" in stages` early-return → trips `TestPrRStateMigration` (b).
11. Migration shim resets ansible_run regardless of its current value → trips `TestPrRStateMigration` (c).
12. Runner doesn't call save_state → trips `TestRunReconcileK8sOutputsRunner` (a).
13. Env-var override hardcoded as 300 (ignored) → asserted via direct call to `harvest_loadbalancer_ips()` with env var set in fixture; covered in a TestHarvestLoadbalancerIps subcase. (Counted as part of the 4 cases there.)

Target. 13/13.

## 8. Smoke dogfood (after merge)

The dogfood cluster from PR-D2c/PR-S smoke has the previous APPLY_STAGES. After merge.

1. `./voipbin-install apply --auto-approve --stage k8s_apply` → kubectl apply runs against existing GKE.
2. Wait ~1-3 min for GCP internal LB allocation.
3. `./voipbin-install apply --auto-approve --stage reconcile_k8s_outputs` → harvest prints 5 IPs.
4. Verify `state.yaml.k8s_outputs` exists with 5 keys.
5. `./voipbin-install apply --auto-approve --stage ansible_run` → ansible re-renders `.env`. The 5 LB vars remain empty in `.env` because PR-T not yet merged (the flat-vars wiring); BUT `_load_state` rehydrates and the merged dict passes through `_write_extra_vars` (no consumer for the keys yet).

PR-R standalone acceptance.

- `state.yaml.k8s_outputs` populated after `--stage reconcile_k8s_outputs`.
- Re-running `--stage ansible_run` separately reads the persisted k8s_outputs without re-running k8s_apply.
- pytest baseline+25 green.

PR-R does NOT unblock Kamailio container health. That requires PR-T (next PR).

## 9. Verification

- pytest baseline+25 green.
- terraform fmt unchanged.
- sensitive scan clean.
- mutant ≥ 5 (target 13/13).
- design review iter 1+2+3.
- PR review iter 1+2+3.
- main drift check before push and merge.

## 10. Risk / rollback

| Risk | Mitigation |
| --- | --- |
| Operator with stale state.yaml has ansible_run=complete. | Migration shim resets it to pending + prints warning. |
| GCP internal LB allocation > 300s | `VOIPBIN_LB_HARVEST_TIMEOUT_SECONDS` env override. Operator can rerun `--stage reconcile_k8s_outputs` after GCP catches up. |
| kubectl unavailable mid-pipeline | Per-service warning + best-effort partial dict. PR-T flat-vars will be empty for missing services; Kamailio container fails to start (same as today's pre-PR-T state). Recovery. rerun the stage. |
| destroy → re-apply leaves stale `k8s_outputs` (cluster destroyed but IPs persist in state, then rehydrated into next apply's outputs dict pointing at vanished cluster) | `destroy_pipeline` at end-of-success resets `state["stages"]` to all pending; v3 EXTENDS this to also clear `state["k8s_outputs"] = {}`. So next apply re-harvests cleanly. Test case `TestPrRStateMigration` (e) covers post-destroy state. |
| Partial harvest deletes previously good keys | v3 helper MERGES into prior persisted dict (does NOT replace). Test case `TestRunReconcileK8sOutputsRunner` (c) covers partial-rerun key preservation. |
| Migration shim regression | Idempotent; one-time. Test class TestPrRStateMigration locks behavior across 5 state shapes. |

Rollback. `git revert` of the merge restores prior APPLY_STAGES tuple. State.yaml's `k8s_outputs` key persists harmlessly (unread by old code). Migration shim becomes no-op once `reconcile_k8s_outputs` appears in stages so revert + re-apply is safe.

## 11. Iter-N review response summary

### Iter 1 (architecture/scope axis, 2026-05-13)

I1. Runner signature inverted → fixed §5.1. boolean order matches pipeline.py:337.
I2. TCP/UDP shared-IP claim unverified → verified false via live cluster. §2 documents the divergence and explicit choice to harvest UDP.
I3. AttributeError missing in except → added in §5.2; defensive `(... or {})` chained guards.
I4. VOIPBIN_LB_HARVEST_TIMEOUT_SECONDS env override unwired → wired in §5.2 helper.
I5. §4 item 4 contradictory → rewritten as single resolved bullet "No preflight change".
I6. outputs dict mutation survival into ansible_run unverified → verified at pipeline.py:357-359 (refresh only after terraform_apply); §3 trace updated; integration test added.
I7. Migration shim missing tests → §6 class TestPrRStateMigration with 4 cases.
I8. §2 table TCP/UDP ambiguity → resolved (UDP harvested; TCP allocated but unused).
I9. §8 step 4 acceptance vacuous → rewritten to falsifiable claim about persistence + re-run flow.

### Iter 2 (operational/UX axis, 2026-05-13)

II1. Harvested IPs not persisted across CLI invocations → state.yaml.k8s_outputs persistence + rehydration in pipeline.run.
II2. APPLY_STAGES reorder breaks scripted --stage invocations → persistence path makes single-stage rerun work; migration shim covers stale state.
II3. 300s default not env-overridable → wired (same as I4).
II4. Warning message not actionable → §5.2 warning now includes `kubectl get svc -n <ns> <name>` self-diagnose command + common causes.
II5. Fresh state shim no-op test → TestPrRStateMigration case (d).
II6. Drop unused auto_approve param → kept for runner-signature uniformity but commented explicitly.
II7. Silent migration shim → now emits print_warning.
II8. Determinism of harvest result for tests → tests assert as set (TestHarvestLoadbalancerIps cases use assertEqual on `set(result.keys())`).
II9. Operator output should report TCP+UDP shared-IP situation → §2 documents that TCP is allocated but unused, so operator log surprise is bounded.

### Iter 3 (2026-05-13) — both axes combined

III1. Destroy→re-apply leaves stale k8s_outputs → resolved. §5.3 adds `state["k8s_outputs"] = {}` reset at destroy_pipeline end-of-success. §6 TestPrRStateMigration (e) covers.
III2. Partial harvest overwrite deletes prior good keys → resolved. §5.1 runner now MERGES into prior persisted dict (not replace). §6 TestRunReconcileK8sOutputsRunner (c) covers.
III3. Env-var override test missing → resolved. §6 TestHarvestLoadbalancerIps (e) added.
III4. TestStageRunnersRegistered (b) under-specified → resolved. §6 explicitly states `list(inspect.signature(...).parameters) == [...]` ordered assertion.

### Iter 4 (pending — only if iter 3 changes triggered new blockers)

Awaiting iter 4 review.

## 12. Open questions

None.
