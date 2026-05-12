# PR-D2b — k8s manifest DSN placeholder rewrite + substitution wiring

Status: Design v1 (draft)
Author: Hermes (CPO)
Date: 2026-05-12
Branch: NOJIRA-PR-D2b-k8s-dsn-substitution-wiring
Builds on: PR-D2a (`aa2078f`)
Parent roadmap: `~/agent-hermes/notes/2026-05-12-pr-d2-split-roadmap.md` (v3)

## 1. Goal

Wire the 5 sensitive Cloud SQL password outputs from PR-D2a into the rendered
`k8s/backend/secret.yaml` so bin-manager / asterisk / rag-manager Pods can
authenticate against the new per-app MySQL/Postgres users. After this PR
merges, the 3 `DATABASE_DSN_*` strings in the running `voipbin` Secret
resolve to real Cloud SQL IPs + real random passwords.

**Out of scope (deferred to PR-D2c):**
- Ansible kamailioro env wiring (`KAMAILIO_AUTH_DB_URL`).
- Postgres password URL-safety regression test (alphabet already locked in D2a).
- Bulk rename of legacy `PLACEHOLDER_CLOUDSQL_PRIVATE_IP[_CIDR]` keys in voip ns / NetworkPolicy manifests (the legacy MySQL alias stays).

## 2. Production extraction (skill `design-first-with-review-loops` precondition)

`k8s/backend/secret.yaml` defines `Secret` named `voipbin` in namespace
`bin-manager`. Inspection of the existing manifest (lines 15-17 in the
PR-D2a base):

```
DATABASE_DSN_ASTERISK: "asterisk:dummy-password@tcp(PLACEHOLDER_CLOUDSQL_PRIVATE_IP:3306)/asterisk"
DATABASE_DSN_BIN: "bin-manager:dummy-password@tcp(PLACEHOLDER_CLOUDSQL_PRIVATE_IP:3306)/bin_manager"
DATABASE_DSN_POSTGRES: "postgres://bin-manager:***@PLACEHOLDER_CLOUDSQL_PRIVATE_IP:5432/bin_manager?sslmode=disable"
```

Consumers (verified via grep across `k8s/backend/services/*.yaml`): 30+
Deployment manifests reference one of the three DSN keys via
`secretKeyRef.name: voipbin, secretKeyRef.key: DATABASE_DSN_BIN` (or `_ASTERISK`
for registrar-manager, or `_POSTGRES` for rag-manager). The DSN env-var on the
container side is named `DATABASE_DSN` (downstream Go services), not
`DATABASE_DSN_BIN`; the rename happens at the Secret-to-container boundary via
`name:` in the env entry.

**No Deployment manifest changes are required.** The container reads
`DATABASE_DSN` (or `POSTGRES_DSN` for rag-manager); the Secret's key is what
PR-D2b rewrites. Existing `secretKeyRef` references stay.

### End-to-end render simulation (mandated by skill)

```python
from unittest.mock import MagicMock
from scripts.k8s import _build_substitution_map

cfg = MagicMock()
cfg.get.side_effect = lambda k, d="": {
    "domain": "example.com",
    "gcp_project_id": "proj-abc",
    "region": "us-central1",
    "cloudsql_private_ip": "10.42.0.5",
    "cloudsql_postgres_private_ip": "10.42.0.6",
    "rabbitmq_user": "guest",
}.get(k, d)

tf_outputs = {
    "cloudsql_mysql_password_bin_manager": "MySQLBinPwd-Sample-24chars!",
    "cloudsql_mysql_password_asterisk": "MySQLAstPwd-Sample-24chars!",
    "cloudsql_mysql_password_call_manager": "MySQLCmPwd-Sample-24chars!",
    "cloudsql_mysql_password_kamailioro": "MySQLKamPwd-Sample-24chars!",
    "cloudsql_postgres_password_bin_manager": "PgBinPwd-Sample-24chars!",
}

subs = _build_substitution_map(cfg, tf_outputs, {})

# Simulate the substitution loop against the new k8s/backend/secret.yaml line:
template = (
    'DATABASE_DSN_BIN: "bin-manager:PLACEHOLDER_DSN_PASSWORD_MYSQL_BIN_MANAGER'
    '@tcp(PLACEHOLDER_CLOUDSQL_MYSQL_PRIVATE_IP:3306)/bin_manager"'
)
for tok in sorted(subs, key=len, reverse=True):
    template = template.replace(tok, str(subs[tok]))

assert "PLACEHOLDER_" not in template
assert "MySQLBinPwd-Sample-24chars!" in template
assert "10.42.0.5" in template
```

This script lives in tests/test_pr_d2b_dsn_render.py and runs at pytest time.

