# PR-U-1. Homer/heplify-server k8s manifests (Internal LB)

Status. Draft v2 (iter 1 findings applied) → Iter-2 design review pending → APPROVED → Implementation

## 1. Problem statement

VoIPBin install repo currently has no HOMER (SIP capture) component. The Kamailio container image (`voipbin/voip-kamailio`) requires the env var `HOMER_URI` to be non-empty during entrypoint validation; with `HOMER_URI=` (empty), the entrypoint exits 1 and the container CrashLoops. This is the last remaining install-repo blocker for Kamailio HEALTHY, observed live on `voipbin-install-dev` after PR-T2 merged.

Production (`monorepo-voip/voip-homer`) runs heplify-server + homer-webapp on GKE in the `infrastructure` namespace, behind two internal LoadBalancer Services (one TCP for ports 9060/9061/9090/9096/80, one UDP for 9060). The installer must ship the same topology.

This PR (U-1) ports the production Kustomize manifests into the installer's `k8s/infrastructure/homer/` directory, registers them with kustomize, and exposes the heplify UDP LoadBalancer IP via the existing `_LB_SERVICES` harvest mechanism so PR-U-3 can wire `HOMER_URI` on the Kamailio VM.

PostgreSQL `homer_data` / `homer_config` databases on the existing CloudSQL Postgres instance are **out of scope** for this PR (PR-U-2). Kamailio docker-compose `heplify-client` sidecar + `HOMER_URI` env wiring is **out of scope** (PR-U-3).

## 2. Goals

1. Manifests deployed to `infrastructure` namespace at `k8s/apply` stage.
2. Two internal LoadBalancer Services (`heplify-tcp`, `heplify-udp`) reachable from `voip` namespace at internal LB IPs.
3. heplify-server UDP LB IP harvested by `reconcile_k8s_outputs` stage and persisted as `heplify_lb_ip` in `state.yaml.k8s_outputs`.
4. heplify-server flat-var threaded through `ansible_runner._write_extra_vars` so PR-U-3 can read it.
5. heplify-server Pod will be in `CrashLoopBackOff` after merge because Postgres DBs + egress NetworkPolicy are absent. **This is expected and documented in §8.** PR-U-2 fixes it.

## 3. Decisions locked (2026-05-13, pchero)

| # | Question | Decision |
|---|---|---|
| 1 | PostgreSQL instance for HOMER | Reuse existing CloudSQL Postgres (PR-D1). No separate DB |
| 2 | HOMER DB user | Reuse `voipbin` user (no separate `heplify` user) |
| 3 | `homer7` schema bootstrap | heplify-server's first-boot auto-migration. **UNVERIFIED** against upstream `sipcapture/heplify-server` source; treated as PR-U-2 acceptance-test risk with fallback plan (run upstream `homer-app/scripts/migration/postgres/*.sql` manually if first-boot does not create tables) |
| 4 | homer-webapp external exposure | None. Internal LB only. Operator access via `kubectl port-forward` |
| 5 | PR split order | U-1 (manifests) → U-2 (Postgres DBs + env wiring + NetworkPolicy + GCP firewall) → U-3 (Kamailio sidecar + HOMER_URI) |

## 4. Affected files

