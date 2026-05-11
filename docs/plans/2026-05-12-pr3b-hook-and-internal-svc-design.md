# PR #3b design: hook-manager LB + api-manager-internal split + frontend sidecar + square-manager namespace

**Date:** 2026-05-12
**Author:** Hermes (CPO)
**Status:** Draft (iteration 2, addressing iter-1 review findings)
**Parent plan:** `docs/plans/2026-05-11-self-hosting-architecture-redesign.md`
**Prior PRs:** #9 (parent plan), #10 (static IPs), #11 (PR #3a: TLS bootstrap + 4 LB flips + ingress removal)
**Branch:** `NOJIRA-PR3b-hook-and-internal-svc`
**Target audience:** fresh self-hosting installers (no migration constraint)

---

## 1. Goal

Close the two transitional gaps introduced by PR #3a and complete the
external-traffic cutover for `hook-manager`:

1. **Restore `api-manager` internal endpoints**: PR #3a stripped ports
   2112 (Prometheus scrape) and 9000 (audiosocket) from the public
   LB. PR #3b creates `api-manager-internal` ClusterIP Service to
   restore cluster-DNS access for in-cluster callers (Prometheus,
   Asterisk Pods in `voip` namespace).
2. **Flip `hook-manager` to LoadBalancer**: PR #3a kept it as ClusterIP
   because the Pod had no 80/443 listener. PR #3b adds 80/443
   containerPorts to the Deployment, flips Service to LoadBalancer
   binding to the PR #10 reserved static IP, and adds NetworkPolicy
   ingress permission.
3. **Move frontends to `square-manager` namespace and rename**:
   `square-admin`/`square-talk`/`square-meet` Deployments and Services
   in `bin-manager` ns move to a new `square-manager` ns with names
   `admin`/`talk`/`meet`. Per-Service LoadBalancer manifests are
   regenerated under the new namespace.
