# PR #5a — cloudsql-proxy removal, manifest layer only

**Date:** 2026-05-12
**Author:** Hermes (CPO)
**Status:** Design (iter 2)
**Repo:** `voipbin/install`
**Branch:** `NOJIRA-PR5a-cloudsql-proxy-removal-manifest-only`
**Parent:** PR #4 (`#14`, squash `d0ed631`) merged on main.
**Roadmap slot:** PR #5a (split from PR #5 after iter 1+2 review
surfaced Terraform private-IP provisioning as a separate concern).
Companion PR `#5b` (Terraform private IP) deferred.

## 1. Context

Production cluster confirmed 2026-05-12:
- `Deployment/cloudsql-proxy` does NOT exist in any namespace.
- `Service/cloudsql-proxy` does NOT exist.
- Backend `Secret/voipbin.DATABASE_DSN_BIN` (and `_ASTERISK`) target
  Cloud SQL **private IP** directly via VPC peering. (Specific IP value
  redacted from this doc; an operator-supplied value flows in via
  `config.yaml` / Terraform output.)

The `k8s/infrastructure/cloudsql-proxy/` directory in install repo is
a phantom component that does not match production. Removing it is the
last cleanup step before PR #4d (verify rewrite + README).

## 2. Scope (5a only)

In scope:
- Delete `k8s/infrastructure/cloudsql-proxy/` directory (3 files).
- Remove reference from `k8s/infrastructure/kustomization.yaml`.
- Update NetworkPolicy files (drop cloudsql-proxy ingress/egress; add
  Cloud SQL private-IP CIDR egress for bin-manager, voip, **and any
  other namespace whose Pods consume any `DATABASE_DSN_*` Secret key**).
- Update literal DSN defaults in:
  - `scripts/secret_schema.py` (the canonical source)
  - `k8s/backend/secret.yaml` (rendered Secret stringData literal)
  - `k8s/voip/secret.yaml` (`DATABASE_ASTERISK_HOST`)
- Add `PLACEHOLDER_CLOUDSQL_PRIVATE_IP` + `PLACEHOLDER_CLOUDSQL_PRIVATE_IP_CIDR`
  to `scripts/k8s.py` substitution map.