| File | Why | New/Modified |
|---|---|---|
| `k8s/infrastructure/homer/deployment.yaml` | heplify + homer-webapp 2-container Deployment, env vars to be substituted in PR-U-2 (left as `PLACEHOLDER_*` for now) | new |
| `k8s/infrastructure/homer/service.yaml` | heplify-tcp + heplify-udp Services with `cloud.google.com/load-balancer-type: "Internal"` annotation | new |
| `k8s/infrastructure/homer/kustomization.yaml` | Lists `deployment.yaml`, `service.yaml`. Image pins `sipcapture/heplify-server:1.30`, `pchero/homer-app:0.0.4`. Does NOT set kustomize-level `namespace:` directive (mirrors sibling pattern in redis/rabbitmq/clickhouse — they rely on per-resource `metadata.namespace`). The Namespace itself comes from top-level `k8s/namespaces.yaml` | new |
| `k8s/infrastructure/kustomization.yaml` | Add `- homer` to resources list | modified (1 line) |
| `scripts/k8s.py` | Add `("infrastructure", "heplify-udp", "heplify_lb_ip")` to `_LB_SERVICES` | modified (1 tuple) |
| `scripts/ansible_runner.py` | Add `heplify_lb_ip` flat-var with same `or ""` coercion pattern as PR-T | modified (~5 lines) |
| `tests/test_pr_u_1_homer_manifests.py` | Invariant tests: kustomize compile-clean, kustomization.yaml registration, namespace `infrastructure`, internal LB annotation, port matrix, placeholder presence | new |
| `tests/test_pr_t1_lb_services_tcp_udp.py` | Add `test_heplify_uses_udp_suffix`; bump `test_five_services` → `test_six_services`; bump literal expected-set in `test_output_keys_unchanged` to include `heplify_lb_ip`; bump `test_namespaces_unchanged` from `infrastructure: 2, voip: 3` → `infrastructure: 3, voip: 3` | modified |
| `tests/test_pr_t_ansible_k8s_lb_flat_vars.py` | Bump literal expected-set in `TestKeyContractMatchesK8sLBServices.test_lb_services_has_expected_keys` to include `heplify_lb_ip`; bump `test_all_five_keys_present_together` literal count check to `test_all_six_keys_present_together`; add parametrized `heplify_lb_ip="10.99.0.99"` case | modified |
| `tests/test_pr_r_pipeline_reorder.py` | Bump `test_timeout_with_nothing_returns_empty_dict_and_5_warnings` warn.call_count assertion from `== 5` to `== 6`; rename test method correspondingly | modified |

