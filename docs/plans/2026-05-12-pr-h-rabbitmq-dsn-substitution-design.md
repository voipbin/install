# PR-H — RABBITMQ DSN password substitution fix

**Date:** 2026-05-12
**Author:** Hermes (CPO)
**Status:** Design v1
**Repo:** `voipbin/install`
**Branch:** `NOJIRA-PR-H-rabbitmq-dsn-substitution`
**Parent:** main `20ce352`
**Roadmap slot:** PR-H (Phase 2, install-redesign v3 §6 — independent fix; **must merge BEFORE PR-G**)
**LOC estimate:** ~150 (schema + 2 YAML literals + tests + docs)

## 1. Context

Roadmap v3 §6 PR-H scope: *"RABBITMQ DSN password substitution fix (A-7, GAP-15)"*. pchero
review decision #4: **KEEP full DSN strings in sops** (production parity, no
decomposition into separate user/password/host keys).

The defect today:

- `scripts/secret_schema.py:101-104` (`BIN_SECRET_KEYS["RABBITMQ_ADDRESS"]`) and
  `scripts/secret_schema.py:148-151` (`VOIP_SECRET_KEYS["RABBITMQ_ADDRESS"]`)
  both default to:
  ```
  amqp://guest:***@rabbitmq.infrastructure.svc.cluster.local:5672
  ```
  The literal `***` is a redaction artifact carried over from the production
  Secret extraction — **not** a substitution token.
- The literal token `PLACEHOLDER_RABBITMQ_PASSWORD` exists standalone in
  `scripts/k8s.py:73` and is registered in the substitution map (`secrets.get("rabbitmq_password", "guest")`),
  but the DSN string the operator-supplied password should land inside contains
  `***`, not `PLACEHOLDER_RABBITMQ_PASSWORD`. The standalone substitution at
  `k8s.py:147-148` therefore never touches the DSN.
- Net effect: the rendered `Secret/voipbin` ships with the literal characters
  `***` where the AMQP password belongs. RabbitMQ auth fails at runtime
  (`ACCESS_REFUSED - Login was refused using authentication mechanism PLAIN`)
  for all 31 `bin-*` Deployments and the 3 `voip`-ns asterisk pods. This is
  A-7 / GAP-15.

`k8s/backend/secret.yaml:44` and `k8s/voip/secret.yaml:15` carry the same
literal `amqp://guest:***@...` strings — they are pre-baked from the schema
defaults and have the same bug.

Symmetric `PLACEHOLDER_RABBITMQ_USER` exists at `k8s.py:72` (`config.get("rabbitmq_user", "guest")`).
It is currently dead for the same reason — the user segment of the DSN is the
literal `guest`, not the token.

## 2. Scope

Approaches considered (per task brief):

- **(a)** Introduce a new `PLACEHOLDER_RABBITMQ_PASSWORD_IN_DSN` token, compute
  the full DSN string in `_build_substitution_map()` from
  `(rabbitmq_user, rabbitmq_password, host)`, and substitute the full DSN at
  render time. Requires net-new token plumbing, a new substitution branch, and
  diverges from how every other secret_schema entry uses standalone
  `PLACEHOLDER_*` tokens.
- **(b)** Restructure the DSN **defaults and YAML literals** so the
  existing standalone tokens `PLACEHOLDER_RABBITMQ_USER` and
  `PLACEHOLDER_RABBITMQ_PASSWORD` appear directly inside the DSN string. The
  existing substitution loop at `scripts/k8s.py:147-148` (longest-token-first
  `str.replace`) then injects user + password correctly. **No new code path.**

**Chosen: (b).** Justification:

- The `k8s.py:147-148` loop already sorts tokens by length descending, so
  `PLACEHOLDER_RABBITMQ_PASSWORD` (29 chars) is substituted before
  `PLACEHOLDER_RABBITMQ_USER` (25 chars) and before any shorter overlap risk.
  No new substitution ordering rules required.
- `PLACEHOLDER_RABBITMQ_USER` and `PLACEHOLDER_RABBITMQ_PASSWORD` are **already**
  registered in `_build_substitution_map()` at `scripts/k8s.py:72-73` with the
  correct config/secret sourcing (`config.rabbitmq_user`, `secrets.rabbitmq_password`,
  fallback `"guest"`). Approach (b) makes those existing entries actually do
  work.
- Diff is minimal: 2 schema defaults + 2 rendered YAML literals + tests. No
  changes to `_build_substitution_map()`, no new token, no new branch.
- Production-parity intent preserved: sops still ships the **full** DSN string
  via `secrets["RABBITMQ_ADDRESS"]` when an operator wants to override it;
  that override path (`scripts/k8s.py:49-53`) takes precedence over the schema
  default and stays untouched.

### 2.1 `scripts/secret_schema.py`

