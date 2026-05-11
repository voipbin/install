# Fix verify.py namespaces to match install repo manifests

**Status:** Draft (in review)
**Branch:** `NOJIRA-Fix-verify-namespaces-to-match-production`
**Author:** Hermes (CPO)
**Date:** 2026-05-11

## 1. Problem statement

`scripts/verify.py` and `scripts/commands/verify.py` reference Kubernetes
namespaces that do not exist anywhere in the install repo. The verify command
is supposed to confirm a fresh self-hosted install is healthy, but every
namespace-scoped check fails on a working install because it looks up the
wrong namespace names.

Current state in the install repo:

- `k8s/namespaces.yaml` creates exactly three namespaces: `bin-manager`,
  `infrastructure`, `voip`.
- `scripts/verify.py:253,257` iterates over the tuple
  `("voipbin-backend", "voipbin-voip", "voipbin-frontend")` for
  `check_pods_ready` and `check_services_endpoints`.
- `scripts/commands/verify.py:63-64` defaults the single-check invocations
  (`voipbin-install verify pods_ready`, etc.) to namespace
  `"voipbin-backend"`.

Result: `voipbin-install verify` on a freshly installed cluster reports
six `fail` lines (3 namespaces x 2 check types) with messages of the form
`kubectl error: namespaces "voipbin-backend" not found` (the same applies
to `voipbin-voip` and `voipbin-frontend`). Per-check invocations such as
`voipbin-install verify pods_ready` hit the same failure because their
default namespace is the same phantom string. This makes the verify
command worse than useless: it actively misleads operators into thinking
their install is broken.

Production parity discussion (5-namespace audit, `monitoring`,
`square-manager`, missing CronJobs, etc.) is documented separately and is
**out of scope for this PR**. See §3.

## 2. Goals

1. `voipbin-install verify` against a fresh install (built from this repo's
   manifests) returns `pass` for `pods_ready` and `services_endpoints` in
   every namespace that the install repo actually creates.
2. `voipbin-install verify pods_ready` (and the rest of the per-check
   invocations) default to a namespace that exists in this repo.
3. No new namespaces, manifests, or workloads are introduced. Behavior
   change is limited to which namespace strings the verify code passes to
   `kubectl`/the GKE API.
4. No drift between the namespace strings used by `verify.py` and the
   namespaces declared in `k8s/namespaces.yaml`.

## 3. Non-goals

- Adding `monitoring` and `square-manager` namespaces. Tracked separately;
  requires manifests for `monitoring-tests` CronJob and `square-*`
  Deployments that this repo does not yet ship.
- Adding the missing `infrastructure` workloads (Prometheus, Grafana,
  Alertmanager, Heplify). Tracked separately.
- Adding `number-renew` CronJob to `bin-manager`. Tracked separately.
- Removing `cloudsql-proxy` from `infrastructure` (it's installed by this
  repo but unused in production). Tracked separately.
- Production cleanup of the dead `event-manager` Deployment. Already
  executed out-of-band before this PR was opened.

## 4. Affected files

| File | Why |
|---|---|
| `scripts/verify.py` | The orchestrator iterates the wrong namespace tuple in `run_all_checks`. |
| `scripts/commands/verify.py` | The per-check args_map defaults `check_pods_ready` and `check_services_endpoints` to `"voipbin-backend"`. |

No other files change. No new files. No deletes.

## 5. Exact string replacements

### 5.1 `scripts/verify.py`

Replace the two iteration tuples (currently identical: lines 253 and 257):

```python
# BEFORE
    # Pods in key namespaces
    for ns in ("voipbin-backend", "voipbin-voip", "voipbin-frontend"):
        results.append(check_pods_ready(ns))

    # Service endpoints
    for ns in ("voipbin-backend", "voipbin-voip", "voipbin-frontend"):
        results.append(check_services_endpoints(ns))
```

```python
# AFTER
    # Pods in key namespaces
    for ns in ("bin-manager", "infrastructure", "voip"):
        results.append(check_pods_ready(ns))

    # Service endpoints
    for ns in ("bin-manager", "infrastructure", "voip"):
        results.append(check_services_endpoints(ns))
```

Order rationale: matches the declaration order in `k8s/namespaces.yaml`
(bin-manager, infrastructure, voip). No code logic depends on order, but
matching the manifest gives consistent output for operators.

### 5.2 `scripts/commands/verify.py`

Replace the two entries in `args_map` (lines 63-64):

```python
# BEFORE
            "check_pods_ready": ("voipbin-backend",),
            "check_services_endpoints": ("voipbin-backend",),
```

```python
# AFTER
            "check_pods_ready": ("bin-manager",),
            "check_services_endpoints": ("bin-manager",),
```

