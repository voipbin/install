# DESIGN: PR-AE-1 — RabbitMQ Liveness/Readiness Probe Timeout Fix

## Problem

`k8s/infrastructure/rabbitmq/deployment.yaml` does not specify `timeoutSeconds`
for liveness or readiness probes. Kubernetes defaults `timeoutSeconds` to 1s.

On GKE, `rabbitmq-diagnostics ping` and `rabbitmq-diagnostics check_running`
occasionally exceed 1s due to:
- CLI startup overhead inside the container
- GKE node load / container cgroup latency spikes

Result (observed in iter#15): RabbitMQ Pod hit 191 liveness kill cycles over
14 hours. False-positive kills cause:
1. RabbitMQ restart → AMQP connections dropped
2. All bin-manager pods (agent-manager etc.) lose MQ connection → Exit 0 immediately
3. `CrashLoopBackOff` on 6 bin-manager pods (exit 0 = startup guard, not crash)

## Root Cause Confirmation

```
livenessProbe:
  exec:
    command: [rabbitmq-diagnostics, -q, ping]
  initialDelaySeconds: 30
  periodSeconds: 30
  # timeoutSeconds: NOT SET → defaults to 1s  ← root cause
readinessProbe:
  exec:
    command: [rabbitmq-diagnostics, -q, check_running]
  initialDelaySeconds: 20
  periodSeconds: 10
  # timeoutSeconds: NOT SET → defaults to 1s  ← root cause
```

GKE liveness probe events log: 2648 failures over 14h (from `kubectl describe pod`).

## Fix

Add `timeoutSeconds` and explicit `failureThreshold` to both probes.

### Chosen values (rationale)

| Param | liveness | readiness | Rationale |
|-------|----------|-----------|-----------|
| `timeoutSeconds` | 10 | 5 | RabbitMQ CLI p99 latency on GKE ~2–3s; 10s gives 3× headroom. readiness can be tighter (5s) since a missed check only removes from endpoint slice, no kill. |
| `failureThreshold` | 6 | 3 | liveness: 6×30s = 3 min before kill → tolerates transient overload; readiness: 3×10s = 30s before pod removed from service. |
| `successThreshold` | 1 | 1 | default, no change |

### Diff (conceptual)

```yaml
livenessProbe:
  exec:
    command: [rabbitmq-diagnostics, -q, ping]
  initialDelaySeconds: 30
  periodSeconds: 30
+ timeoutSeconds: 10
+ failureThreshold: 6

readinessProbe:
  exec:
    command: [rabbitmq-diagnostics, -q, check_running]
  initialDelaySeconds: 20
  periodSeconds: 10
+ timeoutSeconds: 5
+ failureThreshold: 3
```

## Test Strategy

`tests/test_pr_ae1_rabbitmq_probe_timeout.py`

| Test | What it checks |
|------|----------------|
| `test_liveness_timeout_seconds` | `livenessProbe.timeoutSeconds == 10` (exact) |
| `test_liveness_failure_threshold` | `livenessProbe.failureThreshold == 6` (exact) |
| `test_liveness_period_preserved` | `livenessProbe.periodSeconds == 30` |
| `test_liveness_timeout_lt_period` | `livenessProbe.timeoutSeconds < livenessProbe.periodSeconds` |
| `test_readiness_timeout_seconds` | `readinessProbe.timeoutSeconds == 5` (exact) |
| `test_readiness_failure_threshold` | `readinessProbe.failureThreshold == 3` (exact) |
| `test_readiness_period_preserved` | `readinessProbe.periodSeconds == 10` |
| `test_readiness_timeout_lt_period` | `readinessProbe.timeoutSeconds < readinessProbe.periodSeconds` |
| `test_liveness_initial_delay_preserved` | `initialDelaySeconds == 30` |
| `test_readiness_initial_delay_preserved` | `initialDelaySeconds == 20` |

Mutant harness: inject `timeoutSeconds: 1` / remove `failureThreshold` / swap liveness↔readiness values → must fail tests.

## Rollout

Dogfood environment (`voipbin-install-dev`): `kubectl apply -f` on updated deployment.yaml.
Pod will restart once (Recreate strategy). Acceptable — RabbitMQ is already restarting every few minutes due to the probe bug.
No maintenance window required for dogfood.

## Files Changed

- `k8s/infrastructure/rabbitmq/deployment.yaml` — add `timeoutSeconds` + `failureThreshold` to both probes
- `tests/test_pr_ae1_rabbitmq_probe_timeout.py` — new test file (6 tests + mutant harness)

## Non-Goals

- No change to RabbitMQ config, credentials, or persistent volume
- No change to other pod probes (separate PR if needed)
- No change to `scripts/k8s.py` (PR-AE-2 scope)