- Replace `BIN_SECRET_KEYS["RABBITMQ_ADDRESS"]["default"]` (lines 101-104):
  ```python
  "RABBITMQ_ADDRESS": {
      "default": (
          "amqp://PLACEHOLDER_RABBITMQ_USER:PLACEHOLDER_RABBITMQ_PASSWORD@"
          "rabbitmq.infrastructure.svc.cluster.local:5672"
      ),
      "class": "config",
  },
  ```
- Replace `VOIP_SECRET_KEYS["RABBITMQ_ADDRESS"]["default"]` (lines 148-151)
  with identical string. Both Secrets must remain byte-for-byte identical after
  render (the existing 31-service wiring assumes a single RabbitMQ topology).
- `class` stays `"config"` (production-parity DSN is operator-overridable via
  sops `secrets.yaml`, not a hidden secret).

### 2.2 Rendered YAML literals

- `k8s/backend/secret.yaml:44` — change the `RABBITMQ_ADDRESS:` value from
  `"amqp://guest:***@rabbitmq.infrastructure.svc.cluster.local:5672"` to
  `"amqp://PLACEHOLDER_RABBITMQ_USER:PLACEHOLDER_RABBITMQ_PASSWORD@rabbitmq.infrastructure.svc.cluster.local:5672"`.
- `k8s/voip/secret.yaml:15` — same edit.

These two files are pre-baked from the schema defaults; keeping them in lockstep
is the existing pr4 convention. A unit test (see §2.4) asserts the two files
match the schema defaults verbatim, preventing future drift.

### 2.3 `scripts/k8s.py`

**No code changes.** Verified at lines 65-110:

- `PLACEHOLDER_RABBITMQ_USER` already populated at line 72 (`config.get("rabbitmq_user", "guest")`).
- `PLACEHOLDER_RABBITMQ_PASSWORD` already populated at line 73 (`secrets.get("rabbitmq_password", "guest")`).
- Substitution loop at lines 147-148 already iterates the full map longest-first.

The `secrets.get("rabbitmq_password", "guest")` fallback at line 73 means an
operator who has not yet populated `rabbitmq_password` in `secrets.yaml` still
gets a working dev DSN (`amqp://guest:guest@...`), matching the historical
behaviour of the broken default once `***` is replaced.

### 2.4 Tests (`tests/test_rabbitmq_dsn_substitution.py`, new file)

≥6 tests:

1. **`test_bin_rabbitmq_default_contains_placeholders`** — assert both
   `PLACEHOLDER_RABBITMQ_USER` and `PLACEHOLDER_RABBITMQ_PASSWORD` substrings
   are in `BIN_SECRET_KEYS["RABBITMQ_ADDRESS"]["default"]`. Assert literal
   `PLACEHOLDER_RABBITMQ_PASSWORD` is present (and the redaction artifact `***`
   is **not** present).
2. **`test_voip_rabbitmq_default_contains_placeholders`** — same for
   `VOIP_SECRET_KEYS["RABBITMQ_ADDRESS"]["default"]`.
3. **`test_bin_and_voip_rabbitmq_defaults_match`** — schema defaults are
   string-equal.
4. **`test_rendered_secret_yaml_matches_schema`** — parse
   `k8s/backend/secret.yaml` and `k8s/voip/secret.yaml`, assert their
   `RABBITMQ_ADDRESS` `stringData` value equals the corresponding schema
   default.
5. **`test_substitution_injects_password_into_dsn`** — invoke
   `_build_substitution_map()` with `config.rabbitmq_user="bunny"`,
   `secrets["rabbitmq_password"]="hunter2"`; render a minimal YAML
  containing the new DSN template through the same longest-first
  `str.replace` loop; assert result equals
  `amqp://bunny:PLACEHOLDER_RABBITMQ_PASSWORD@rabbitmq.infrastructure.svc.cluster.local:5672`.
6. **`test_substitution_default_when_secrets_missing`** — empty secrets dict
   → DSN renders as `amqp://guest:PLACEHOLDER_RABBITMQ_PASSWORD@rabbitmq.infrastructure.svc.cluster.local:5672`
   (the substitution map defaults for both user and password are `"guest"`).