- Source: **operator-supplied via `config.yaml`** (`cloudsql_private_ip`,
  required). No Terraform output dependency (deferred to PR #5b).
- Add `config.yaml` schema entries with sentinel default
  `cloudsql-private.invalid` (RFC 2606) to keep test fixtures clean
  and produce a clear failure when operator forgets to set it.
- Add `scripts/preflight.py` check: refuse to render manifests if
  `cloudsql_private_ip` equals the sentinel or is empty.
- Update `scripts/terraform_reconcile.py`: drop `cloudsql_proxy_sa_name`
  reference (the SA still exists in Terraform module; PR #5b will
  delete it). No Terraform changes in 5a.
- Add `tests/test_pr5a_cloudsql_removal.py` with 5 assertions
  (enumerated in §6).
- Update `tests/test_k8s.py` and `tests/test_terraform_reconcile.py`.

Out of scope (deferred to **PR #5b**):
- Terraform private-IP provisioning (`google_service_networking_connection`,
  `private_network` on `cloudsql.tf`).
- New Terraform output `cloudsql_private_ip`.
- Removal of dead Terraform SA `google_service_account.sa_cloudsql_proxy`
  and its IAM binding.
- Wiring Terraform output back into `scripts/k8s.py` substitution map.

Out of scope (deferred to **PR #4d**):
- `scripts/verify.py` cleanup.
- README / architecture updates.

## 3. DSN default values (post-5a)

| Secret key | New default value (rendered as literal in Secret manifest) |
|---|---|
| `DATABASE_DSN_BIN` | `bin-manager:dummy-password@tcp(PLACEHOLDER_CLOUDSQL_PRIVATE_IP:3306)/bin_manager` |
| `DATABASE_DSN_ASTERISK` | `asterisk:dummy-password@tcp(PLACEHOLDER_CLOUDSQL_PRIVATE_IP:3306)/asterisk` |
| `DATABASE_DSN_POSTGRES` | `postgres://bin-manager:dummy-password@PLACEHOLDER_CLOUDSQL_PRIVATE_IP:5432/bin_manager?sslmode=disable` |
| `DATABASE_ASTERISK_HOST` (voip ns) | `PLACEHOLDER_CLOUDSQL_PRIVATE_IP` |
| `DATABASE_ASTERISK_PORT` (voip ns) | `3306` |

The placeholder resolves at render time from
`config.cloudsql_private_ip` (operator-supplied). Operators replace
`dummy-password` via sops `secrets.yaml`.

## 4. Substitution map additions

```python
# scripts/k8s.py _build_substitution_map():
cloudsql_private_ip = config.get("cloudsql_private_ip", "")

# Sentinel detection. If operator left the example value, fail at
# preflight (separate check), not here. Empty string also flows
# through and causes a noisy failure on the operator side.
return {
    ...
    "PLACEHOLDER_CLOUDSQL_PRIVATE_IP": cloudsql_private_ip,
    "PLACEHOLDER_CLOUDSQL_PRIVATE_IP_CIDR": (
        config.get("cloudsql_private_ip_cidr")
        or (f"{cloudsql_private_ip}/32" if cloudsql_private_ip else "")
    ),
    ...
}
```

CIDR override: operators with HA Cloud SQL (regional failover) may want
the broader peering range (e.g. `/24`); config field
`cloudsql_private_ip_cidr` optional override.

## 5. NetworkPolicy changes

Audit every NetworkPolicy file for cloudsql-proxy references and DSN
consumers:

Verified Pods that consume any `DATABASE_DSN_*` Secret key (from PR #4
appendix + new postgres consumer):
- **bin-manager ns**: all 31 bin-* Deployments (MySQL DSN), via
  `Secret/voipbin.DATABASE_DSN_BIN`.
- **bin-manager ns**: `rag-manager` additionally consumes
  `DATABASE_DSN_POSTGRES`. NetworkPolicy must allow port 5432 egress
  to Cloud SQL CIDR.
- **voip ns**: `asterisk-registrar` consumes `DATABASE_ASTERISK_HOST`
  + port 3306 via `DATABASE_ASTERISK_PORT`.

### 5.1 `k8s/network-policies/infrastructure-policies.yaml`
- Remove all cloudsql-proxy related rules (ingress to port 3306 from
  bin-manager / voip).
- Keep ClickHouse, RabbitMQ, Redis rules intact.

### 5.2 `k8s/network-policies/bin-manager-policies.yaml`
- Remove rule allowing bin-manager → cloudsql-proxy egress.
- Add rule:
  ```yaml
  - to:
      - ipBlock:
          cidr: PLACEHOLDER_CLOUDSQL_PRIVATE_IP_CIDR
    ports:
      - protocol: TCP
        port: 3306
      - protocol: TCP
        port: 5432  # rag-manager → Cloud SQL Postgres
  ```

### 5.3 `k8s/network-policies/voip-policies.yaml`
- Remove rule allowing voip → cloudsql-proxy egress.
- Add Cloud SQL CIDR egress for port 3306 only (asterisk-registrar).

### 5.4 Audit query
Implementation will grep all `k8s/network-policies/*.yaml` for
`cloudsql-proxy` and resolve every reference, not relying on the
enumeration above.

## 6. Tests

### 6.1 New tests (`tests/test_pr5a_cloudsql_removal.py`)

1. **`test_no_cloudsql_proxy_resources`** — render manifests, assert
   no Deployment, Service, or ServiceAccount named `cloudsql-proxy`
   in any namespace.
2. **`test_no_cloudsql_proxy_string_in_rendered_manifests`** — parse
   rendered YAML, walk every string value (deep), assert substring
   `cloudsql-proxy` does not appear anywhere. Catches stale references
   in DSN strings, NetworkPolicy podSelectors, comments rendered into
   values, etc.
3. **`test_dsn_secret_uses_private_ip_placeholder`** — render with a
   non-sentinel `cloudsql_private_ip` value; assert the rendered
   `Secret/voipbin.DATABASE_DSN_BIN` contains the substituted IP and
   does NOT contain literal `PLACEHOLDER_` substring.
4. **`test_network_policy_allows_cloudsql_cidr_egress`** — render,
   for bin-manager and voip NetworkPolicies, assert exactly one
   egress rule with `ipBlock.cidr` set to the substituted CIDR and
   port 3306 (and 5432 for bin-manager only).
5. **`test_preflight_rejects_sentinel_cloudsql_ip`** — call preflight
   with `cloudsql_private_ip` = `cloudsql-private.invalid`, assert
   it raises `PreflightError` with a message naming the field.

### 6.2 Existing test updates
- `tests/test_k8s.py`: remove or update any assertion referencing
  `cloudsql-proxy.infrastructure.svc.cluster.local`. Add assertion
  that substitution map contains `PLACEHOLDER_CLOUDSQL_PRIVATE_IP`.
- `tests/test_terraform_reconcile.py`: drop `cloudsql_proxy_sa_name`
  expectation (TF SA cleanup deferred to PR #5b; reconcile should
  ignore the SA for now).
- `tests/test_pr4_manifest_invariants.py::test_all_placeholder_tokens_resolved`
  automatically covers any new PLACEHOLDER_* token.

## 7. Operator UX & error messages

### 7.1 Sentinel default

`config.yaml` schema gets new required field:

```yaml
cloudsql_private_ip: "cloudsql-private.invalid"  # operator MUST set
```

`scripts/config.py` validates on load. Sentinel value is rejected at
preflight time with the message:

```
config.cloudsql_private_ip is not set (got 'cloudsql-private.invalid').
Provide the private IP of your Cloud SQL instance (visible in GCP
Console → SQL → connections → Private IP). VPC peering between your
GKE VPC and the Cloud SQL service-networking VPC must be active.
See docs/operations/cloudsql-private-ip.md.
```

### 7.2 docs/operations/cloudsql-private-ip.md (new, brief)

Short doc explaining:
- How to find the private IP in GCP Console.
- How to verify VPC peering is active (gcloud command).
- Why install repo doesn't provision this yet (PR #5b future work).
- How to set it in `config.yaml`.

Roughly 80–120 lines, no marketing tone.

## 8. Migration considerations

This PR is **fresh-install only**. Any cluster previously deployed
from PR #4 main has `Deployment/cloudsql-proxy` running. PR #5a's
manifest removal does NOT prune it from the cluster (kustomize apply
doesn't delete).

Mitigations:
- PR description explicitly states fresh-install-only.
- Optional preflight check: if `kubectl get deploy/cloudsql-proxy -n infrastructure` succeeds, print a warning naming the manual cleanup steps:
  ```
  kubectl delete deploy cloudsql-proxy -n infrastructure
  kubectl delete svc cloudsql-proxy -n infrastructure
  kubectl delete sa cloudsql-proxy -n infrastructure
  ```
- This warning is best-effort and non-blocking (operator may not have
  kubectl context at preflight time).

PR #4 is freshly-merged today (2026-05-12) and the install repo is
still in pre-launch state; the universe of "operators with PR #4 main
already deployed" is likely just pchero himself. Migration risk is low.

## 9. Implementation order

1. Update `scripts/secret_schema.py` DSN defaults → use
   `PLACEHOLDER_CLOUDSQL_PRIVATE_IP` placeholder substring.
2. Update `k8s/backend/secret.yaml` DSN literals (3 keys).
3. Update `k8s/voip/secret.yaml` `DATABASE_ASTERISK_HOST` literal.
4. Update `scripts/k8s.py` substitution map: add
   `PLACEHOLDER_CLOUDSQL_PRIVATE_IP` + `_CIDR`.
5. Update `scripts/config.py` schema: new field `cloudsql_private_ip`
   with sentinel default, optional `cloudsql_private_ip_cidr`.
6. Update `scripts/preflight.py`: sentinel rejection check + best-effort
   in-cluster cloudsql-proxy detection warning.
7. Update `scripts/terraform_reconcile.py`: drop
   `cloudsql_proxy_sa_name` reference (silently ignore TF SA; PR #5b
   will delete).
8. Delete `k8s/infrastructure/cloudsql-proxy/` (3 files).
9. Update `k8s/infrastructure/kustomization.yaml`.
10. Update `k8s/network-policies/bin-manager-policies.yaml` (drop
    cloudsql-proxy egress; add Cloud SQL CIDR egress port 3306 + 5432).
11. Update `k8s/network-policies/voip-policies.yaml` (drop
    cloudsql-proxy egress; add Cloud SQL CIDR egress port 3306).
12. Update `k8s/network-policies/infrastructure-policies.yaml` (drop
    cloudsql-proxy ingress rules).
13. Update existing tests.
14. Add `tests/test_pr5a_cloudsql_removal.py` (5 tests).
15. Write `docs/operations/cloudsql-private-ip.md`.
16. Run pytest, kustomize build, sensitive audit.
17. Commit, push, open PR.
18. PR review iter 1 / 2 / 3 (mandatory, minimum 3).

## 10. Risks

- **CIDR scope**: `/32` is correct for single-instance Cloud SQL.
  Regional-HA (`availability_type=REGIONAL`) failover keeps the same
  private IP (per Google docs); but operator with custom topology can
  override `cloudsql_private_ip_cidr`. Risk: operator may run with `/32`
  and a stale-from-failover IP. Acceptable: documented in
  `docs/operations/cloudsql-private-ip.md`.
- **rag-manager postgres egress**: First explicit allow-list of port
  5432. Verify rendered NetworkPolicy correctness via test #4.
- **In-cluster phantom from PR #4**: handled via best-effort preflight
  warning (§8).

## 11. Checklist

- [x] Production cloudsql-proxy absence confirmed
- [x] DSN private IP pattern extracted from production (value redacted in doc)
- [x] Scope split: 5a (manifest layer) vs 5b (Terraform private IP)
- [x] All DSN consumers (including rag-manager postgres) enumerated
- [x] Sentinel + RFC2606 invalid pattern for safe defaults
- [x] Sensitive audit clean (no real prod IP in design doc)
- [x] Operator UX error message specified
- [ ] Design review iter 1 (this rewrite — iter 2 effectively)
- [ ] Design review iter 2 (minimum)