## 3. Strategy decision (locked)

The roadmap §PR-D2b scope item 1 listed two strategies:

(a) Replace whole DSN strings with single tokens (`PLACEHOLDER_DATABASE_DSN_BIN`).
(b) Keep DSN shape; replace password slot with `PLACEHOLDER_DSN_PASSWORD_*`
    and IP slot with `PLACEHOLDER_CLOUDSQL_{MYSQL,POSTGRES}_PRIVATE_IP`.

**Strategy (b) selected.** Rationale:

- Minimal diff against existing `k8s/backend/secret.yaml` (3 lines edited,
  not deleted-and-replaced).
- The DSN string SHAPE stays visible in the manifest (operators can read
  the rendered Secret and see "bin-manager:...@tcp(10.x.x.x:3306)/bin_manager"
  format, matching production).
- Substitution mechanism already supports nested placeholders within one
  string (verified in `_render_manifests`: longest-first loop processes
  every key, so `PLACEHOLDER_DSN_PASSWORD_MYSQL_BIN_MANAGER` and
  `PLACEHOLDER_CLOUDSQL_MYSQL_PRIVATE_IP` both resolve in the same pass).
- Strategy (a) would require `_build_substitution_map` to compose the full
  DSN string from terraform outputs, duplicating DSN-format knowledge
  between `secret_schema.py` and `k8s.py`. (b) keeps the DSN shape in one
  place (the manifest).

## 4. Schema migration paths (preconditions #3, #4 from roadmap)

### 4.0.1 `bin_manager` MySQL schema (precondition #3)

Schema management owner: `bin-dbscheme-manager` (monorepo). Uses **Alembic**
(not golang-migrate as the v1 retrospective speculated). Migrations live in
`bin-dbscheme-manager/bin-manager/main/versions/*.py`.

Execution path on this cluster: `k8s/database/migration-job.yaml` runs a
Kubernetes Job named `database-migration` in the `bin-manager` namespace using
the `voipbin/bin-database` Docker image. The Job's container reads
`DATABASE_DSN_BIN` from the `voipbin` Secret (the same Secret this PR
rewrites). On image build, `bin-dbscheme-manager`'s Dockerfile pre-applies
the Alembic migrations against a temporary local MariaDB and exports schema
dumps to `/docker-entrypoint-initdb.d/`; at Job runtime, the dumps are
imported into the live `bin_manager` Cloud SQL database.

**Exit criterion (mandatory):** after `voipbin-install apply`, the Job
`database-migration` completes successfully (`kubectl get job -n bin-manager
database-migration -o json | jq .status.succeeded` returns `1`). If the Job
remains `failed` or `running` past 5 minutes, dogfood is failed and we
auto-revert.

**Why this works after D2b but not before:** pre-D2b the Job's
`DATABASE_DSN_BIN` env-var pointed at `bin-manager:dummy-password@tcp(<MySQL
IP>:3306)/bin_manager` against a database that did NOT exist (legacy was
`voipbin`). Post-D2a the `bin_manager` database exists; post-D2b the DSN
authenticates with the real password. Both gates lift simultaneously.

### 4.0.2 `asterisk` MySQL schema (precondition #4)

Schema management owner: `bin-dbscheme-manager/asterisk_config/` (monorepo,
Alembic). On the install cluster there is currently **no Job manifest** to
apply this schema. Production must run it some other way (likely a separate
out-of-band Job or a CI/CD pipeline step that this install repo doesn't yet
own).

**Decision: defer asterisk schema wiring out of PR-D2b.**

Rationale:
- The 3 DSN strings rewritten by D2b include `DATABASE_DSN_ASTERISK`, but it
  is consumed only by Kamailio (via realtime, future D2c) and by
  asterisk-realtime Deployments in the `voip` namespace. Both of those are
  out of scope for D2b.
- Adding an asterisk migration Job is a new manifest + image dependency,
  expanding D2b beyond the LOC budget (≤300 prod LOC) and beyond the goal
  ("DSN wiring").
- Without the schema, `asterisk-realtime` Pod will still start (Asterisk
  tolerates an empty PJSIP config DB by falling back to file config); the
  manifestation is a non-functional Asterisk realtime, not a Pod crash.

**Follow-up filed:** `NOJIRA-Asterisk-MySQL-Schema-Migration-Job` stub added
to `docs/follow-ups.md`.

**Smoke assertion update (§5):** D2b smoke checks `bin_manager` schema only
(via the Job exit status). `asterisk` schema verification is deferred to the
follow-up.

## 4.1 `k8s/backend/secret.yaml` (3 lines edited)