7. **`test_sops_override_full_dsn_still_wins`** — when
   `secrets["RABBITMQ_ADDRESS"]="amqp://prod-user:prod-pass@prod-host:5672"`,
   the override path at `scripts/k8s.py:49-53` produces
   `PLACEHOLDER_RABBITMQ_ADDRESS → "amqp://prod-user:prod-pass@..."`. The
   schema default is shadowed; nothing in the rendered Secret contains a
   `PLACEHOLDER_` substring after substitution. (Guards against regression of
   pchero decision #4 — full-DSN override path stays functional.)
8. **`test_no_partial_token_collision`** — assert `PLACEHOLDER_RABBITMQ_USER`
   is **not** a prefix of `PLACEHOLDER_RABBITMQ_PASSWORD` (it isn't:
   `..._USER` vs `..._PASSWORD`), and that longest-first ordering at
   `k8s.py:147` preserves correctness for both.

### 2.5 Documentation

- `docs/plans/2026-05-12-pr4-production-parity-reset-design.md` — add a 2-line
  banner cross-referencing this PR for the RABBITMQ_ADDRESS row of the
  53-key / 10-key inventories (note that `***` was a redaction artifact and
  PR-H replaced it with `PLACEHOLDER_RABBITMQ_{USER,PASSWORD}`).
- README — no change required; the substitution mechanism is internal.

## 3. Out of scope

- Decomposing the DSN into separate user/host/port keys (rejected by pchero
  decision #4 — production keeps the full DSN string).
- Adding `rabbitmq_host` / `rabbitmq_port` placeholders. The host literal
  `rabbitmq.infrastructure.svc.cluster.local:5672` stays hardcoded; PR-G's
  Terraform-output-driven host injection is the right venue if/when needed.
- `REDIS_PASSWORD` / `REDIS_ADDRESS` substitution. They use the standalone
  pattern already and are not in scope for A-7 / GAP-15.
- Any change to `_build_substitution_map()` token list or ordering.
- `scripts/cli.py`, pipeline stages, state file — untouched.

## 4. Risks

- **PR-G merge-conflict on `scripts/k8s.py`**: PR-G ("RabbitMQ broker config
  from Terraform output", roadmap §6) modifies `scripts/k8s.py` lines 73-110
  to source `rabbitmq_password` (and likely `rabbitmq_host`) from
  `terraform_outputs` rather than `secrets.yaml`. PR-H also reads lines 65-110
  but **does not edit them** (§2.3 above). The textual conflict surface is
  therefore the surrounding context only — PR-H's diff is in
  `secret_schema.py` and the two YAML files. **Mitigation: merge PR-H first**
  (roadmap §6 already mandates this). PR-G then rebases on a `k8s.py` whose
  lines 65-110 are unchanged, and PR-G's edits to the
  `PLACEHOLDER_RABBITMQ_PASSWORD` entry land cleanly. If PR-G has already
  branched, the rebase is trivial because PR-H touches no Python in
  `_build_substitution_map()`.
- **Substitution-order regression**: the loop at `k8s.py:147-148` sorts by
  length descending. `PLACEHOLDER_RABBITMQ_PASSWORD` (29) > `PLACEHOLDER_RABBITMQ_USER`
  (25); no substring of one is a prefix of the other. Covered by test §2.4 #8.
- **Operator-supplied full-DSN override**: if an operator sets
  `secrets["RABBITMQ_ADDRESS"]` to a full DSN literal (production-parity
  workflow), the override branch at `k8s.py:49-53` writes the literal into the
  substitution map under key `PLACEHOLDER_RABBITMQ_ADDRESS`. That literal is
  the value substituted into pod env via `valueFrom.secretKeyRef`. Covered by
  test §2.4 #7.
- **YAML-literal drift**: someone edits `k8s/backend/secret.yaml` without
  updating `secret_schema.py` (or vice versa). Covered by test §2.4 #4.
- **Production sync**: existing production Secret has the real password, not
  `***` and not `PLACEHOLDER_*`. PR-H only affects rendered output of
  `voipbin-install`. No live cluster impact.

## 5. Test plan summary

≥8 new tests in `tests/test_rabbitmq_dsn_substitution.py`. Existing
`tests/test_k8s.py` and `tests/test_secret_schema.py` (any tests asserting
literal `***` in the DSN default) updated. No subprocess; pure-Python
substitution assertions matching the existing test pattern.

Target: existing test count + 8 new passing.

## 6. Smoke dogfood (post-merge)

Per roadmap v3 §7 on `voipbin-install-dev`:

- `voipbin-install init` → `voipbin-install apply`.
- After `k8s_apply`: `kubectl -n bin-manager get secret voipbin -o jsonpath='{.data.RABBITMQ_ADDRESS}' | base64 -d`
  → expect `amqp://guest:PLACEHOLDER_RABBITMQ_PASSWORD@rabbitmq.infrastructure.svc.cluster.local:5672`
  (or operator-supplied user/password). **No `***` characters in output.**
- Same check on `kubectl -n voip get secret voipbin ...`.
- Smoke-tail any `bin-*` pod log: no `ACCESS_REFUSED` AMQP errors.
- No destroy. ~10 min on top of the standard PR-G/PR-A dogfood window.

## 7. Checklist

- [x] Approaches (a) and (b) evaluated against actual `scripts/k8s.py:65-110`
      and `scripts/secret_schema.py:101-104, 148-151` source
- [x] (b) chosen — no new tokens, no `_build_substitution_map()` changes
- [x] PR-G merge dependency documented (§4) — PR-H merges first; conflict
      surface limited to context lines around `k8s.py:73`
- [x] pchero decision #4 (full-DSN sops parity) preserved via override-path
      test (§2.4 #7)
- [x] Substitution-ordering safety asserted (§2.4 #8)
- [x] YAML-literal drift guarded by schema-vs-rendered-yaml test (§2.4 #4)
- [x] LOC ≤ 150 (estimate: ~10 schema/YAML + ~110 test + ~30 doc/banner)
- [ ] Design review iter 1
- [ ] Design review iter 2
