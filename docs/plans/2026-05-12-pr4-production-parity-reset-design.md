# PR #4 — Production parity reset (secret schema + env wiring + frontend TLS rollback)

**Date:** 2026-05-12
**Author:** Hermes (CPO)
**Status:** Design (iter 1 → iter 2; supersedes earlier #4a split)
**Repo:** `voipbin/install`
**Branch:** `NOJIRA-PR4-production-parity-reset`
**Parent:** PR #3 series (`#9/#10/#11/#12/#13`) merged on `main`.
**Roadmap slot:** revised PR #4 — combined `#4a` (secret schema) +
`#4b` (frontend TLS rollback) into a single PR after iter 1 design
review flagged a cert regression window if the two halves shipped
separately. `#4c` (cloudsql-proxy removal + DSN direct) and `#4d`
(verify + README) follow as separate PRs.

## 1. Context and motivation

PR #3 series (#11/#12/#13, merged 2026-05-12) was designed under the
assumption that the install repo would terminate TLS in-cluster
(backend Pod env-var, frontend nginx-tls sidecar) using cert pairs
bootstrapped by `tls_bootstrap.py`. Production walk-through on
2026-05-12 disproved both halves of that assumption.

Production reality (inspected 2026-05-12 on the canonical GKE cluster):

- Single `Secret/voipbin` in `bin-manager` ns with **53 keys** (mix of
  real secrets, DSNs, base64 TLS PEM blobs, and config-like values).
- **No** `voipbin-config` ConfigMap. All config lives in `Secret/voipbin`.
- All 31 `bin-*` Deployments wire env-vars **explicitly** with
  `valueFrom.secretKeyRef`, NOT via `envFrom`.
- Several Pod env-var names are **renamed** from Secret keys (e.g. Pod
  env `DATABASE_DSN` ← Secret key `DATABASE_DSN_BIN`, Pod env
  `SSL_CERT_BASE64` ← Secret key `SSL_CERT_API_BASE64`).
- `registrar-manager` is the **only exception** to the DSN rename
  pattern: it uses Pod env `DATABASE_DSN_BIN` and `DATABASE_DSN_ASTERISK`
  directly (no rename).
- `Service/api-manager` exposes **port 443 only**. Pod also listens on
  9000 (audiosocket), but **only Pod-IP direct connections** use that
  port — no Service noisily fronts it (confirmed by Asterisk → Pod IP
  TCP probe).
- `Service/hook-manager` exposes **both 80 and 443**.
- frontend Deployments (`admin`, `talk`, `meet` in `square-manager` ns)
  listen on **port 80 only**. `Service` maps 443 → targetPort 80
  (cleartext to Pod). **TLS termination happens at Cloudflare**, not in
  the cluster.
- Plain-literal `:***@` in `RABBITMQ_ADDRESS` is the production pattern
  (RabbitMQ runs `guest`/`guest`; the `***` in the secret value is
  literal, not a substitution placeholder).

Consequence: an install run from PR #13 main today would:
1. Deploy 31 backend Pods that CrashLoopBackOff because expected env-vars
   (`DATABASE_DSN`, `JWT_KEY`, `SSL_CERT_BASE64`, etc.) are unset.
2. Frontend Pods would launch correctly but the nginx-tls sidecar
   (PR #3b) would compete with Cloudflare's TLS termination, doubling
   complexity for self-hosters and diverging from production.

PR #4's job is to converge the install repo with production exactly.
No new features, no behaviour changes beyond moving from "doesn't
match prod" to "matches prod".

## 2. Scope

In scope (combined #4a + #4b):

**Backend schema and env wiring (was #4a):**
- Replace `Secret/voipbin-secret` with `Secret/voipbin` (53 keys).
- Delete `ConfigMap/voipbin-config` and all references.
- Rewrite all 31 `k8s/backend/services/*.yaml` Deployment specs:
  - remove `envFrom`
  - add explicit `env:` block with `valueFrom.secretKeyRef` per
    production wiring (see appendix `pr4a-env-wiring.md`)
  - preserve container ports, probes, resources, prom annotations
- Honour the `registrar-manager` exception (DSN keys not renamed).
- Honour every literal env-var (e.g. `PROMETHEUS_ENDPOINT=/metrics`).
- Honour every fieldRef env-var (POD_NAME, POD_NAMESPACE, POD_IP).
- Template any production literal that contains the brand domain — substitute `PLACEHOLDER_DOMAIN`.
- Add port 80 to `hook-manager` Deployment + Service +
  `allow-ingress-to-hook` NetworkPolicy.
- Delete `Service/api-manager-internal` (PR #3b artifact, not in
  production). Pod containerPort 9000 stays for Pod-IP direct dial.
- Migration Job: rewire `envFrom` → explicit `env:` block.
- Update `scripts/k8s.py` substitution map for 53 keys.
- Update `scripts/secretmgr.py` allowed-key set.
- Update `tests/test_k8s.py` + add new schema/rename assertion tests.

**Frontend TLS rollback (was #4b):**
- Delete nginx-tls sidecar from `admin`/`talk`/`meet` Deployments.
- Delete `frontend/tls-proxy-configmap.yaml`.
- Change frontend Service port mapping: 443 → targetPort **80**
  (was targetPort 443 to sidecar).
- Delete `voipbin-tls` Secret bootstrap path in both namespaces.
- Rewrite `scripts/tls_bootstrap.py`:
  - Stop emitting `Secret/voipbin-tls` resources.
  - Start populating four `SSL_*_BASE64` keys inside
    `Secret/voipbin` (api + hook cert pairs) at first
    `voipbin-install init`.
  - Idempotent — never overwrite existing values.
- `square-manager` namespace stays (frontend separation is still
  useful; isolation, future per-app services).
- README + architecture: add "Cloudflare front-of-cluster TLS
  termination is assumed; cluster Services for admin/talk/meet do not
  serve HTTPS in-Pod" note. Full README rewrite deferred to #4d.

Out of scope (subsequent PRs):

- **PR #5**: cloudsql-proxy removal + Cloud SQL private-IP direct DSN
  (production uses a direct private-IP endpoint on port 3306).
- **PR #4d**: `verify --check=secret_schema` (53-key check),
  `verify --check=tls_cert_is_production` redefinition for new
  in-Secret base64 model, full README + architecture rewrite.

## 3. Production secret inventory (source of truth)

The following 53 keys exist in `Secret/voipbin` in production. This is
the canonical key set that the install repo must emit.

### 3.1 Categories

| Category | Count | Notes |
|---|---|---|
| Real secrets (API tokens, passwords) | 22 | `JWT_KEY`, `OPENAI_API_KEY`, etc. |
| DSNs | 3 | `DATABASE_DSN_BIN`, `DATABASE_DSN_ASTERISK`, `DATABASE_DSN_POSTGRES` |
| TLS PEM blobs (base64) | 4 | `SSL_CERT_{API,HOOK}_BASE64`, `SSL_PRIVKEY_{API,HOOK}_BASE64` |
| Config-like (non-secret in nature) | 24 | host addresses, project ids, paddle price ids, etc. |
| **Total** | **53** | |

All 53 live in the single `Secret/voipbin`. Consolidation matches
production exactly and avoids drift between two manifests.

### 3.2 Full key inventory with default values

Notation: `dummy:<form>` indicates the install repo's default seed
value. Operators replace via `secrets.yaml` (sops-encrypted).
"Sensitive" column uses one vocabulary: `secret` (high-impact),
`config` (operational config, low-impact), `dsn` (DB connection string
with embedded password), `tls` (init-generated cert material).

| Key | Default | Class | Comment |
|---|---|---|---|
| AUTHTOKEN_MESSAGEBIRD | `dummy-messagebird-token` | secret | optional unless using MessageBird |
| AUTHTOKEN_TELNYX | `dummy-telnyx-authtoken` | secret | distinct from `TELNYX_TOKEN`; **not referenced by any current Pod env** — kept for prod parity |
| AWS_ACCESS_KEY | `dummy-aws-access-key` | secret | for AWS S3 / Polly |
| AWS_SECRET_KEY | `dummy-aws-secret-key` | secret | |
| CARTESIA_API_KEY | `dummy-cartesia-key` | secret | TTS provider |
| CLICKHOUSE_ADDRESS | `clickhouse.infrastructure.svc.cluster.local:9000` | config | native protocol |
| CLICKHOUSE_DATABASE | `default` | config | |
| DATABASE_DSN_ASTERISK | `asterisk:password@tcp(cloudsql-proxy.infrastructure.svc.cluster.local:3306)/asterisk` | dsn | Go MySQL DSN; PR #5 will replace host with private IP |
| DATABASE_DSN_BIN | `bin-manager:password@tcp(cloudsql-proxy.infrastructure.svc.cluster.local:3306)/bin_manager` | dsn | |
| DATABASE_DSN_POSTGRES | `postgres://bin-manager:dummy-password@cloudsql-proxy-postgres.infrastructure.svc.cluster.local:5432/bin_manager?sslmode=disable` | dsn | reserved for postgres-backed services; **not referenced by any current Pod env** |
| DEEPGRAM_API_KEY | `dummy-deepgram-key` | secret | STT provider |
| DOMAIN_NAME_EXTENSION | `registrar.PLACEHOLDER_DOMAIN` | config | Asterisk registrar host |
| DOMAIN_NAME_TRUNK | `trunk.PLACEHOLDER_DOMAIN` | config | Asterisk trunk host |
| ELEVENLABS_API_KEY | `dummy-elevenlabs-key` | secret | TTS provider |
| ENGINE_KEY_CHATGPT | `dummy-openai-engine-key` | secret | **not referenced by any current Pod env as Secret key** (ai-manager's Pod env `ENGINE_KEY_CHATGPT` is sourced from Secret key `OPENAI_API_KEY` via rename); kept for prod parity |
| EXTERNAL_SIP_GATEWAY_ADDRESSES | `""` (empty) | config | comma-separated provider IPs |
| GCP_BUCKET_NAME_MEDIA | `PLACEHOLDER_PROJECT_ID-voipbin-media` | config | bucket for media storage |
| GCP_BUCKET_NAME_TMP | `PLACEHOLDER_PROJECT_ID-voipbin-tmp` | config | temp uploads |
| GCP_PROJECT_ID | `PLACEHOLDER_PROJECT_ID` | config | |
| GCP_PROJECT_NAME | `PLACEHOLDER_PROJECT_ID` | config | |
| GCP_REGION | `PLACEHOLDER_REGION` | config | |
| GOOGLE_API_KEY | `dummy-google-api-key` | secret | |
| HOMER_API_ADDRESS | `http://homer.local` | config | SIP capture; null endpoint by default |
| HOMER_AUTH_TOKEN | `dummy-homer-token` | secret | |
| HOMER_WHITELIST | `""` (empty) | config | allowlist for HEP traffic |
| JWT_KEY | (random 64-char hex, generated at first `init`) | secret | **MUST be unique per install** |
| MAILGUN_API_KEY | `dummy-mailgun-key` | secret | email provider |
| OPENAI_API_KEY | `dummy-openai-key` | secret | |
| PADDLE_API_KEY | `dummy-paddle-key` | secret | billing provider (optional) |
| PADDLE_PRICE_ID_BASIC | `""` (empty) | config | |
| PADDLE_PRICE_ID_PROFESSIONAL | `""` (empty) | config | |
| PADDLE_WEBHOOK_SECRET_KEY | `dummy-paddle-webhook-secret` | secret | |
| PROJECT_BASE_DOMAIN | `PLACEHOLDER_DOMAIN` | config | |
| PROJECT_BUCKET_NAME | `PLACEHOLDER_PROJECT_ID-voipbin-media` | config | duplicate of GCP_BUCKET_NAME_MEDIA in production (intentional, kept for parity); **not referenced by any current Pod env as Secret key** (call-manager Pod env `PROJECT_BUCKET_NAME` sources from `GCP_BUCKET_NAME_MEDIA` via rename) |
| PROMETHEUS_ENDPOINT | `/metrics` | config | also set as literal in env; kept in Secret for back-compat |
| PROMETHEUS_LISTEN_ADDRESS | `:2112` | config | same |
| RABBITMQ_ADDRESS | `amqp://guest:***@rabbitmq.infrastructure.svc.cluster.local:5672` | config | DSN-convention; literal `***` is the actual production password value (RabbitMQ defaults to `guest`/`guest`), not a substitution placeholder |
| REDIS_ADDRESS | `redis.infrastructure.svc.cluster.local:6379` | config | |
| REDIS_DATABASE | `1` | config | |
| REDIS_PASSWORD | `""` (empty) | secret | production Redis runs unauthenticated |
| SENDGRID_API_KEY | `dummy-sendgrid-key` | secret | |
| SSL_CERT_API_BASE64 | (self-signed PEM, base64-encoded, generated at first `init`) | tls | api-manager Pod TLS cert |
| SSL_CERT_HOOK_BASE64 | (self-signed PEM, base64-encoded, generated at first `init`) | tls | hook-manager Pod TLS cert |
| SSL_PRIVKEY_API_BASE64 | (matching private key) | tls | |
| SSL_PRIVKEY_HOOK_BASE64 | (matching private key) | tls | |
| STREAMING_LISTEN_PORT | `8080` | config | also set as literal in transcribe-manager env; kept in Secret for parity |
| STT_PROVIDER_PRIORITY | `GCP,AWS` | config | same |
| TELNYX_CONNECTION_ID | `""` (empty) | config | |
| TELNYX_PROFILE_ID | `""` (empty) | config | |
| TELNYX_TOKEN | `dummy-telnyx-token` | secret | |
| TWILIO_SID | `dummy-twilio-sid` | secret | |
| TWILIO_TOKEN | `dummy-twilio-token` | secret | |
| XAI_API_KEY | `dummy-xai-key` | secret | |

### 3.3 Keys present in Secret but not consumed by any Pod env as Secret key (prod parity)

Eight keys are kept for production parity but no current bin-* Pod env
references them **as Secret keys** directly:

- `AUTHTOKEN_TELNYX` (message-manager Pod env `AUTHTOKEN_TELNYX` is
  sourced from Secret key `TELNYX_TOKEN` via rename)
- `DATABASE_DSN_POSTGRES` (reserved for future postgres-backed service;
  genuinely unreferenced today)
- `ENGINE_KEY_CHATGPT` (ai-manager Pod env `ENGINE_KEY_CHATGPT` is
  sourced from Secret key `OPENAI_API_KEY` via rename)
- `PROJECT_BUCKET_NAME` (call-manager Pod env `PROJECT_BUCKET_NAME` is
  sourced from Secret key `GCP_BUCKET_NAME_MEDIA` via rename)
- `PROMETHEUS_ENDPOINT`, `PROMETHEUS_LISTEN_ADDRESS`,
  `STREAMING_LISTEN_PORT`, `STT_PROVIDER_PRIORITY` (set as literal envs
  on Pods, not sourced from Secret)

Decision: **keep all** for production parity and to avoid surprising
operators who reference production examples. Audit / cleanup deferred
to a future PR if/when monorepo confirms they are truly unused.

### 3.4 Init-generated keys

Five keys are generated by `scripts/tls_bootstrap.py` (rewritten in
this PR) at first `voipbin-install init` and persisted into
`secrets.yaml` (sops-encrypted):

- `JWT_KEY` — 64-char random hex. Reuse existing logic.
- `SSL_CERT_API_BASE64` + `SSL_PRIVKEY_API_BASE64` — self-signed
  10-year RSA-2048 for `api.PROJECT_BASE_DOMAIN`. base64-encoded PEM.
- `SSL_CERT_HOOK_BASE64` + `SSL_PRIVKEY_HOOK_BASE64` — same, for
  `hook.PROJECT_BASE_DOMAIN`.

These five are written directly into the operator's `secrets.yaml`
(sops-encrypted), then surface as Secret keys via the existing sops
decrypt + substitution pipeline. The `Secret/voipbin-tls` resource and
nginx-tls sidecar (PR #3b) are deleted entirely.

## 4. Manifest changes

### 4.1 `k8s/backend/secret.yaml`

Rename Secret from `voipbin-secret` to `voipbin`. Replace stringData
with the full 53-key inventory from §3.2. Each value is a
`PLACEHOLDER_*` token resolved by `scripts/k8s.py` substitution at
render time.

### 4.2 `k8s/backend/configmap.yaml`

Delete this file. Remove from `k8s/backend/kustomization.yaml`.

### 4.3 `k8s/backend/services/*.yaml` (31 files)

For each Deployment, replace the `envFrom` block with an explicit
`env:` block that mirrors production wiring exactly. The complete
per-service mapping is in appendix `pr4a-env-wiring.md`.

**Critical pattern callouts** (the design example below is illustrative
only; the appendix is authoritative for each service):

1. **DSN rename**: every service except `registrar-manager` uses Pod
   env `DATABASE_DSN` ← Secret key `DATABASE_DSN_BIN`.
   `registrar-manager` uses Pod env `DATABASE_DSN_BIN` directly (no
   rename), AND additionally Pod env `DATABASE_DSN_ASTERISK` from
   Secret key `DATABASE_DSN_ASTERISK`.

2. **SSL rename**: `api-manager` uses Pod env `SSL_CERT_BASE64` ←
   Secret key `SSL_CERT_API_BASE64` (and matching privkey).
   `hook-manager` uses the same Pod env names but ← Secret key
   `SSL_CERT_HOOK_BASE64`. Pod env name is identical across services;
   Secret key differs.

3. **ENGINE_KEY_CHATGPT rename**: `ai-manager` uses Pod env
   `ENGINE_KEY_CHATGPT` ← Secret key `OPENAI_API_KEY`.

4. **PROJECT_BUCKET_NAME rename**: `call-manager` uses Pod env
   `PROJECT_BUCKET_NAME` ← Secret key `GCP_BUCKET_NAME_MEDIA`.

5. **AUTHTOKEN_TELNYX rename**: `message-manager` uses Pod env
   `AUTHTOKEN_TELNYX` ← Secret key `TELNYX_TOKEN`.

6. **GCP_BUCKET_NAME rename**: `api-manager` uses Pod env
   `GCP_BUCKET_NAME` ← Secret key `GCP_BUCKET_NAME_TMP`.

7. **Literal env-vars**: every bin-* Pod has at least
   `PROMETHEUS_ENDPOINT=/metrics` and `PROMETHEUS_LISTEN_ADDRESS=:2112`
   as literals. `transcribe-manager` additionally has
   `STREAMING_LISTEN_PORT=8080` and `STT_PROVIDER_PRIORITY=GCP,AWS` as
   literals.

8. **Templated literals**: production `call-manager` has a literal
   `PROJECT_BASE_DOMAIN=<brand-domain>`. **install repo must template this
   to `PLACEHOLDER_DOMAIN`.** Same audit applied to every literal value
   in every service: if it contains a brand identifier, template it.

9. **fieldRef**: `api-manager`, `transcribe-manager`, and any service
   needing self-aware addressing has `POD_NAME`/`POD_NAMESPACE`/`POD_IP`
   from `fieldRef`. Preserve verbatim.

Example — `agent-manager.yaml`:

```yaml
# before (PR #3 main)
envFrom:
  - configMapRef:
      name: voipbin-config
  - secretRef:
      name: voipbin-secret

# after (this PR)
env:
  - name: DATABASE_DSN          # renamed
    valueFrom:
      secretKeyRef:
        name: voipbin
        key: DATABASE_DSN_BIN
  - name: RABBITMQ_ADDRESS
    valueFrom: { secretKeyRef: { name: voipbin, key: RABBITMQ_ADDRESS } }
  - name: REDIS_ADDRESS
    valueFrom: { secretKeyRef: { name: voipbin, key: REDIS_ADDRESS } }
  - name: REDIS_PASSWORD
    valueFrom: { secretKeyRef: { name: voipbin, key: REDIS_PASSWORD } }
  - name: REDIS_DATABASE
    valueFrom: { secretKeyRef: { name: voipbin, key: REDIS_DATABASE } }
  - name: CLICKHOUSE_ADDRESS
    valueFrom: { secretKeyRef: { name: voipbin, key: CLICKHOUSE_ADDRESS } }
  - name: PROMETHEUS_ENDPOINT
    value: /metrics
  - name: PROMETHEUS_LISTEN_ADDRESS
    value: ":2112"
```

Total: 31 services × 4–16 env-vars each ≈ 290 lines of new explicit
env declarations replacing ~62 lines of `envFrom` blocks.

### 4.4 Hook-manager port addition

Production `hook-manager` listens on both 80 and 443. install repo
currently exposes only 443 (PR #3b). This PR adds:

- Deployment: container port 80 (named `service-http`).
- Service `hook-manager`: port 80 → targetPort 80 (in addition to
  existing 443).
- NetworkPolicy `allow-ingress-to-hook`: allow ingress port 80 (in
  addition to existing 443).

Rationale: webhook providers that don't speak TLS hit port 80.
Production confirmed dual-listen.

Security note: port 80 receives **cleartext** webhooks. Authenticity
is gated by **per-provider signature verification** inside hook-manager
(monorepo bin-hook-manager code), not by transport security. Operators
relying on TLS-only providers can firewall port 80 at LB level if
desired. PR #4d README will call this out.

### 4.5 Delete `api-manager-internal`

PR #3b added `Service/api-manager-internal` (ClusterIP, ports 2112 +
9000) as a forward-compat surface. **Production has no such Service.**
Audiosocket is Pod-IP direct-dial (confirmed by Asterisk Pod TCP probe:
Service DNS:9000 fails, Pod IP:9000 succeeds).

Delete:
- `k8s/backend/services/api-manager-internal.yaml`
- Reference in `k8s/backend/services/kustomization.yaml`
- Any NetworkPolicy mentioning `api-manager-internal`

api-manager Deployment retains containerPort 9000 (Pod-IP direct).
Prometheus scraping of port 2112 already works via Pod-level
`prometheus.io/scrape` annotation; no Service required.

### 4.6 Migration job (`k8s/database/migration-job.yaml`)

Currently uses `envFrom: secretRef: voipbin-secret`. Rewire to
explicit `env:` block. Minimum env-vars needed: `DATABASE_DSN` ←
`DATABASE_DSN_BIN` (Alembic targets bin_manager schema only;
Asterisk DB schema is managed separately).

If a future Job needs Asterisk schema migration, add a separate Job
manifest. Do not overload the existing bin migration Job.

### 4.7 Frontend Deployments (square-manager ns) — TLS rollback

`k8s/frontend/admin.yaml`, `k8s/frontend/talk.yaml`,
`k8s/frontend/meet.yaml`:
- **Delete** the `nginx-tls` sidecar container from each Deployment.
- **Delete** the `voipbin-tls` Secret volumeMount.
- **Delete** the `nginx-tls-proxy-config` ConfigMap volumeMount.
- Frontend container keeps its own port 80 (the static-site nginx).
- **Keep** the `nginx-exporter` sidecar (production retains it for
  Prometheus metrics on port 9113; this is a separate sidecar from
  nginx-tls).

`k8s/frontend/tls-proxy-configmap.yaml`:
- **Delete** this file.
- Remove reference from `k8s/frontend/kustomization.yaml`.

`k8s/frontend/admin.yaml` Service:
- Change port 443 → targetPort **80** (was targetPort 443 to sidecar).
- Keep loadBalancerIP annotation from PR #11/#12.

Same for `talk.yaml` and `meet.yaml` Service blocks.

`k8s/namespaces.yaml`:
- **Keep** `square-manager` ns (frontend separation is still useful).

### 4.8 `voipbin-tls` Secret bootstrap removal

`scripts/tls_bootstrap.py` (rewritten):
- Stop creating `Secret/voipbin-tls` in either namespace.
- Stop touching `square-manager` ns (TLS cert is gone from there).
- Generate four base64 PEM strings (api cert/key, hook cert/key) and
  write them into `secrets.yaml` (sops-encrypted) under keys
  `SSL_CERT_API_BASE64`, `SSL_PRIVKEY_API_BASE64`,
  `SSL_CERT_HOOK_BASE64`, `SSL_PRIVKEY_HOOK_BASE64`.
- **Idempotency, exact spec**:
  - If all four SSL keys exist in `secrets.yaml`: skip (BYOC
    operator's pre-existing cert is preserved).
  - If 0 of 4 exist: generate both cert pairs.
  - If 1–3 of 4 exist (partial state): generate only the missing
    pairs. Treat api-cert pair and hook-cert pair as separate units:
    either both api keys or neither (same for hook). If only one of
    a pair exists (e.g. CERT but not PRIVKEY), raise an error
    (corrupt secrets.yaml).
  - `JWT_KEY` generation follows the same skip-if-exists rule.
- This is now a pure "seed the sops file" script. No kubectl
  interaction. Single test surface, no race conditions with cluster
  state.

### 4.9 NetworkPolicy

`k8s/network-policies/bin-manager-policies.yaml`:
- Add port 80 to `allow-ingress-to-hook` ingress rule.

`k8s/network-policies/square-manager-policies.yaml`:
- Remove rules that referenced `voipbin-tls` Secret access or the
  bootstrap Job's pod selector. Frontend Pods now have no Secret
  dependency.
- Keep frontend ingress allow-list (port 80 from LoadBalancer-routed
  traffic).

### 4.10 `voip` namespace Secret

K8s does not allow cross-ns Secret refs, so `voip` ns needs its own
Secret. Production `Secret/voipbin` in ns `voip` has **10 keys**
(extracted 2026-05-12):

| Key | Class | Consumer |
|---|---|---|
| DATABASE_ASTERISK_DATABASE | config | asterisk-registrar (asterisk container) |
| DATABASE_ASTERISK_HOST | config | asterisk-registrar |
| DATABASE_ASTERISK_PASSWORD | secret | asterisk-registrar |
| DATABASE_ASTERISK_PORT | config | asterisk-registrar |
| DATABASE_ASTERISK_USERNAME | config | asterisk-registrar |
| KAMAILIO_INTERNAL_LB_ADDRESS | config | asterisk-call (asterisk container) |
| KAMAILIO_INTERNAL_LB_NAME | config | asterisk-call |
| RABBITMQ_ADDRESS | config | all 3 asterisk-proxy sidecars (call/conference/registrar) |
| REDIS_ADDRESS | config | all 3 asterisk-proxy sidecars |
| REDIS_PASSWORD | secret | all 3 asterisk-proxy sidecars |

Per-deployment env wiring (production-extracted):

- **asterisk-call**: 2 containers.
  - `asterisk-proxy` sidecar: `RABBITMQ_ADDRESS`, `REDIS_ADDRESS`,
    `REDIS_PASSWORD` from Secret; ~17 literal envs (`ARI_ADDRESS`,
    `ARI_ACCOUNT`, etc.).
  - `asterisk` main: `KAMAILIO_INTERNAL_LB_ADDRESS`,
    `KAMAILIO_INTERNAL_LB_NAME` from Secret; `POD_IP` fieldRef.
- **asterisk-conference**: 2 containers.
  - `asterisk-proxy`: same 3 Secret keys, ~16 literals.
  - `asterisk`: `POD_IP` fieldRef only.
- **asterisk-registrar**: 2 containers.
  - `asterisk-proxy`: same 3 Secret keys, ~14 literals.
  - `asterisk`: 5 `DATABASE_ASTERISK_*` Secret keys (all DSN-component
    fields, NOT a single DSN string), plus `POD_IP` fieldRef.

Action for PR #4:
- Replace `k8s/voip/secret.yaml` `stringData` with these 10 keys (each
  a `PLACEHOLDER_VOIP_*` token).
- Update `scripts/k8s.py` substitution map with 10 corresponding
  tokens. Defaults:
  - `DATABASE_ASTERISK_HOST`: same private-IP/proxy host as bin-manager
    DSN (will become Cloud SQL direct IP in PR #5).
  - `DATABASE_ASTERISK_PORT`: `3306`
  - `DATABASE_ASTERISK_DATABASE`: `asterisk`
  - `DATABASE_ASTERISK_USERNAME`: `asterisk`
  - `DATABASE_ASTERISK_PASSWORD`: from sops `secrets.yaml`, fallback
    `dummy-asterisk-password`.
  - `KAMAILIO_INTERNAL_LB_ADDRESS`: needs operator input (Kamailio
    internal LB IP). Placeholder until configured.
  - `KAMAILIO_INTERNAL_LB_NAME`: similar.
  - `RABBITMQ_ADDRESS`, `REDIS_ADDRESS`, `REDIS_PASSWORD`: identical
    values to bin-ns Secret (substituted from same tokens).
- Update 3 `k8s/voip/asterisk-*/deployment.yaml` files to wire
  explicit `env:` blocks per production extraction (envFrom already
  not used in production).
- Add `DATABASE_ASTERISK_PASSWORD` and `KAMAILIO_INTERNAL_LB_ADDRESS`
  to `scripts/secretmgr.py` allowed-sops-keys set.
- Add tests: `test_voip_secret_schema_complete` (10 keys),
  `test_voip_deploys_no_envfrom`.

Sops-editable keys count update: 22 bin secret + 3 bin DSN + 1 voip
secret (`DATABASE_ASTERISK_PASSWORD`) = **26 operator-editable via
sops**. The two `KAMAILIO_INTERNAL_LB_*` keys are operator-discovered
infrastructure values; per §5.3 they flow through `config.yaml`
(prompted at first `init`), NOT through sops `secrets.yaml`.

## 5. Script changes

### 5.1 `scripts/secret_schema.py` (new module)

Single source of truth for:
- 53 Secret key definitions (name, default value, class).
- Per-service env wiring (Pod env name → Secret key name) for all 31
  bin-* services.
- Literal env-vars per service.

Consumed by:
- `scripts/k8s.py` for substitution map and Secret manifest generation.
- Tests for assertions (no drift between code and manifests).

### 5.2 `scripts/k8s.py` — substitution map

- Build the substitution map from `secret_schema.py`.
- All 53 keys get a substitution token; values from sops `secrets.yaml`
  override defaults from `secret_schema.py`.
- Remove obsolete tokens (`PLACEHOLDER_RABBITMQ_USER`,
  `PLACEHOLDER_RABBITMQ_PASSWORD` — superseded by direct
  `PLACEHOLDER_RABBITMQ_ADDRESS`).
- Keep all PR #10 / PR #11 static-IP and `PLACEHOLDER_DOMAIN` tokens.

### 5.3 `scripts/secretmgr.py` — allowed sops keys

The operator-supplied sops `secrets.yaml` may contain only operator
or init-managed keys. Final count per §4.10 update:

- 22 bin-ns `secret` class keys (per §3.2)
- 3 bin-ns `dsn` class keys
- 1 voip-ns `DATABASE_ASTERISK_PASSWORD`

= **26 operator-editable via sops**. (Note: `JWT_KEY` and the four
`SSL_*_BASE64` keys are init-generated by `tls_bootstrap.py` and end
up in `secrets.yaml`, so the file contains 31 entries total. Operator
must not edit the init-generated five manually; `verify` warns if
their format is invalid.)

**Unknown-key handling**: `secretmgr.py` hard-fails on any key in
`secrets.yaml` that is not in the 31-entry allowed set (26 sops +
5 init-generated). Prevents typos like `JWT_KEYS` from silently being
ignored. Error message names the offending key.

Host/address `config`-class keys (CLICKHOUSE_ADDRESS, REDIS_ADDRESS,
RABBITMQ_ADDRESS, HOMER_API_ADDRESS, GCP_BUCKET_NAME_*, GCP_REGION,
KAMAILIO_INTERNAL_LB_ADDRESS, KAMAILIO_INTERNAL_LB_NAME, etc.) flow
through `PLACEHOLDER_*` substitution from `config.yaml` (or defaults),
**not** through `secrets.yaml`. Operators wanting different infra
hosts edit `config.yaml`; `secret_schema.py` supplies the defaults.
This is consistent with PR #3c's `tls_strategy` config flow. New
config.yaml fields for PR #4: `kamailio_internal_lb_address` (required,
prompted at first `init`), `kamailio_internal_lb_name` (optional,
defaults to `kamailio-internal-lb`).

### 5.4 `scripts/tls_bootstrap.py` — rewritten

See §4.8. Drops multi-namespace Secret writes. Writes 4 base64 PEM
strings + JWT_KEY into `secrets.yaml`. Single namespace touch removed
entirely (no kubectl interactions). This is now a pure "seed the sops
file" script.

### 5.5 `scripts/preflight.py` — unchanged

PR #10/#11 static-IP preflight remains. No changes.

## 6. Tests

### 6.1 Existing test rewrites

- `tests/test_k8s.py`: rewire all `PLACEHOLDER_*_PASSWORD` /
  `PLACEHOLDER_*_USER` assertions to match new substitution map.
- Update tests asserting `Secret/voipbin-secret` → `Secret/voipbin`.
- Update tests asserting `ConfigMap/voipbin-config` → assert absence.
- Update tests asserting `voipbin-tls` Secret → assert absence.
- Update tests asserting nginx-tls sidecar → assert absence.
- Update `tls_bootstrap` tests: new behaviour writes to secrets.yaml,
  no kubectl involvement.

### 6.2 New tests

1. **`test_secret_schema_complete`** — render manifests, locate the
   `voipbin` Secret resource, assert all 53 keys are present (no
   missing, no extra).

2. **`test_no_envfrom_in_bin_services`** — render manifests, for every
   Deployment in `bin-manager` ns assert `envFrom` is absent and `env`
   is present.

3. **`test_bin_services_reference_existing_secret_keys`** — for every
   `secretKeyRef.key` referenced by any bin-* Deployment env, assert
   the key exists in the `voipbin` Secret stringData. Catches typos.

4. **`test_bin_services_rename_map`** — for each service in the rename
   map (derived from `secret_schema.py`), assert the rendered manifest
   has exactly those `(env_name, secret_key)` tuples. Catches future
   editors "fixing" the unusual renames (especially
   `registrar-manager`).

5. **`test_no_voipbin_config_configmap`** — render manifests, assert
   no ConfigMap named `voipbin-config` exists in any namespace.

6. **`test_no_voipbin_tls_secret`** — render manifests, assert no
   Secret named `voipbin-tls` exists in any namespace.

7. **`test_no_nginx_tls_sidecar`** — render manifests, for every
   Deployment in `square-manager` ns assert no container named
   `nginx-tls` or volume mount referencing `voipbin-tls`.

8. **`test_hook_manager_exposes_80_and_443`** — assert Service
   `hook-manager` has both ports, and Deployment containerPort 80
   exists.

9. **`test_hook_networkpolicy_allows_80_and_443`** — assert
   `allow-ingress-to-hook` NetworkPolicy permits both ports.

10. **`test_no_api_manager_internal_service`** — assert no Service
    named `api-manager-internal` exists.

11. **`test_no_brand_domain_in_rendered_manifests`** — parse rendered
    manifest stream as YAML; walk every resource's stringData / env /
    spec values; assert no value contains the brand-domain literal.
    Comments (`#` lines) are excluded by the YAML parse. Catches
    B1-class issues: untemplated production literals leaking into
    self-hoster manifests.

12. **`test_tls_bootstrap_seeds_secrets_yaml`** — call
    `tls_bootstrap.run()` against a temp sops file; assert:
    - First run: four SSL keys + JWT_KEY present, valid base64 PEM.
    - Second run (all five keys exist): file mtime AND decoded values
      byte-equal to first run. No regeneration.
    - Third run with only `SSL_CERT_API_BASE64` deleted (partial
      state): both api keys regenerated (cert + privkey treated as
      one unit), hook keys + JWT_KEY untouched.
    - Fourth run with `SSL_CERT_API_BASE64` deleted but
      `SSL_PRIVKEY_API_BASE64` present (corrupt half-state): raises
      explicit error, does not silently regenerate.

Expected count: existing tests (some rewritten) + 12 new. Exact final
count confirmed during implementation.

## 7. Migration / operator impact

This is a **fresh-install installer**; no upgrade path required.

Any cluster previously deployed from PR #3 main must be torn down
and reinstalled once PR #4 lands. PR description will state this.

For the pre-existing production cluster, this PR has zero
impact — production was not built from the install repo.

## 8. Open questions

None at iter 2 time. All earlier open questions resolved:
- `api-manager-internal` Service → DELETED (pchero confirmed Pod-IP
  direct usage, no Service needed).
- 9000 audiosocket routing → Pod-IP direct, confirmed via TCP probe.
- voip-ns Secret minimum keys → `RABBITMQ_ADDRESS` + likely
  `DATABASE_DSN_ASTERISK`; confirm at implementation by inspecting
  Asterisk container env in production.
- Three "orphan" keys (STREAMING_LISTEN_PORT, STT_PROVIDER_PRIORITY,
  ENGINE_KEY_CHATGPT) → consumed via literal/rename paths; kept in
  Secret for prod parity (§3.3).

## 9. Risks

- **All 31 bin-* Deployment YAMLs touched + 3 frontend YAMLs + scripts**
  → high diff surface. Mitigation: mechanical change pattern,
  per-service appendix as source of truth, automated tests #3 and #4.
- **Renamed env-vars** (registrar-manager DSN, SSL_CERT_BASE64,
  ENGINE_KEY_CHATGPT, PROJECT_BUCKET_NAME, AUTHTOKEN_TELNYX,
  GCP_BUCKET_NAME) → easy to miss. Mitigation: appendix + test #4.
- **secrets.yaml schema change** breaks operator sops files from PR #3.
  Mitigation: `voipbin-install init` regenerates secrets.yaml from
  scratch; documented in PR description.
- **tls_bootstrap behavioural change** (no longer touches kubectl).
  Verify init flow still works end-to-end. Test #12.
- **PR #3b artifacts deleted** (voipbin-tls, nginx-tls sidecar,
  multi-ns bootstrap, api-manager-internal) → confirm tests #6/#7/#10
  catch any leftover reference.

## 10. Implementation order

1. Create `scripts/secret_schema.py` with 53-key bin inventory + 10-key
   voip inventory + per-service rename map derived from appendix
   `pr4a-env-wiring.md` and §4.10. Single source of truth.
2. Update `scripts/k8s.py` substitution map.
3. Rewrite `k8s/backend/secret.yaml` (53 stringData keys).
4. Delete `k8s/backend/configmap.yaml`.
5. Rewrite 31 `k8s/backend/services/*.yaml` (mechanical, per appendix).
   - Audit each for templated literals (call-manager
     `PROJECT_BASE_DOMAIN` → `PLACEHOLDER_DOMAIN`).
6. Update `k8s/backend/services/hook-manager.yaml` (port 80).
7. Delete `k8s/backend/services/api-manager-internal.yaml`.
8. Update `k8s/database/migration-job.yaml`.
9. Update `k8s/backend/kustomization.yaml` AND
   `k8s/backend/services/kustomization.yaml` to reflect file deletions
   (configmap + api-manager-internal) in a single explicit step.
10. Delete nginx-tls sidecar from 3 frontend Deployments.
11. Delete `k8s/frontend/tls-proxy-configmap.yaml` and remove from
    `k8s/frontend/kustomization.yaml`.
12. Update 3 frontend Service blocks (443 → targetPort 80).
13. Rewrite `scripts/tls_bootstrap.py` (sops-only, partial-state spec).
14. Rewrite `k8s/voip/secret.yaml` (10 keys per §4.10).
15. Rewrite 3 `k8s/voip/asterisk-*/deployment.yaml` files for explicit
    env wiring per §4.10.
16. Update `k8s/network-policies/bin-manager-policies.yaml` (port 80 on
    hook).
17. Update `k8s/network-policies/square-manager-policies.yaml` (remove
    voipbin-tls Secret access).
18. Update `scripts/secretmgr.py` allowed-sops keys (28 operator-editable).
19. Rewrite/add tests per §6.
20. Run `pytest`, `kustomize build` dry-run, sensitive-data audit.
21. Commit, push, open PR.

## A. Per-service env wiring appendix

See `pr4a-env-wiring.md` (filename retained from earlier split for
git-blame continuity; covers the same 31 services). 31 services ×
env-var list, derived from production extraction on 2026-05-12. This
appendix is the **mechanical source** for §4.3 manifest rewrites and
for `scripts/secret_schema.py` rename-map constants.

## B. PR #3b items reversed in PR #4

| PR #3b item | PR #4 disposition |
|---|---|
| `Secret/voipbin-tls` in `bin-manager` ns | DELETED (TLS now via SSL_*_BASE64 in `voipbin` Secret) |
| `Secret/voipbin-tls` in `square-manager` ns | DELETED |
| nginx-tls sidecar in 3 frontend Deployments | DELETED |
| `k8s/frontend/tls-proxy-configmap.yaml` | DELETED |
| Frontend Service 443 → targetPort 443 | CHANGED to targetPort 80 |
| Multi-ns `tls_bootstrap.py` (two namespaces) | REWRITTEN (writes to sops, not kubectl) |
| `Service/api-manager-internal` | DELETED |
| `square-manager` namespace | KEPT (still useful for future per-app isolation) |
| `hook-manager` LB + PDB + replicas=2 | KEPT |
| `hook-manager` Service port 443 | KEPT, plus added port 80 |

## Checklist (design phase)

- [x] Production secret inventory captured (53 keys)
- [x] All 31 bin-* service env wirings captured
- [x] Hook-manager dual-port (80 + 443) documented
- [x] api-manager-internal deletion justified by Pod-IP-direct evidence
- [x] frontend TLS rollback (Cloudflare front-of-cluster model) documented
- [x] tls_bootstrap rewrite documented (sops-only, no kubectl)
- [x] PR #3b reversals enumerated (§B)
- [x] Renamed env-vars enumerated (§4.3 callouts)
- [x] Templated literals callout (call-manager PROJECT_BASE_DOMAIN)
- [x] Test plan with 12 tests
- [x] Out-of-scope items deferred to PR #5 and PR #4d explicitly
- [x] iter 1 blockers (B1–B5) addressed
- [ ] Design review iter 2
- [ ] (optional) Design review iter 3 if iter 2 surfaces blockers