```diff
- DATABASE_DSN_ASTERISK: "asterisk:dummy-password@tcp(PLACEHOLDER_CLOUDSQL_PRIVATE_IP:3306)/asterisk"
- DATABASE_DSN_BIN: "bin-manager:dummy-password@tcp(PLACEHOLDER_CLOUDSQL_PRIVATE_IP:3306)/bin_manager"
- DATABASE_DSN_POSTGRES: "postgres://bin-manager:***@PLACEHOLDER_CLOUDSQL_PRIVATE_IP:5432/bin_manager?sslmode=disable"
+ DATABASE_DSN_ASTERISK: "asterisk:PLACEHOLDER_DSN_PASSWORD_MYSQL_ASTERISK@tcp(PLACEHOLDER_CLOUDSQL_MYSQL_PRIVATE_IP:3306)/asterisk"
+ DATABASE_DSN_BIN: "bin-manager:PLACEHOLDER_DSN_PASSWORD_MYSQL_BIN_MANAGER@tcp(PLACEHOLDER_CLOUDSQL_MYSQL_PRIVATE_IP:3306)/bin_manager"
+ DATABASE_DSN_POSTGRES: "postgres://bin-manager:PLACEHOLDER_DSN_PASSWORD_POSTGRES_BIN_MANAGER@PLACEHOLDER_CLOUDSQL_POSTGRES_PRIVATE_IP:5432/bin_manager?sslmode=disable"
```

### 4.2 `scripts/secret_schema.py` (3 DSN defaults updated)

`BIN_SECRET_KEYS["DATABASE_DSN_BIN"|"DATABASE_DSN_ASTERISK"|"DATABASE_DSN_POSTGRES"]["default"]`
updated to mirror the manifest's new tokens. No `dummy-password` anywhere.

### 4.3 `scripts/k8s.py:_build_substitution_map` (7 new entries)

Add to the `subs.update({...})` block:

```python
# PR-D2a/D2b: Cloud SQL application user passwords (from terraform outputs).
"PLACEHOLDER_DSN_PASSWORD_MYSQL_BIN_MANAGER": terraform_outputs.get(
    "cloudsql_mysql_password_bin_manager", ""),
"PLACEHOLDER_DSN_PASSWORD_MYSQL_ASTERISK": terraform_outputs.get(
    "cloudsql_mysql_password_asterisk", ""),
"PLACEHOLDER_DSN_PASSWORD_MYSQL_CALL_MANAGER": terraform_outputs.get(
    "cloudsql_mysql_password_call_manager", ""),
"PLACEHOLDER_DSN_PASSWORD_MYSQL_KAMAILIORO": terraform_outputs.get(
    "cloudsql_mysql_password_kamailioro", ""),
"PLACEHOLDER_DSN_PASSWORD_POSTGRES_BIN_MANAGER": terraform_outputs.get(
    "cloudsql_postgres_password_bin_manager", ""),
# New IP placeholders for the rewritten DSN defaults; values mirror the
# existing legacy aliases (which stay in place for non-DSN manifests).
"PLACEHOLDER_CLOUDSQL_MYSQL_PRIVATE_IP": cloudsql_private_ip,
"PLACEHOLDER_CLOUDSQL_POSTGRES_PRIVATE_IP": config.get(
    "cloudsql_postgres_private_ip", ""),
```

Legacy `PLACEHOLDER_CLOUDSQL_PRIVATE_IP[_CIDR]` stay (consumed by
`k8s/voip/secret.yaml` and the NetworkPolicy manifests).

### 4.4 New tests `tests/test_pr_d2b_dsn_render.py`

| Class | Cases | Coverage |
|---|---|---|
| `TestSubstitutionMapNewEntries` | 7 | each of 5 password placeholders maps to the right tf_output key; 2 new IP placeholders resolve correctly |
| `TestSecretSchemaDsnDefaults` | 3 | each DSN default in BIN_SECRET_KEYS contains the new password + IP placeholders, no `dummy-password` |
| `TestSecretYamlManifest` | 3 | `k8s/backend/secret.yaml` contains the new placeholders on the 3 DSN lines |
| `TestEndToEndRenderBin` | 1 | mock TF outputs, run subs against secret.yaml text, assert DATABASE_DSN_BIN line contains real password + MySQL IP, no `PLACEHOLDER_` left |
| `TestEndToEndRenderAsterisk` | 1 | same for DATABASE_DSN_ASTERISK |
| `TestEndToEndRenderPostgres` | 1 | same for DATABASE_DSN_POSTGRES (Postgres IP, postgres password) |
| `TestLegacyMySQLAliasStillEmitted` | 1 | PR-D2a regression guard — `PLACEHOLDER_CLOUDSQL_PRIVATE_IP[_CIDR]` keys still emitted (consumed by voip ns + NetworkPolicies) |
| `TestRenderManifestsLoopHonorsLongestFirst` | 1 | drive the actual `_render_manifests` substitution loop logic; assert nested-placeholder DSN resolves correctly (drives production code, NOT a re-implementation) |
| `TestPostgresDsnPrefixCanonical` | 1 | `DATABASE_DSN_POSTGRES` default starts with `postgres://` not `postgresql://` |

