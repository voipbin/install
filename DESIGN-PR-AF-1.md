# DESIGN: PR-AF-1 — RabbitMQ .erlang.cookie Permission Fix

## Problem

RabbitMQ fails to start with:
```
Cookie file /var/lib/rabbitmq/.erlang.cookie must be accessible by owner only
```

RabbitMQ requires `.erlang.cookie` to be mode 0400 (owner-only). When Kubernetes
mounts a PVC with `securityContext.fsGroup` set, it runs a recursive `chown/chmod`
pass over the volume. This adds group read bits to existing files — including
`.erlang.cookie` — breaking the ownership requirement.

Observed in dogfood iter#16: PVC deletion + recreation did not help. The bug
reproduces on every pod restart because the fsGroup chmod runs before the
container starts.

## Root Cause

`deployment.yaml` pod `securityContext`:
```yaml
securityContext:
  runAsNonRoot: true
  runAsUser: 999
  fsGroup: 999          ← triggers recursive chown/chmod on PVC mount
  seccompProfile:
    type: RuntimeDefault
```

Kubernetes default `fsGroupChangePolicy` is `Always` — it recursively changes
ownership and permissions on every mount. This sets group bits on
`/var/lib/rabbitmq/.erlang.cookie`, making it non-0400.

## Fix

Add `fsGroupChangePolicy: OnRootMismatch` to the pod `securityContext`:

```yaml
securityContext:
  runAsNonRoot: true
  runAsUser: 999
  fsGroup: 999
  fsGroupChangePolicy: OnRootMismatch   ← NEW
  seccompProfile:
    type: RuntimeDefault
```

`OnRootMismatch` applies ownership changes only to the **root directory** of the
volume, not recursively to every file. The root directory gets the correct
ownership (UID 999, GID 999). `.erlang.cookie` retains its mode 0400 as set by
RabbitMQ on first run.

Supported since Kubernetes 1.20. GKE uses 1.35+ — fully supported.

## Why Not Other Options

| Option | Problem |
|--------|---------|
| Remove `fsGroup` | PVC root not owned by GID 999 → RabbitMQ cannot write new files |
| initContainer `chmod 400` | Runs after fsGroup chmod → band-aid, not root fix |
| Secret-based cookie | Over-engineered for dogfood; adds k8s secret management scope |

## Test Strategy

`tests/test_pr_af1_rabbitmq_cookie_perms.py`

| Test | What it checks |
|------|----------------|
| `test_fsgroupchangepolicy_set` | `securityContext.fsGroupChangePolicy == "OnRootMismatch"` |
| `test_fsgroup_preserved` | `fsGroup` still present and == 999 |
| `test_runasuser_preserved` | `runAsUser == 999` |
| `test_runasnonroot_preserved` | `runAsNonRoot == True` |

Mutant harness: remove field / set to "Always" → must fail tests.

## Files Changed

- `k8s/infrastructure/rabbitmq/deployment.yaml` — add `fsGroupChangePolicy: OnRootMismatch`
- `tests/test_pr_af1_rabbitmq_cookie_perms.py` — new test file