Default rationale: when an operator runs `voipbin-install verify pods_ready`
without specifying a namespace, `bin-manager` is the most informative
default because this repo's `k8s/backend/` manifests target it for all
backend microservices (the manifests under `k8s/backend/services/*.yaml`
deploy into `bin-manager`). On a fresh install where the backend has
finished rolling out, a `fail` here is a strong signal that something is
wrong; on a partially-installed cluster the check correctly returns
`warn: no pods found` (per `scripts/verify.py:70`) instead of the current
phantom-namespace `fail`. The two other namespaces, `infrastructure` and
`voip`, can be checked explicitly via the all-checks invocation
`voipbin-install verify`.

## 6. Wire-field / API surface checklist

This change does not touch any external API, OpenAPI spec, gRPC proto, or
SDK call. The only "wire" surface affected is the string passed to
`kubectl get pods --namespace=<ns>` and `kubectl get endpoints --namespace=<ns>`
inside the existing helper functions. Those helpers are unchanged.

Verified namespace strings (must match `k8s/namespaces.yaml` exactly,
including case and hyphenation):

- [x] `bin-manager` — declared at `k8s/namespaces.yaml:4`
- [x] `infrastructure` — declared at `k8s/namespaces.yaml:12`
- [x] `voip` — declared at `k8s/namespaces.yaml:20`

## 7. Copy / decision rationale

- **Why not 5 namespaces?** The other two namespaces production currently
  uses (`monitoring`, `square-manager`) are not created by this repo. Adding
  them to verify without also adding the manifests would make verify fail
  on every fresh install for a different reason. See §3.
- **Why `bin-manager` as the per-check default instead of preserving the old
  default?** The old default (`voipbin-backend`) is a phantom string that
  exists nowhere in the repo or in production. Any value we pick will be a
  behavior change; `bin-manager` is both consistent with the manifests and
  the most informative single namespace to spot-check.
- **Why no help-text update?** `scripts/commands/verify.py:54` lists the
  available *check names* (e.g. `pods_ready`), not namespaces, and that
  list is unaffected.

## 8. Verification plan

Per-edit checklist:

1. `grep -nE "voipbin-(backend|voip|frontend)" scripts/ -r` returns zero
   matches after the edit.
2. `grep -nE '"(bin-manager|infrastructure|voip)"' scripts/verify.py` and
   `scripts/commands/verify.py` shows exactly the expected new occurrences
   (4 in verify.py, 2 in commands/verify.py).
3. `python3 -c "import ast; ast.parse(open('scripts/verify.py').read()); ast.parse(open('scripts/commands/verify.py').read())"` succeeds.
4. Dry-run the orchestrator with stub config to confirm no exceptions.
   `scripts/__init__.py` exists, so the import below resolves when invoked
   from the worktree root:

   ```bash
   cd /home/pchero/gitvoipbin/install/.worktrees/NOJIRA-Fix-verify-namespaces-to-match-production
   python3 -c "
   from scripts.verify import run_all_checks
   results = run_all_checks({'gcp_project_id': '', 'zone': '', 'domain': ''})
   names = [r['name'] for r in results]
   print(len(results), 'checks generated')
   print(names)
   "
   ```

   Expected: result names include `Namespace bin-manager pods`,
   `Namespace infrastructure pods`, `Namespace voip pods`, and the
   matching `Namespace <ns> endpoints` entries (six total namespace-scoped
   names). Format string source: `scripts/verify.py:82,113`. The output
   must NOT mention any of `voipbin-backend`, `voipbin-voip`,
   `voipbin-frontend`. The check status values themselves will be `fail`
   because no real cluster is available in the verification env; we are
   only verifying the names emitted.

5. Manual smoke: on a cluster context that has the three namespaces
   created (with or without workloads), `voipbin-install verify pods_ready`
   should produce `pass`, `warn` (`no pods found`, per
   `scripts/verify.py:70`), or a legitimate `fail` for `bin-manager`. It
   must never produce `fail: kubectl error: namespaces "voipbin-backend"
   not found`. (Will be done by the user post-merge; not blocking for
   this PR since CI has no cluster.)

## 9. Rollout / risk

- **Behavior change for existing users:** If anyone is already running
  `voipbin-install verify` on a real install, all six namespace-scoped
  checks currently fail and now they will start passing. This is the
  desired outcome. There are no users who depended on the failing
  behavior.
- **Per-check default change:** `voipbin-install verify pods_ready` (no
  args) previously checked `voipbin-backend`. Now it checks `bin-manager`.
  The old default never returned a useful result, so this is a strict
  improvement, but it is technically a CLI default change. Worth noting
  in the PR body.
- **Risk of false pass:** Low. The check still asks the real `kubectl`
  API, which will return empty if the namespace genuinely has no pods.
  `check_pods_ready` reports `warn` on zero pods.
- **Reversibility:** Trivial. Single string replacement on two files.

## 10. Open questions

None blocking. The 5-namespace production-parity work is acknowledged and
explicitly deferred (§3) to keep this PR small and reviewable.

## 11. Approval status

- [ ] Design approved by independent reviewer
- [ ] Implementation matches approved design
- [ ] PR approved by independent reviewer
- [ ] Merged by pchero (CEO/CTO)