**Total: 19 cases.**

### 4.5 Synthetic injection mutants (gate ≥ 5)

1. `dummy-password` re-introduced in any DSN default → trips `TestSecretSchemaDsnDefaults`.
2. `PLACEHOLDER_DSN_PASSWORD_MYSQL_BIN_MANAGER` mistyped (missing underscore) → trips `TestSubstitutionMapNewEntries`.
3. New `PLACEHOLDER_CLOUDSQL_POSTGRES_PRIVATE_IP` removed from substitution map → trips `TestEndToEndRenderPostgres`.
4. Legacy `PLACEHOLDER_CLOUDSQL_PRIVATE_IP` removed from substitution map → trips `TestLegacyMySQLAliasStillEmitted`.
5. `postgresql://` in DSN default → trips `TestPostgresDsnPrefixCanonical`.
6. `k8s/backend/secret.yaml` reverted to dummy-password line → trips `TestSecretYamlManifest`.

## 5. Smoke dogfood (after merge)

**Baseline capture (before `voipbin-install apply`):**
- Same as PR-D2a; capture `kubectl get pods -n bin-manager -o wide`.

**Apply invocation:**

```bash
voipbin-install apply
```

(No `--force-destroy-legacy-voipbin` needed; legacy db was destroyed in PR-D2a.)

**Post-apply assertions:**
1. `kubectl exec -n bin-manager deploy/api-manager -- printenv DATABASE_DSN` resolves to a real DSN — no `PLACEHOLDER_*` and no `dummy-password`.
2. `kubectl get job -n bin-manager database-migration -o jsonpath='{.status.succeeded}'` returns `1` within 5 minutes of apply (per §4.0.1, this is the mandatory exit criterion for bin_manager schema).
3. `kubectl logs -n bin-manager deploy/api-manager --tail=50` shows a successful database connection.
4. `kubectl logs -n bin-manager deploy/rag-manager --tail=100` shows `golang-migrate` completes successfully against Postgres, including `CREATE EXTENSION vector`.
5. `kubectl get pods -n bin-manager` shows api-manager / bin-manager / rag-manager Pods Running (NOT CrashLoopBackoff).
6. Sensitive scan clean on diff.

**Asterisk schema (deferred to follow-up):** Not asserted here per §4.0.2 deferral. The `voip` namespace `asterisk-call` / `asterisk-conference` / `asterisk-registrar` Deployments will start but PJSIP realtime queries against an empty `asterisk` DB will return empty result sets (non-crashing). Filed as `NOJIRA-Asterisk-MySQL-Schema-Migration-Job` in `docs/follow-ups.md`.

**Rollback runbook (operator-facing, in case of dogfood failure):**

```bash
# 1. Revert the merge commit on main
gh pr create --repo voipbin/install --title "NOJIRA-Revert-PR-D2b-k8s-dsn-substitution-wiring" \
    --body "Revert PR-D2b; bin-manager Pods failed to reach Running on dev." \
    --base main --head NOJIRA-Revert-PR-D2b-k8s-dsn-substitution-wiring
# Body details and CI-clean revert handled by abbreviated R1 review.
# 2. After revert merges:
voipbin-install apply --stage k8s_apply
# This re-renders Secret with the previous (dummy-password) DSN strings.
# 3. Pods that had reached Running between D2b apply and revert will CrashLoop
# back; that's expected — they were only functional because D2b was in
# effect. Operator must wait for the next PR to ship before retrying.
```

**Auto-revert trigger:** if assertions (1)-(5) fail, file an auto-revert PR via abbreviated R1 review loop and report to pchero.

## 6. Verification

- `pytest -q --no-header --ignore=tests/test_pr_n_oslogin.py` green (588 baseline + 19 new = 607).
- `terraform fmt -check` clean (no terraform changes in this PR).
- Sensitive scan clean.
- Synthetic injection ≥ 5 catches (target 6/6).

## 7. Exit criteria

- Three `DATABASE_DSN_*` strings in `k8s/backend/secret.yaml` render to real
  Cloud SQL IPs + random passwords after substitution (verified by the
  end-to-end render test and the smoke dogfood).
- bin-manager / api-manager / rag-manager Pods reach Running on dev.
- pytest green; synthetic injection ≥ 5.
- 3-iter min PR review APPROVED.