4. **Add nginx TLS-termination sidecar to frontends**: each frontend
   Pod gains an nginx sidecar that terminates TLS on port 443 using
   the `voipbin-tls` Secret (created in PR #3a). The frontend
   container's plain-HTTP port 80 stays as the upstream behind the
   sidecar. LB Service flips from port 80 to port 443.

After PR #3b merges, a fresh install delivers:
- 5 LoadBalancers (api, hook, admin, talk, meet) all serving HTTPS via
  the installer-managed self-signed cert.
- Operator creates all 5 DNS A records.
- Prometheus + Asterisk Pods reach `api-manager` internal endpoints
  via `api-manager-internal.bin-manager.svc.cluster.local`.

PR #3c follows with config-schema cleanup, Terraform variable updates,
README/dns-guide rewrite, and `verify.check_tls_cert_is_production`.

### Decision lineage

- pchero (2026-05-12, PR #3a session): split PR #3 into 3a/3b/3c.
- pchero (2026-05-12, PR #3a design doc §6.2): `api-manager-internal`
  creation is the highest-priority gating item among PR #3b items.

---

## 2. Non-goals (in PR #3b)

- `tls_strategy` enum reduction in config schema. PR #3c.
- README / dns-guide rewrite. PR #3c.
- `verify.check_tls_cert_is_production`. PR #3c.
- Production-parity workloads (number-renew CronJob, monitoring-tests,
  Prometheus/Grafana/Alertmanager, Heplify). PR #4.
- `cloudsql-proxy` removal. PR #5.
- Changes to `bin-api-manager`/`bin-hook-manager` Go code (Pod-level
  TLS continues to use the env-injection pattern unchanged).
- Adding a second TLS Secret per service. The single `voipbin-tls`
  Secret in `bin-manager` (from PR #3a) is the source. PR #3b copies
  it into `square-manager` via Helm-style render or `kubectl get
  secret ... | sed | kubectl apply`. Implementation detail in §6.

---

## 3. Inputs

### 3.1 What is in `main` today (post PR #11)

- 4 Services in `bin-manager` are `type: LoadBalancer`: `api-manager`,
  `square-admin`, `square-talk`, `square-meet`.
- `hook-manager` Service is still `ClusterIP` exposing only port 2112
  (metrics). Deployment exposes only `containerPort: 2112`.
- `voipbin-tls` Secret exists in `bin-manager` namespace (created by
  `scripts/tls_bootstrap.py` at first `init` run).
- `voipbin-secret` holds `SSL_CERT_BASE64` + `SSL_PRIVKEY_BASE64`
  consumed by `bin-api-manager` and `bin-hook-manager` Go binaries.
- `k8s/network-policies/bin-manager-policies.yaml`:
  - `allow-ingress-to-api` permits TCP 443 + 9000.
  - `allow-ingress-to-frontends` permits TCP 80 on `square-*`.
  - No `hook-manager` ingress rule yet.
- `k8s/namespaces.yaml`: 3 namespaces (`bin-manager`, `infrastructure`,
  `voip`). No `square-manager`, no `monitoring`.
- 5 `PLACEHOLDER_STATIC_IP_ADDRESS_*` tokens in `scripts/k8s.py`. The
  HOOK_MANAGER token is unused by any manifest in main.
- `scripts/preflight.py`: `check_nodeport_availability(needed=4)`.
- `scripts/tls_bootstrap.py`: idempotent + atomic-pair contract;
  bootstraps `voipbin-tls` in `bin-manager`.

### 3.2 Why this PR is mergeable on its own

A fresh installer who runs through PR #3b:
- Has 5 working LBs all serving HTTPS (self-signed cert).
- Has `api-manager-internal` Service for in-cluster Prometheus +
  Asterisk audiosocket.
- Has `square-manager` ns with admin/talk/meet workloads.
- Has `hook-manager` Pod serving HTTP on 80 and HTTPS on 443.

Audiosocket from `voip` ns Asterisk Pods now resolves to
`api-manager-internal.bin-manager.svc.cluster.local:9000`, restoring
the production-parity in-cluster path that PR #3a temporarily removed.

PR #3c is still required for config-schema cleanup and operator-facing
docs, but the platform-level state is functionally complete at PR #3b.

---

## 4. Architecture (after PR #3b merges)

```
Internet
   |
   |  Operator-managed DNS A records: 5 hostnames → 5 reserved LB IPs
   v
GCP regional Service LoadBalancers (5 total, all on port 443)
   |
   +-- api.<domain>   → bin-manager/api-manager LB → Pod 443
   |                    (Pod TLS via voipbin-secret env)
   +-- hook.<domain>  → bin-manager/hook-manager LB → Pod 443 (TLS)
   |                                                + Pod 80 (HTTP fallback for legacy webhook providers)
   +-- admin.<domain> → square-manager/admin LB → nginx sidecar 443
   |                    → upstream container 80
   +-- talk.<domain>  → square-manager/talk LB  → nginx sidecar 443
   |                    → upstream container 80
   +-- meet.<domain>  → square-manager/meet LB  → nginx sidecar 443
                        → upstream container 80

In-cluster only (no external LB):
   bin-manager/api-manager-internal ClusterIP
     ports: 2112 (metrics scrape), 9000 (audiosocket TCP)
   voip/asterisk-* Pods → api-manager-internal.bin-manager:9000
   monitoring/prometheus → api-manager-internal.bin-manager:2112
```

Namespaces: `bin-manager`, `infrastructure`, `voip`, **`square-manager`** (new).

---

## 5. Changes

### 5.1 `k8s/namespaces.yaml`: add `square-manager`

```yaml
---
apiVersion: v1
kind: Namespace
metadata:
  name: square-manager
  labels:
    app.kubernetes.io/part-of: voipbin
    pod-security.kubernetes.io/enforce: baseline
```

### 5.2 `k8s/backend/services/hook-manager.yaml`

Deployment gains 80 and 443 containerPorts; replicas bumped to 2 to
work safely with `externalTrafficPolicy: Local`. Service flips to
LoadBalancer:

```yaml
# Deployment.spec
replicas: 2

# Deployment.spec.template.spec.containers[0].ports
- containerPort: 80
  name: http
- containerPort: 443
  name: https
- containerPort: 2112
  name: metrics

# Service
spec:
  type: LoadBalancer
  loadBalancerIP: PLACEHOLDER_STATIC_IP_ADDRESS_HOOK_MANAGER
  externalTrafficPolicy: Local   # preserve client IP for webhook providers
  ports:
    - name: http
      port: 80
      targetPort: 80
    - name: https
      port: 443
      targetPort: 443
  selector:
    app: hook-manager
```

Add `k8s/backend/services/hook-manager-pdb.yaml`:

```yaml
apiVersion: policy/v1
kind: PodDisruptionBudget
metadata:
  name: hook-manager-pdb
  namespace: bin-manager
spec:
  minAvailable: 1
  selector:
    matchLabels:
      app: hook-manager
```

**Why `replicas: 2` + PDB instead of `replicas: 1`:** with
`externalTrafficPolicy: Local`, GCP LB only routes to nodes that host
a hook-manager Pod. If only 1 replica exists and its node drains for
upgrade, traffic returns 503 until reschedule (potentially minutes).
2 replicas + PDB(minAvailable=1) guarantees a Pod is always serving
during voluntary disruptions. Resource cost: ~50m CPU + 64Mi memory
per extra replica (the existing limits in main); acceptable for
fresh-install minimal baseline because hook-manager is on the public
ingress path.

`externalTrafficPolicy: Local` is selected here (not `Cluster` as on
the other 4 LBs) because hook-manager receives webhook callbacks from
external providers; preserving the source IP enables operator-side
audit logs and IP allowlisting. `Local` requires a healthCheckNodePort
which GCP allocates automatically; counted in NodePort preflight (§5.7).

The metrics port (2112) is intentionally NOT exposed via this
LoadBalancer. Prometheus reaches it via Pod IP scraping (kubelet
annotations already in place); no separate `hook-manager-internal`
Service is needed because 2112 is not a service endpoint, only a
scrape target.

### 5.3 `k8s/backend/services/api-manager-internal.yaml` (new file)

```yaml
apiVersion: v1
kind: Service
metadata:
  name: api-manager-internal
  namespace: bin-manager
  labels:
    app: api-manager
spec:
  type: ClusterIP
  ports:
    - name: metrics
      port: 2112
      targetPort: 2112
    - name: audiosocket
      port: 9000
      targetPort: 9000
  selector:
    app: api-manager
```

Same selector as the external `api-manager` Service. ClusterIP only,
no public exposure.

**Scope clarification (verified via grep of `k8s/voip/` and
`k8s/network-policies/voip-policies.yaml` in this worktree, both
empty for `api-manager`/`9000`/`audiosocket`).** In the install repo
today, NO consumer of port 9000 or 2112 exists yet:
- Asterisk dial-string for audiosocket lives in the monorepo Asterisk
  image / Helm chart fragments, not in this install repo.
- Prometheus scrape ConfigMap is deferred to PR #4.

Therefore the `api-manager-internal` Service in this PR is
**forward-looking infrastructure**: it provides the in-cluster DNS
endpoint that the monorepo Asterisk chart and PR #4 Prometheus will
target. PR #3a's "highest unblocking priority" framing assumed the
audiosocket consumer was in this repo; that assumption was wrong.
Re-framed correctly, this PR adds the Service so that:

1. The monorepo-side Asterisk chart (separate PR, separate sequencing,
   handled by pchero) gains a stable internal target it can dial
   without referencing the external LB Service.
2. PR #4 (Prometheus) does not have to add this Service as part of its
   own scope.

No `bin-api-manager` Go code change is required (the Pod listens on
2112 and 9000 already; only the Kubernetes Service envelope is being
added).

### 5.4 Frontends: move to `square-manager` + rename + add nginx sidecar

Delete 3 files from `k8s/frontend/`:
- `square-admin.yaml`, `square-talk.yaml`, `square-meet.yaml`

Create 3 replacement files (`admin.yaml`, `talk.yaml`, `meet.yaml`) in
the same directory with these properties:

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: admin                    # was: square-admin
  namespace: square-manager      # was: bin-manager
  labels:
    app: admin
spec:
  replicas: 1
  selector:
    matchLabels:
      app: admin
  template:
    metadata:
      labels:
        app: admin
    spec:
      containers:
        - name: admin
          image: voipbin/square-admin   # image name unchanged
          ports:
            - containerPort: 80
              name: http
          # liveness/readiness unchanged
        - name: nginx-tls
          image: nginx:1.27-alpine
          ports:
            - containerPort: 443
              name: https
          resources:
            requests:
              cpu: "10m"
              memory: "32Mi"
            limits:
              cpu: "100m"
              memory: "64Mi"
          volumeMounts:
            - name: tls
              mountPath: /etc/nginx/tls
              readOnly: true
            - name: nginx-conf
              mountPath: /etc/nginx/conf.d
              readOnly: true
      volumes:
        - name: tls
          secret:
            secretName: voipbin-tls
        - name: nginx-conf
          configMap:
            name: frontend-tls-proxy
---
apiVersion: v1
kind: Service
metadata:
  name: admin
  namespace: square-manager
  labels:
    app: admin
spec:
  type: LoadBalancer
  loadBalancerIP: PLACEHOLDER_STATIC_IP_ADDRESS_ADMIN
  externalTrafficPolicy: Cluster
  ports:
    - name: https
      port: 443
      targetPort: 443
  selector:
    app: admin
```

Repeat for `talk.yaml` and `meet.yaml`. Image names stay
`voipbin/square-admin|talk|meet` (no monorepo coupling).

### 5.5 `k8s/frontend/tls-proxy-configmap.yaml` (new)

Shared nginx config for the 3 frontend sidecars:

```yaml
apiVersion: v1
kind: ConfigMap
metadata:
  name: frontend-tls-proxy
  namespace: square-manager
data:
  default.conf: |
    server {
      listen 443 ssl;
      ssl_certificate     /etc/nginx/tls/tls.crt;
      ssl_certificate_key /etc/nginx/tls/tls.key;
      ssl_protocols       TLSv1.2 TLSv1.3;
      location / {
        proxy_pass http://127.0.0.1:80;
        proxy_set_header Host              $host;
        proxy_set_header X-Real-IP         $remote_addr;
        proxy_set_header X-Forwarded-For   $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto https;
      }
    }
```

Single ConfigMap shared across 3 Deployments (admin/talk/meet) — the
proxy logic is identical because the upstream is always `127.0.0.1:80`
on the same Pod.

### 5.6 `voipbin-tls` Secret replication to `square-manager`

Two strategy options:

**(A) Bootstrap creates Secret in BOTH namespaces.** Modify
`scripts/tls_bootstrap.py` to accept a list of target namespaces. Both
`bin-manager` and `square-manager` get the same cert from a single
generation run.

**(B) Use a Reflector-style controller (kube-reflector or similar).**
Rejected: adds an external dependency that violates "minimal
installer" principle.

**Choice: (A).** `bootstrap_voipbin_tls_secret` gains
`namespaces: list[str] = [DEFAULT_NAMESPACE, "square-manager"]`. The
`voipbin-secret` patch still runs only in `bin-manager` (the Opaque
Secret consumed by `bin-api-manager`/`bin-hook-manager`); the
`square-manager` namespace only needs the `voipbin-tls`
kubernetes.io/tls Secret for sidecar mount.

**Consistency semantics (not transactional, self-healing on retry).**
The two kubectl creates (`bin-manager/voipbin-tls`,
`square-manager/voipbin-tls`) are sequential, not atomic. If create N
succeeds and create N+1 fails:
- The script raises `BootstrapError` and aborts `init`.
- On the next `init` invocation, the stale-cleanup logic from PR #3a
  triggers because the opaque Secret SSL keys are still empty AND at
  least one `voipbin-tls` Secret exists somewhere. The cleanup MUST
  iterate over ALL configured namespaces and delete `voipbin-tls`
  (using `--ignore-not-found`) so that the fresh-cert path can
  regenerate a single cert and re-create the pair from scratch.
- During the failure window before retry, sidecars in
  `square-manager` that already started will stay in
  `ContainerCreating` (FailedMount events) on the missing Secret
  mount until the next bootstrap pass creates it. This is acceptable
  for fresh install because
  manifest apply happens BEFORE bootstrap (existing
  `scripts/k8s.py` ordering — sidecar Pods don't exist until after
  bootstrap runs).

This contract is "self-healing on retry," not "atomic." Tests must
cover the multi-namespace stale-cleanup path explicitly.

### 5.7 `scripts/preflight.py`: `check_nodeport_availability(needed=7)`

| Service | Ports | NodePorts |
|---|---|---|
| api-manager LB | 443 | 1 |
| hook-manager LB | 80, 443 | 2 |
| hook-manager LB healthCheckNodePort (externalTrafficPolicy=Local) | 1 |
| admin LB | 443 | 1 |
| talk LB | 443 | 1 |
| meet LB | 443 | 1 |
| **Total** | — | **7** |

Bump `needed=4` → `needed=7`.

### 5.8 NetworkPolicy

`k8s/network-policies/bin-manager-policies.yaml`:
- Remove `allow-ingress-to-frontends` (frontends moved out of namespace).
- Add `allow-ingress-to-hook`:

```yaml
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: allow-ingress-to-hook
  namespace: bin-manager
spec:
  podSelector:
    matchLabels:
      app: hook-manager
  policyTypes:
    - Ingress
  ingress:
    - ports:
        - protocol: TCP
          port: 80
        - protocol: TCP
          port: 443
        - protocol: TCP
          port: 2112   # in-cluster Prometheus scrape (when PR #4 adds it)
```

- **Do NOT trim `allow-ingress-to-api`.** NetworkPolicy `podSelector`
  selects Pods, not Services. The new `api-manager-internal` ClusterIP
  routes to the SAME Pod set as the external LB. Removing port 9000
  from the policy would drop in-cluster audiosocket ingress to those
  Pods regardless of which Service fronts them. Keep the existing
  `ports: [{TCP,443}, {TCP,9000}]` ingress rule unchanged. The 2112
  metrics port is NOT in the current rule and stays out (kubelet
  scrape uses Pod IP host-network bypass, not NetworkPolicy-gated
  traffic; if PR #4 ships an in-cluster Prometheus Pod, that PR adds
  a 2112 ingress allow then). Optional hardening (defer to PR #3c or
  later): scope `from:` clause to `namespaceSelector: voip` for the
  9000 rule once voip ns Asterisk Pods stabilize their labels.

`k8s/network-policies/square-manager-policies.yaml` (new file):
default-deny ingress + explicit allow for LB → Pod 443:

```yaml
---
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: default-deny-ingress
  namespace: square-manager
spec:
  podSelector: {}
  policyTypes:
    - Ingress
---
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: allow-ingress-to-frontends
  namespace: square-manager
spec:
  podSelector:
    matchExpressions:
      - key: app
        operator: In
        values: [admin, talk, meet]
  policyTypes:
    - Ingress
  ingress:
    - ports:
        - protocol: TCP
          port: 443
---
# Egress: frontends need DNS + (no backend calls; browsers call api directly)
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: allow-dns
  namespace: square-manager
spec:
  podSelector: {}
  policyTypes:
    - Egress
  egress:
    - to: []
      ports:
        - protocol: UDP
          port: 53
        - protocol: TCP
          port: 53
```

### 5.9 Kustomization wiring

Three nested kustomization files require updates (single
`k8s/kustomization.yaml` at the root is not a generator-style root in
this repo):

- `k8s/frontend/kustomization.yaml`: replace `square-{admin,talk,meet}.yaml`
  entries with `{admin,talk,meet}.yaml` and add `tls-proxy-configmap.yaml`.
- `k8s/backend/services/kustomization.yaml`: add `api-manager-internal.yaml`
  and `hook-manager-pdb.yaml`.
- `k8s/network-policies/kustomization.yaml`: add
  `square-manager-policies.yaml`.

Implementation Step 5j enumerates each file edit. Implementation MUST
`grep -rn "square-admin\|square-talk\|square-meet" .` first to verify
no other reference is missed.

### 5.10 `scripts/tls_bootstrap.py` multi-namespace

Function signature update:

```python
def bootstrap_voipbin_tls_secret(
    namespaces: list[str] | None = None,   # NEW (replaces `namespace: str`)
    hostnames: list[str] | None = None,
    tls_secret_name: str = DEFAULT_TLS_SECRET,
    opaque_secret_name: str = DEFAULT_OPAQUE_SECRET,
    opaque_secret_namespace: str = DEFAULT_NAMESPACE,   # NEW (bin-manager only)
    valid_days: int = DEFAULT_VALID_DAYS,
) -> BootstrapResult:
```

Default `namespaces = ["bin-manager", "square-manager"]`.

`voipbin-tls` is created in EACH namespace (loop). `voipbin-secret`
patch runs only against `opaque_secret_namespace="bin-manager"` because
only `bin-api-manager`/`bin-hook-manager` consume the env-var pair.

Self-healing-on-retry contract:
- "fresh-generate path" trigger: SSL keys empty in `voipbin-secret`
  AND `voipbin-tls` missing from ALL configured namespaces.
- "stale cleanup" trigger: SSL keys empty AND `voipbin-tls` exists in
  ANY namespace. Iterate ALL configured namespaces, delete
  `voipbin-tls` (each with `--ignore-not-found`), then proceed with
  single-cert generation + multi-ns create.
- "skipped-prefilled" trigger: SSL keys populated. Skip all writes.
- "partial fill" hard-error: unchanged.

**Namespace pre-existence requirement.** Bootstrap is called from
`scripts/k8s.py` AFTER manifest apply (PR #3a ordering). Manifest
apply includes `k8s/namespaces.yaml` which now contains the
`square-manager` Namespace with PSS=baseline label. Therefore both
namespaces exist before bootstrap runs and the existing
`_ensure_namespace` defensive create (PR #3a §5.x) handles the
edge case where an operator deletes a namespace between apply and
bootstrap. Bootstrap does NOT add PSS labels itself; the canonical
labels come from `k8s/namespaces.yaml` (single source of truth).

`BootstrapResult.voipbin_tls_action` becomes a `dict[str, str]`
mapping namespace → action (`"created"` or `"skipped"`).

**Migration note.** Existing callers in `scripts/k8s.py` use only
keyword arguments. The signature change is back-compat at the call
boundary, but all test assertions of the form
`result.voipbin_tls_action == "created"` need to be rewritten to
either `result.voipbin_tls_action == {"bin-manager": "created",
"square-manager": "created"}` or
`all(v == "created" for v in result.voipbin_tls_action.values())`.
Implementation Step 2 enumerates each test that needs updating
(currently 4 assertions in `test_tls_bootstrap.py`).

### 5.11 `scripts/k8s.py`

- Update call to `bootstrap_voipbin_tls_secret`:

```python
result = bootstrap_voipbin_tls_secret(
    namespaces=["bin-manager", "square-manager"],
    hostnames=hostnames,
)
```

- Update display logic to iterate `result.voipbin_tls_action.items()`.

### 5.12 Asterisk + audiosocket address coupling — DEFERRED to monorepo

**Verified (grep `k8s/voip/` and `k8s/network-policies/voip-policies.yaml`):**
this install repo has ZERO references to `api-manager`, `9000`, or
`audiosocket` in any voip-namespace manifest or NetworkPolicy.

The audiosocket dial string is owned by the monorepo Asterisk image
or its Helm chart fragment (not in this repo). Therefore PR #3b's
`api-manager-internal` Service (§5.3) is forward-looking: it
publishes a stable in-cluster DNS endpoint that the monorepo Asterisk
PR can target, but PR #3b does NOT need to touch any address strings
in install.

Action items for pchero (separate, monorepo-side):
- Open a monorepo PR that updates the Asterisk image / chart audiosocket
  dial address to `api-manager-internal.bin-manager.svc.cluster.local:9000`.
- That PR is independent of this install PR; no cross-repo sequencing
  required because both old (`api-manager.bin-manager:9000` via LB
  ClusterIP-routed Pod selector) and new (`api-manager-internal:9000`)
  resolve to the same Pod set during the transition.

This PR's §10 review checklist removes the "Asterisk ConfigMap
updates" item.

---

## 6. Risks

| # | Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|---|
| 1 | `square-manager` Secret replication is not transactional; partial-failure window allows sidecar CrashLoop until retry | Med | Med | Tests cover multi-namespace stale-cleanup. Sidecars CrashLoop is acceptable because manifest apply happens before bootstrap; on `init` failure operator simply re-runs. Re-framed contract as "self-healing on retry" (§5.6). |
| 2 | hook-manager nginx-style HTTP→HTTPS providers reject self-signed cert | Expected | Med | Documented (PR #3c README). Operator MUST replace cert before production webhook providers. |
| 3 | ~~Asterisk audiosocket address coupling in monorepo~~ | n/a | n/a | Verified iter 2: monorepo-side. §5.12. Install repo adds forward-looking `api-manager-internal` Service only. |
| 4 | `externalTrafficPolicy: Local` on hook-manager with low replicas → outage on node drain | Low (with mitigation) | Med | Locked `replicas: 2` + PDB(minAvailable=1) at §5.2. Eliminates single-Pod node-drain footgun. |
| 5 | nginx sidecar adds ~10MB RAM and ~2% CPU per frontend Pod | Expected | Low | Resource requests/limits added to sidecar spec (10m CPU, 32Mi memory). |
| 6 | NodePort preflight bump from 4 to 7 might warn on tight clusters | Low | Low | Already non-fatal warning. |
| 7 | Removing `allow-ingress-to-frontends` from bin-manager-policies before frontends actually move could cause brief unreachability | Low | Low | Atomic via `kubectl apply -k` ordering: namespace+policies+deployments apply in one transaction. |
| 8 | nginx 1.27-alpine image pinning drift | Low | Low | Pin minor version. Document upgrade procedure. |
| 9 | `voipbin-tls` cert SAN already includes 5 hosts (PR #3a §5.1) so no regeneration needed when adding to square-manager ns | n/a | n/a | Verified: PR #3a SAN list covers api/hook/admin/talk/meet. |
| 10 | Prometheus scrape ConfigMap pointing at old `api-manager:2112` not yet in main (PR #4 scope) | n/a | n/a | Confirmed iter 2: absent. The rename is forward-only; no breakage. |
| 11 | bin-api-manager Pod restart needed after voipbin-secret patch is already covered in PR #3a `scripts/k8s.py` post-bootstrap rollout-restart | n/a | n/a | Existing behavior. PR #3b extends the restart targets to include any sidecar-bearing Pods if applicable (admin/talk/meet rolling restart on first sidecar add — handled by `kubectl apply` reconcile naturally). |
| 12 | `_ensure_namespace` race: bootstrap auto-creates `square-manager` before `kubectl apply -k` overlays the PSS=baseline label | Low | Low | Manifest apply runs BEFORE bootstrap (existing ordering); namespaces.yaml apply creates `square-manager` with the label. `_ensure_namespace` is purely defensive for operator-deletion edge cases. §5.10 documents. |

---

## 7. Implementation order

**Step 1: Pre-edit verification (audit) — DONE in iter 2**
- a. ✅ `grep -rn "api-manager\|9000\|audiosocket" k8s/voip/
     k8s/network-policies/voip-policies.yaml` → zero hits. Audiosocket
     dial address is in monorepo, not install. §5.12 deferred.
- b. `grep -rn "square-admin\|square-talk\|square-meet" .` (in
     implementation Step 5 before file moves) to find all references
     that need updating (kustomization, network-policies, scripts).
- c. ✅ Confirmed `voipbin-tls` SAN list from PR #3a covers all 5 hosts
     (`scripts/tls_bootstrap.py:42` DEFAULT_HOSTS).

**Step 2:** `scripts/tls_bootstrap.py` multi-namespace refactor + tests:
- API change: `namespace: str` → `namespaces: list[str]`.
- BootstrapResult.voipbin_tls_action → `dict[str, str]`.
- Stale-cleanup iterates ALL configured namespaces.
- Update 4 existing assertions in `tests/test_tls_bootstrap.py` to
  the dict shape.
- Add 5 new test cases covering multi-ns paths (§8).

**Step 3:** `scripts/k8s.py` caller update + display tweak.

**Step 4:** `scripts/preflight.py` `needed=7`.

**Step 5:** Manifest edits:
- a. `k8s/namespaces.yaml` add `square-manager` (PSS=baseline).
- b. `k8s/backend/services/hook-manager.yaml` Deployment (replicas=2,
     80/443/2112 ports) + Service (LoadBalancer 80/443, externalTrafficPolicy=Local).
- c. `k8s/backend/services/hook-manager-pdb.yaml` (new PDB minAvailable=1).
- d. `k8s/backend/services/api-manager-internal.yaml` (new ClusterIP 2112+9000).
- e. Delete 3 `k8s/frontend/square-*.yaml` files.
- f. Create 3 `k8s/frontend/{admin,talk,meet}.yaml` with sidecar.
- g. `k8s/frontend/tls-proxy-configmap.yaml` (new).
- h. `k8s/network-policies/bin-manager-policies.yaml`: remove
     `allow-ingress-to-frontends`, add `allow-ingress-to-hook` (TCP
     80/443/2112). Do NOT trim `allow-ingress-to-api`.
- i. `k8s/network-policies/square-manager-policies.yaml` (new).
- j. `k8s/kustomization.yaml` reference updates.

**Step 6:** Sensitive-data audit gate
(`scripts/dev/check-plan-sensitive.sh`).

**Step 7:** Full test suite + `kubectl apply --dry-run=server -k k8s/`
smoke (post-substitution).

---

## 8. Tests

| File | Tests |
|---|---|
| `tests/test_tls_bootstrap.py` (extend) | (1) multi-ns create: both nss get voipbin-tls; (2) multi-ns stale cleanup: voipbin-tls in one of 2 nss triggers delete in BOTH; (3) namespace-mismatched opaque-secret patch still runs only in bin-manager; (4) per-namespace action dict shape; (5) failure in second-ns create after first-ns create succeeds → both rolled back via delete on retry. |
| `tests/test_k8s.py` (extend) | Updated bootstrap call wiring; multi-ns assertion. |
| `tests/test_preflight.py` (extend) | `needed=7` default; 7 free pass; 6 free warn. |
| `tests/test_yaml_lint.py` (if exists, else add) | All new manifest files parse as valid YAML. |

Target: 342 + ~12 new ≈ 354 passing.

---

## 9. Open questions

1. ~~Asterisk audiosocket address coupling~~ ✅ Resolved iter 2:
   monorepo-owned; install repo adds forward-looking Service only. See
   §5.12.
2. ~~Prometheus scrape ConfigMap location~~ ✅ Resolved iter 2:
   deferred to PR #4; `api-manager-internal` Service `metrics: 2112`
   port is forward-looking.
3. ~~hook-manager `replicas: 1` vs `replicas: 2` trade-off~~ ✅
   Resolved iter 2: locked at 2 with PDB(minAvailable=1) per §5.2. The
   extra Pod (~50m CPU, 64Mi RAM) is acceptable because hook-manager
   is on the public ingress path and `externalTrafficPolicy: Local`
   makes single-replica vulnerable to node drain.

No open questions remain. Implementation can proceed after iter 2 design review.

---

## 10. Review checklist

- [ ] Sensitive-data audit passes.
- [ ] `square-manager` namespace added with PSS=baseline label.
- [ ] hook-manager Deployment has 80, 443, 2112 containerPorts.
- [ ] hook-manager Service is `type: LoadBalancer` on 80+443 with
      `externalTrafficPolicy: Local` and `loadBalancerIP` placeholder.
- [ ] `api-manager-internal` ClusterIP Service exposes 2112 + 9000.
- [ ] Frontend Deployments + Services moved to `square-manager` ns
      and renamed to `admin`/`talk`/`meet`.
- [ ] Each frontend Pod has nginx-tls sidecar mounting `voipbin-tls`
      + `frontend-tls-proxy` ConfigMap.
- [ ] Frontend LB Services expose port 443 (not 80).
- [ ] `frontend-tls-proxy` ConfigMap created in `square-manager`.
- [ ] `scripts/tls_bootstrap.py` accepts `namespaces: list[str]` and
      creates `voipbin-tls` in both `bin-manager` and `square-manager`.
- [ ] Multi-ns stale cleanup tested.
- [ ] `bootstrap_voipbin_tls_secret` opaque-secret patch still runs
      only in `bin-manager`.
- [ ] `scripts/preflight.py` `needed=7`.
- [ ] `bin-manager-policies.yaml`: `allow-ingress-to-frontends`
      removed, `allow-ingress-to-hook` added, `allow-ingress-to-api`
      LEFT UNCHANGED (TCP 443+9000).
- [ ] `square-manager-policies.yaml` new with default-deny + allow.
- [ ] `k8s/kustomization.yaml` references all new + removed paths.
- [ ] `hook-manager-pdb.yaml` (PDB minAvailable=1) included.
- [ ] hook-manager Deployment `replicas: 2`.
- [ ] No Asterisk address change in this PR (deferred to monorepo PR
      per §5.12).
- [ ] `kubectl apply --dry-run=server -k k8s/` passes after substitution.
- [ ] No PR #3c/#4/#5 scope leakage.
- [ ] No real production IDs, IPs, domain names, or Cloud SQL instance
      names anywhere.