The `infrastructure` Namespace is already declared in top-level `k8s/namespaces.yaml`; this PR does NOT add a per-component `namespace.yml` (diverges from production's `voip-homer/k8s/namespace.yml` which is standalone). Mirrors the existing pattern of `k8s/infrastructure/{redis,rabbitmq,clickhouse}/` which also rely on the top-level Namespace declaration.

## 5. Producer→consumer trace

| Producer change | Consumer file | Consumer read path | Verification at design-time |
|---|---|---|---|
| `_LB_SERVICES` += heplify-udp tuple | `scripts/pipeline.py:_run_reconcile_k8s_outputs` → `scripts/k8s.py:harvest_loadbalancer_ips()` | iterates `_LB_SERVICES`, polls kubectl for each | grep `_LB_SERVICES` in pipeline.py: 1 hit (function call), confirmed |
| `state["k8s_outputs"]["heplify_lb_ip"]` persisted | `scripts/pipeline.py:run_pipeline` L390-397 PR-R hydration | reads `state["k8s_outputs"]` into `tf_outputs.setdefault()` | confirmed in pipeline.py |
| `tf_outputs["heplify_lb_ip"]` | `scripts/ansible_runner.py:_write_extra_vars` | new line `ansible_vars["heplify_lb_ip"] = terraform_outputs.get("heplify_lb_ip", "") or ""` | new in this PR |
| `ansible_vars["heplify_lb_ip"]` JSON top-level | ansible role consumes via Jinja2 `{{ heplify_lb_ip }}` | **NOT in PR-U-1 scope. PR-U-3 adds `HOMER_URI=udp:{{ heplify_lb_ip }}:9060` to env.j2** | grep `heplify_lb_ip` in `ansible/`: 0 hits today (expected; PR-U-3 adds it) |
| Pod env vars use inline `value: PLACEHOLDER_HOMER_DB_*` form (NOT `valueFrom.secretKeyRef`) | `scripts/k8s.py:_build_substitution_map()` (modified by PR-U-2, not this PR) | text-substitutes the placeholder tokens in rendered manifests at `kubectl apply` time | grep `PLACEHOLDER_HOMER_DB_` in scripts/: 0 hits today; PR-U-2 adds substitution entries |

Correction vs v1: the §5 row previously claimed `valueFrom.secretKeyRef` wiring; the actual installer uses inline `value:` strings substituted at apply-time (matches `_build_substitution_map()` semantics). The `valueFrom.secretKeyRef` form is reserved for credentials sourced from a `voipbin-secret` k8s Secret (PR-U-2 may choose either; deferred decision).

## 6. Exact manifests (per-file)

### 6.1 `k8s/infrastructure/homer/deployment.yaml`

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: heplify-deployment
  namespace: infrastructure
  labels:
    app: heplify-server
spec:
  replicas: 1
  strategy:
    type: Recreate
  selector:
    matchLabels:
      app: heplify-server
  template:
    metadata:
      labels:
        app: heplify-server
    spec:
      containers:
        - name: heplify
          image: heplify-server  # kustomize override → sipcapture/heplify-server:1.30
          command:
            - "./heplify-server"
          env:
            # PLACEHOLDER_CLOUDSQL_POSTGRES_PRIVATE_IP is substituted at apply-time
            # by scripts/k8s.py:_build_substitution_map(); see PR-D1 for the source
            # Terraform output. Heplify wants <host>:<port>, so we append :5432
            # literally in this manifest (Postgres default port is fixed).
            - name: HEPLIFYSERVER_DBADDR
              value: PLACEHOLDER_CLOUDSQL_POSTGRES_PRIVATE_IP:5432
            - name: HEPLIFYSERVER_DBDRIVER
              value: postgres
            # NOTE: HEPLIFYSERVER_DBSHEMA misspelling is INTENTIONAL — this is
            # the actual env var name the upstream heplify-server binary reads
            # (https://github.com/sipcapture/heplify-server). Do not "fix" to
            # DBSCHEMA; the binary will not see it.
            - name: HEPLIFYSERVER_DBSHEMA
              value: homer7
            - name: HEPLIFYSERVER_DBDROPDAYS
              value: "7"
            # PR-U-2 will switch these two to valueFrom.secretKeyRef sourced
            # from voipbin-secret. For PR-U-1 we leave inline placeholders so
            # the manifest is syntactically valid; substitution map fills in
            # the actual values in PR-U-2.
            - name: HEPLIFYSERVER_DBUSER
              value: PLACEHOLDER_HOMER_DB_USER
            - name: HEPLIFYSERVER_DBPASS
              value: PLACEHOLDER_HOMER_DB_PASS
            - name: HEPLIFYSERVER_DBDATATABLE
              value: homer_data
            - name: HEPLIFYSERVER_DBCONFTABLE
              value: homer_config
            - name: HEPLIFYSERVER_ESADDR
            - name: HEPLIFYSERVER_CONFIGHTTPADDR
              value: 0.0.0.0:9090
            - name: HEPLIFYSERVER_HEPADDR
              value: 0.0.0.0:9060
            - name: HEPLIFYSERVER_HEPTCPADDR
              value: 0.0.0.0:9060
            - name: HEPLIFYSERVER_HEPTLSADDR
              value: 0.0.0.0:9061
            - name: HEPLIFYSERVER_LOGLVL
              value: error
            - name: HEPLIFYSERVER_LOGSTD
              value: "true"
            - name: HEPLIFYSERVER_PROMADDR
              value: 0.0.0.0:9096
            - name: HEPLIFYSERVER_PROMTARGETIP
            - name: HEPLIFYSERVER_PROMTARGETNAME
          ports:
            - containerPort: 9060
              protocol: TCP
            - containerPort: 9060
              protocol: UDP
            - containerPort: 9061
              protocol: TCP
            - containerPort: 9090
              protocol: TCP
            - containerPort: 9096
              protocol: TCP
          resources:
            requests:
              cpu: "5m"
              memory: "12M"
            limits:
              cpu: "50m"
              memory: "128M"

        - name: homer-webapp
          image: homer-webapp  # kustomize override → pchero/homer-app:0.0.4
          env:
            - name: DB_HOST
              value: PLACEHOLDER_CLOUDSQL_POSTGRES_PRIVATE_IP
            - name: DB_USER
              value: PLACEHOLDER_HOMER_DB_USER
            - name: DB_PASS
              value: PLACEHOLDER_HOMER_DB_PASS
            - name: HOMER_ENABLE_API
              value: "true"
            - name: HOMER_LOGLEVEL
              value: debug
          ports:
            - containerPort: 80
          resources:
            requests:
              cpu: "4m"
              memory: "12M"
            limits:
              cpu: "40m"
              memory: "128M"
```

Notes vs production:
- `selector.matchLabels.app: heplify-server` (production uses kustomize `labels:` override to `heplify`). Installer follows other infrastructure components (`redis`, `rabbitmq`, `clickhouse`) which use a single per-app label without override. This avoids the kustomize-label-override footgun. Functionally equivalent — Service selectors and Deployment template labels match within the manifest.
- Token reuse: `PLACEHOLDER_CLOUDSQL_POSTGRES_PRIVATE_IP` is the existing token from the install repo (see `scripts/secret_schema.py` / `scripts/k8s.py:_build_substitution_map()`). Reusing it instead of inventing `PLACEHOLDER_HOMER_DB_ADDR` means PR-U-2 only needs to add `PLACEHOLDER_HOMER_DB_USER` and `PLACEHOLDER_HOMER_DB_PASS` (two new tokens) instead of three.
- `:5432` is appended literally to `HEPLIFYSERVER_DBADDR` because the port is a Postgres default; the substitution map doesn't need to know about it.
- `HEPLIFYSERVER_DBSHEMA` typo intentional, see inline comment.
- 2 new placeholder tokens (`PLACEHOLDER_HOMER_DB_USER`, `PLACEHOLDER_HOMER_DB_PASS`). These get added to the substitution map by PR-U-2. Until then, heplify-server fails to connect to Postgres (intentional mid-state, §8).
- `HEPLIFYSERVER_DBDATATABLE=homer_data` / `DBCONFTABLE=homer_config` added explicitly (production had defaults; explicit is safer for fresh installs).

### 6.2 `k8s/infrastructure/homer/service.yaml`

```yaml
apiVersion: v1
kind: Service
metadata:
  name: heplify-tcp
  namespace: infrastructure
  labels:
    app: heplify-server
  annotations:
    cloud.google.com/load-balancer-type: "Internal"
spec:
  type: LoadBalancer
  selector:
    app: heplify-server
  ports:
    - name: hep-server-tcp
      port: 9060
      targetPort: 9060
      protocol: TCP
    - name: hep-server-tls
      port: 9061
      targetPort: 9061
      protocol: TCP
    - name: hep-server-config
      port: 9090
      targetPort: 9090
      protocol: TCP
    - name: hep-promport
      port: 9096
      targetPort: 9096
      protocol: TCP
    - name: homer
      port: 80
      targetPort: 80
      protocol: TCP

---
apiVersion: v1
kind: Service
metadata:
  name: heplify-udp
  namespace: infrastructure
  labels:
    app: heplify-server
  annotations:
    cloud.google.com/load-balancer-type: "Internal"
spec:
  type: LoadBalancer
  selector:
    app: heplify-server
  ports:
    - name: hep-server-udp
      port: 9060
      targetPort: 9060
      protocol: UDP
```

Notes vs production:
- Production hard-pins `loadBalancerIP: <prod-internal-LB-IP-tcp>` / `<prod-internal-LB-IP-udp>`. Installer drops this; GCP allocates dynamically. The IP is then harvested by `reconcile_k8s_outputs` and used by PR-U-3.
- Selector `app: heplify-server` (matches Deployment Pod labels). Production uses `app: heplify` via kustomize override; rationale in §6.1.

### 6.3 `k8s/infrastructure/homer/kustomization.yaml`

```yaml
apiVersion: kustomize.config.k8s.io/v1beta1
kind: Kustomization

resources:
  - deployment.yaml
  - service.yaml

images:
  - name: heplify-server
    newName: sipcapture/heplify-server
    newTag: "1.30"
  - name: homer-webapp
    newName: pchero/homer-app
    newTag: "0.0.4"
```

Notes:
- The kustomize-level `namespace:` directive is intentionally omitted to mirror the sibling pattern (`k8s/infrastructure/{redis,rabbitmq,clickhouse}/kustomization.yaml`), which rely solely on the per-resource `metadata.namespace: infrastructure` field declared inside `deployment.yaml` / `service.yaml`. The `infrastructure` Namespace itself is declared in top-level `k8s/namespaces.yaml`; this file never re-declares it.

### 6.4 `k8s/infrastructure/kustomization.yaml` (modified)

```diff
 resources:
   - redis
   - rabbitmq
   - clickhouse
+  - homer
```

### 6.5 `scripts/k8s.py` (modified)

```diff
 _LB_SERVICES: list[tuple[str, str, str]] = [
     ("infrastructure", "redis", "redis_lb_ip"),
     ("infrastructure", "rabbitmq", "rabbitmq_lb_ip"),
+    ("infrastructure", "heplify-udp", "heplify_lb_ip"),
     ("voip", "asterisk-call-udp", "asterisk_call_lb_ip"),
     ("voip", "asterisk-registrar-udp", "asterisk_registrar_lb_ip"),
     ("voip", "asterisk-conference-udp", "asterisk_conference_lb_ip"),
 ]
```

Rationale for UDP harvest target (not TCP): Kamailio's heplify-client sidecar (PR-U-3) sends HEP packets over UDP to port 9060. The TCP Service exists for homer-webapp UI (port 80) plus heplify-server config/TLS/Prometheus ports, none of which Kamailio talks to. Mirrors `asterisk-*-udp` pattern from PR-T1.

### 6.6 `scripts/ansible_runner.py` (modified)

```diff
     ansible_vars["asterisk_conference_lb_ip"] = (
         terraform_outputs.get("asterisk_conference_lb_ip", "") or ""
     )
+    # PR-U-1: heplify-server LoadBalancer IP for Kamailio HOMER_URI wiring
+    # (consumed by env.j2 in PR-U-3, currently no-op until that PR lands).
+    ansible_vars["heplify_lb_ip"] = (
+        terraform_outputs.get("heplify_lb_ip", "") or ""
+    )
     ansible_vars["rtpengine_socks"] = _build_rtpengine_socks(terraform_outputs)
```

## 7. Tests

### 7.1 `tests/test_pr_u_1_homer_manifests.py` (new)

Test classes:

| Class | Test count | Asserts |
|---|---|---|
| `TestHomerKustomizationStructure` | 4 | (a) `k8s/infrastructure/homer/kustomization.yaml` exists; (b) lists `deployment.yaml` + `service.yaml` as resources; (c) sets `namespace: infrastructure`; (d) pins `sipcapture/heplify-server:1.30` and `pchero/homer-app:0.0.4` |
| `TestHomerRegisteredInInfrastructureKustomization` | 1 | `k8s/infrastructure/kustomization.yaml` resources list contains `homer` |
| `TestHomerNamespaceIsInfrastructure` | 2 | (a) Deployment metadata.namespace = `infrastructure`; (b) Services x2 namespace = `infrastructure` |
| `TestHeplifyServicesInternalLB` | 4 | (a) heplify-tcp annotation `cloud.google.com/load-balancer-type=Internal`; (b) heplify-udp same; (c) heplify-tcp ports cover {9060, 9061, 9090, 9096, 80}; (d) heplify-udp ports cover {9060} UDP only |
| `TestHeplifyPodSelectorMatchesDeploymentLabels` | 2 | (a) heplify-tcp.spec.selector matches Deployment template labels; (b) heplify-udp same |
| `TestKustomizeBuildCompiles` | 1 | `kubectl kustomize k8s/infrastructure/homer/` returns 0 (or fall back to PyYAML parse if kubectl not in PATH) |
| `TestPlaceholderTokensPresent` | 1 | Deployment contains `PLACEHOLDER_CLOUDSQL_POSTGRES_PRIVATE_IP` x2 + `PLACEHOLDER_HOMER_DB_USER` x2 + `PLACEHOLDER_HOMER_DB_PASS` x2 (sentinels for PR-U-2 to substitute) |

Total: **15 tests**.

### 7.2 `tests/test_pr_t1_lb_services_tcp_udp.py` (modified)

Add a new test:

```python
def test_heplify_uses_udp_suffix(self):
    services = {svc for (_ns, svc, _key) in _LB_SERVICES}
    assert "heplify-udp" in services
    assert "heplify" not in services  # ambiguous; must specify -udp
```

Three literal-count bumps:
- `test_five_services` → `test_six_services`. Assert `len(_LB_SERVICES) == 6`.
- `test_namespaces_unchanged`: `{"infrastructure": 2, "voip": 3}` → `{"infrastructure": 3, "voip": 3}`.
- `test_output_keys_unchanged`: add `heplify_lb_ip` to the expected literal set (6 keys total).

### 7.3 `tests/test_pr_t_ansible_k8s_lb_flat_vars.py` (modified)

Two literal-count bumps:
- `TestKeyContractMatchesK8sLBServices.test_lb_services_has_expected_keys`: add `heplify_lb_ip` to the expected literal set (6 keys total).
- `TestK8sLbIpsLandAtTopLevel.test_all_five_keys_present_together` → `test_all_six_keys_present_together`. Add `heplify_lb_ip` to the outputs dict and assertions.

Parametrized addition: `("heplify_lb_ip", "10.99.0.99")` to `test_individual_key_lands_at_top_level`.

### 7.4 `tests/test_pr_r_pipeline_reorder.py` (modified)

One literal-count bump:
- `test_timeout_with_nothing_returns_empty_dict_and_5_warnings` → `test_timeout_with_nothing_returns_empty_dict_and_6_warnings`. Bump `assert warn.call_count == 5` → `== 6`.

## 8. Expected mid-state (intentional inconsistency)

After this PR merges and `voipbin-install apply` runs:

| Resource | Expected state | Reason |
|---|---|---|
| heplify-deployment Pod | `CrashLoopBackOff` (DB connection failure) | `PLACEHOLDER_HOMER_DB_*` tokens are still literal strings in the rendered manifest (PR-U-2 adds the substitution map entries). Even with placeholders substituted, the `infrastructure` namespace's `default-deny-all` Egress NetworkPolicy blocks TCP/5432 to CloudSQL Postgres — PR-U-2 must also add the egress allow-rule |
| heplify-tcp / heplify-udp Services | Created, externalIP allocated | k8s allocates LB IP regardless of Pod readiness |
| `state.yaml.k8s_outputs.heplify_lb_ip` | Set to allocated UDP LB IP | reconcile_k8s_outputs harvests Service externalIP, not Pod readiness |
| `tf_outputs.heplify_lb_ip` flat-var | Available to ansible role | ansible role doesn't consume yet (PR-U-3); silent until then |
| Kamailio container | Still CrashLoops with `HOMER_URI required` | env.j2 not modified in this PR; PR-U-3 wires it |

The user explicitly authorized this mid-state in conversation today (PR split decision: U-1 manifests → U-2 DB+NetworkPolicy → U-3 Kamailio integration). Reviewers should NOT flag heplify-deployment CrashLoop as a blocker.

## 9. Verification plan

Pre-commit checks:

1. `python -m pytest tests/ -q` → all green (716 existing + ~15 new + literal-count bumps in 3 existing test files all consistent)
2. `kubectl kustomize k8s/infrastructure/homer/ > /dev/null` → no errors (CI image has kubectl; local fallback path uses PyYAML parse)
3. `kubectl kustomize k8s/ > /dev/null` → top-level still compiles
4. `python -c "from scripts.k8s import _LB_SERVICES; assert ('infrastructure', 'heplify-udp', 'heplify_lb_ip') in _LB_SERVICES"` → no AssertionError
5. `grep -rn "heplify_lb_ip" scripts/` → 2 hits (k8s.py + ansible_runner.py)
6. `grep -rn "PLACEHOLDER_HOMER" k8s/` → 4 hits (deployment.yaml: USER x2, PASS x2), 0 hits elsewhere
7. `grep -rn "PLACEHOLDER_CLOUDSQL_POSTGRES_PRIVATE_IP" k8s/` → existing hits + 2 new (heplify DBADDR + homer-webapp DB_HOST)
8. `bash scripts/dev/check-plan-sensitive.sh docs/plans/2026-05-13-pr-u-1-homer-k8s-manifests-design.md` → no sensitive data leak

Post-merge dogfood (manual, NOT part of CI):

1. `voipbin-install apply --auto-approve` runs through `reconcile_k8s_outputs` stage
2. `kubectl get svc -n infrastructure heplify-udp -o json | jq .status.loadBalancer.ingress[0].ip` returns non-empty
3. `grep heplify_lb_ip ~/gitvoipbin/install/.voipbin-state.yaml` shows the harvested IP
4. heplify-deployment Pod is in CrashLoopBackOff (expected, see §8). PR-U-2 will fix.

## 10. Rollout / risk

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| heplify Pod CrashLoop alarms operator | Expected | Low (cosmetic in `kubectl get pods` listing) | Documented in §8. README/operator note in PR-U-2 once Postgres is wired |
| `homer` resource missing from `k8s/infrastructure/kustomization.yaml` | Low | High (entire homer namespace silently skipped at apply) | Test `TestHomerRegisteredInInfrastructureKustomization` catches |
| heplify-udp LB IP not allocated → harvest timeout 300s | Low | Medium (apply hangs 5 min then fails) | Same GCP allocation path as redis/rabbitmq/asterisk-* — proven to work in PR-T2 dogfood |
| Production has `loadBalancerIP: <prod-internal-LB-IP>` hard-pinned, installer drops it | Expected | None (dynamic allocation is desired in installer) | Documented in §6.2 notes |
| Image pull from `pchero/homer-app:0.0.4` (Docker Hub personal account) | Low | Medium (CrashLoop if registry down or account deleted) | Same image production uses; standard Docker Hub availability. **Tracked deferral: mirror to project's Artifact Registry — see §11** |
| Kustomize `labels` override vs explicit `app: heplify-server` | Low | Low | Installer chooses explicit; safer for future selectors. Documented in §6.1 |
| `HEPLIFYSERVER_DBSHEMA` typo gets "corrected" by a future contributor to `DBSCHEMA` | Low | High (heplify silently fails to read schema → migration never runs) | Inline comment in §6.1; flagged in code review checklist |
| heplify-server 1.30 first-boot schema bootstrap fails (no upstream verification) | **Unverified** | High (PR-U-2 acceptance gate; heplify Ready never reached) | PR-U-2 acceptance test runs `kubectl exec` → `psql ... -c "\dt"` to confirm tables exist. Fallback: run upstream `homer-app/scripts/migration/postgres/*.sql` manually |

## 11. Out of scope (deferred to PR-U-2 / U-3 / future)

**PR-U-2 (Postgres + heplify substitution + NetworkPolicy):**
- Postgres `homer_data` / `homer_config` databases on the existing CloudSQL Postgres instance
- heplify-server env var substitution map entries for `PLACEHOLDER_HOMER_DB_USER` and `PLACEHOLDER_HOMER_DB_PASS` (sourced from `voipbin` user credentials)
- heplify-server first-boot schema bootstrap verification + fallback manual migration plan
- **NetworkPolicy egress allow-rule** from `infrastructure` namespace heplify Pods to `PLACEHOLDER_CLOUDSQL_POSTGRES_PRIVATE_IP_CIDR` on TCP/5432 (today the namespace is `default-deny-all` Egress + DNS-only allow)

**PR-U-3 (Kamailio integration):**
- Kamailio `heplify-client` docker-compose sidecar (mirror `monorepo-voip/voip-kamailio-ansible` pattern)
- env.j2 `HOMER_URI=udp:{{ heplify_lb_ip }}:9060` wiring
- env.j2 `homer_enabled=true` flip
- **GCP firewall rule** allowing Kamailio VM source range → heplify-udp Internal LB on UDP/9060 (verify if existing GKE node-tag firewall covers this or if a new rule is needed)
- **NetworkPolicy ingress allow-rule** in `infrastructure` namespace for heplify Pods accepting UDP/9060 from `voip` namespace (today the namespace has `default-deny-all` Ingress)
- Kamailio container HEALTHY validation (PR-U-3 acceptance)

**Future / not committed:**
- homer-webapp UI access (manual via `kubectl port-forward`, no install repo work)
- homer-webapp API token / user object configuration (manual operator step, documented in PR-U-2)
- **Mirror `pchero/homer-app:0.0.4` to project Artifact Registry** (supply chain hardening; ETA: post-GA)

## 12. Open questions (for reviewer)

1. Should the `homer` Kustomize directory live under `k8s/infrastructure/homer/` or `k8s/observability/homer/`? Production uses `infrastructure` namespace; installer's other observability components (none yet) might warrant a separate dir. Recommend stay in `infrastructure` for namespace parity.

2. Should heplify-server replicas be `1` (production) or `2` (HA)? heplify-server's UDP packet capture is per-Pod (no built-in fan-out); 2 replicas would only get half the traffic each behind a UDP LB hash. Recommend stay at `1`.

3. heplify-server `1.30` first-boot schema bootstrap behavior is unverified against upstream. If the binary does NOT auto-migrate, PR-U-2 needs an init container or Job to run `homer-app/scripts/migration/postgres/*.sql` against the `homer_data`/`homer_config` DBs. Flagging now so PR-U-2 designer knows.

## 13. Iter-1 review response summary (2026-05-13)

10 actionable findings from iter 1, all REAL. Applied:

| # | Iter-1 finding | §addressed | Resolution |
|---|---|---|---|
| 1 | `test_pr_r_pipeline_reorder.py` warn.call_count == 5 also hard-coded | §4 (Affected files), §7.4 | Added file to Affected, added §7.4 with the bump |
| 2 | Make literal expected-set bump explicit for both test_pr_t1 + test_pr_t_ansible | §7.2, §7.3 | Rewrote bullets to spell out each literal-set bump |
| 3 | Missing NetworkPolicy egress deferral for infrastructure → Postgres | §11 (PR-U-2), §8, §2 goal 5 | Added explicit deferral entry; mentioned in §2 goal 5 and §8 table row 1 reason |
| 4 | Missing GCP firewall rule deferral for Kamailio → heplify-udp UDP/9060 | §11 (PR-U-3) | Added explicit deferral entry; also flagged NetworkPolicy ingress allow-rule for the same path |
| 5 | Reuse existing PLACEHOLDER_CLOUDSQL_POSTGRES_PRIVATE_IP instead of new HOMER_DB_ADDR | §6.1, §5, §11, §9 verification | Rewrote deployment.yaml env block. heplify-server DBADDR uses existing token + literal `:5432`; homer-webapp DB_HOST uses existing token directly. Net new tokens: 2 (USER + PASS) instead of 4 |
| 6 | Unverified claim about heplify-server first-boot schema migration | §3 row 3, §10 risk, §11 (PR-U-2), §12 Q3 | Marked UNVERIFIED in §3; risk table row added; PR-U-2 deferral spells out fallback (run upstream migration SQL); §12 Q3 flags for PR-U-2 designer |
| 7 | §5 inconsistency: claimed valueFrom.secretKeyRef but §6.1 used inline value | §5 (last row + correction note) | Added a §5 row documenting inline `value:` form is what's used; correction note added below the table |
| 8 | Confirm kustomize `namespace:` doesn't recreate Namespace | §4 (table row for homer/kustomization.yaml), §6.3 (comment) | Spelled out in §4 and added inline comment in §6.3 yaml |
| 9 | Commit to inline comment about DBSHEMA typo | §6.1 (yaml has inline `# NOTE: ... INTENTIONAL` block) | Comment added in §6.1 |
| 10 | `pchero/homer-app:0.0.4` supply chain — track Artifact Registry mirror | §10 risk row, §11 (Future / not committed section) | Risk row reframed; explicit deferral added |

## 14. Iter-2 review response summary (2026-05-13)

2 REAL findings from iter 2, both addressed:

| # | Iter-2 finding | §addressed | Resolution |
|---|---|---|---|
| 1 | Live production IP literal `<prod-internal-LB-IP>` in §6.2/§10 fails `check-plan-sensitive.sh` | §6.2, §10 | Replaced with redacted `<prod-internal-LB-IP>` form |
| 2 | Kustomize `namespace:` directive divergence from sibling pattern | §4, §6.3 | Dropped the directive; mirrors redis/rabbitmq/clickhouse pattern; documented in §6.3 Notes |

iter-2 confirmed via spot-check that iter-1 findings #1, #5, #8, #9 were correctly applied. No iter-1 regression detected.

Convergence signal: iter-2 produced 2 REAL findings (both mechanical/cosmetic, not architecture). iter-3 will verify the two patches landed cleanly.

## 15. Approval status

Draft v3 (iter 1 + 2 findings applied) → Iter-3 verification (light) → APPROVED → Implementation
