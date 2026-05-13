# PR-U-2 — HOMER Postgres database + user provisioning + substitution wiring

Status: Draft v3 (iter-1 + iter-2 fixes applied)
Author: Hermes (CPO)
Date: 2026-05-13
Worktree: `~/gitvoipbin/install/.worktrees/NOJIRA-PR-U-2-homer-db-provisioning`
Branch: `NOJIRA-PR-U-2-homer-db-provisioning`
Predecessor: PR-U-1 (merged at `60a2038`, k8s manifests + 2 forward-compat placeholders)
Successor: PR-U-3 (Kamailio heplify-client sidecar + HOMER_URI wiring, separate PR)

---

## 1. Problem statement

PR-U-1 deployed HOMER k8s manifests (heplify-server + homer-app) to the `infrastructure` namespace with placeholder DB env vars wired through `_build_substitution_map()` using a fallback chain that defaults to the `voipbin` user identifier and an empty password. The `voipbin` user does NOT exist in the CloudSQL Postgres instance (only the built-in `postgres` admin and the `bin-manager` application user exist today — see `terraform/cloudsql.tf:190` and `:211`). Even if it did, the `homer_data` and `homer_config` databases that heplify-server expects to migrate at first boot do NOT exist. Result: heplify-server container is in error state since PR-U-1 merged (intentional mid-state, PR-U-1 design §8).

PR-U-2 closes this gap by provisioning a dedicated HOMER Postgres role and the two HOMER databases on the existing CloudSQL Postgres instance, surfacing the password as a sensitive Terraform output, and wiring the real credentials into the existing `PLACEHOLDER_HOMER_DB_USER` / `PLACEHOLDER_HOMER_DB_PASS` substitution-map entries. After PR-U-2 merges, both containers in the heplify-deployment Pod (`heplify` + `homer-webapp`) should authenticate against Postgres, heplify-server should run its first-boot schema migration into `homer_data` / `homer_config`, and the Pod should reach Ready=2/2.

## 2. Goals (numbered, testable)

1. Add `homer` Postgres user to the existing CloudSQL Postgres instance via Terraform with a 24-char random password (Terraform-managed, distinct from `voipbin` MySQL admin and from `bin-manager` Postgres user).
2. Add `homer_data` and `homer_config` Postgres databases via Terraform on the same instance.
3. Expose `cloudsql_postgres_password_homer` as a sensitive Terraform output, mirroring the pattern at `terraform/outputs.tf:117` (`cloudsql_postgres_password_bin_manager`).
4. Replace the PR-U-1 forward-compat fallback chain in `_build_substitution_map()`:
   - `PLACEHOLDER_HOMER_DB_USER` resolves to the literal string `homer` (sourced from a constant, not a config override).
   - `PLACEHOLDER_HOMER_DB_PASS` resolves to `terraform_outputs["cloudsql_postgres_password_homer"]` with empty-string fallback for the case where the Terraform output is not yet harvested (preflight will catch the empty value before k8s_apply).
5. Add a preflight assertion that, after Terraform apply but before `k8s_apply`, the homer password is non-empty when HOMER manifests are present in `k8s/infrastructure/`. This prevents silent CrashLoop after rotation or import races.
6. Add invariant + functional tests covering: Terraform resource shape (user + 2 dbs + password output), substitution-map real-credential wiring, preflight assertion, and end-to-end placeholder-to-rendered-manifest substitution.
7. Keep the design strictly within DB provisioning + substitution wiring. Kamailio docker-compose `heplify-client` sidecar and `HOMER_URI` plumbing remain out of scope (PR-U-3).
8. After PR-U-2 merges and `voipbin-install apply` runs against the dogfood VM, `kubectl get pod -n infrastructure -l app=heplify-server` reports the `heplify-deployment` Pod as Ready=2/2 (both `heplify` and `homer-webapp` containers).

**Removed from v1:** the `terraform_reconcile.py` registry entries goal (former §2.4) and the reconcile-registry test class (former §7.2 `TestReconcileRegistry`). Rationale: iter-1 review confirmed `check_exists_in_gcp` at `scripts/terraform_reconcile.py:192-207` is rc-only and `gcloud sql users list` and `gcloud sql databases describe` interactions need careful treatment. Registering reconcile entries for HOMER resources would propagate the latent rc-only false-positive (or, for `sql databases describe`, would be fine since describe returns non-zero on absence). The conservative path: **do NOT register HOMER resources in the reconcile registry in PR-U-2**. For fresh installs, the Terraform-managed resources are created on first apply and the state persists thereafter — reconcile is only needed for existing-but-untracked installs, which is not the dogfood scenario. A future PR (separate from PR-U-3) may fix `check_exists_in_gcp` and add reconcile entries for all Postgres users uniformly. Documented as §Risks #R4.

## 3. Non-goals (explicit scope cuts)

- Kamailio heplify-client sidecar (PR-U-3).
- Kamailio `HOMER_URI` env var wiring through docker-compose (PR-U-3).
- HOMER ingress / public LB exposure. Internal LB only, kubectl port-forward access only, no public DNS. (Already locked in PR-U-1 §3.)
- HOMER auth/SSO integration. Default admin credentials remain `admin/sipcapture` until a future security PR.
- Switching heplify-server from inline `value:` env vars to `valueFrom.secretKeyRef`. Deferred — PR-U-1 reasoned the inline form matches the rest of the installer's substitution semantics. A future hardening PR may switch to k8s Secret references.
- Postgres binary log / point-in-time-recovery for HOMER tables. Dev tier defaults are sufficient.
- `terraform_reconcile.py` registry entries for HOMER resources (deferred to a future PR that also fixes `check_exists_in_gcp` rc-only heuristic).

## 4. Affected files (table: file → why)

| File | Why | Change type |
|---|---|---|
| `terraform/cloudsql.tf` | Add `random_password.postgres_homer`, `google_sql_database.voipbin_postgres_homer_data`, `google_sql_database.voipbin_postgres_homer_config`, `google_sql_user.voipbin_postgres_homer` | append (lines after L215) |
| `terraform/outputs.tf` | Add `cloudsql_postgres_password_homer` sensitive output | append (after L120) |
| `scripts/k8s.py` | Rewrite `PLACEHOLDER_HOMER_DB_USER` and `PLACEHOLDER_HOMER_DB_PASS` resolution in `_build_substitution_map()` (currently at L199-L217). Drop fallback chain that pointed at `voipbin`; resolve to literal `"homer"` + Terraform output | modify (replace ~18 lines) |
| `scripts/preflight.py` | Add public top-level `check_homer_credentials_present()` matching existing `check_cloudsql_private_ip` idiom. Register call site in the preflight orchestration function | modify |
| `tests/test_pr_u_2_homer_db_provisioning.py` | New test file: Terraform-shape invariants, substitution-map wiring, preflight assertion, end-to-end render check | new |
| `tests/test_pr_d2a_cloudsql_resources.py` | Mechanical bump: `TestSensitiveOutputs.EXPECTED` dict (L262-273) — add `cloudsql_postgres_password_homer` entry | modify (1 line) |
| `docs/plans/2026-05-13-pr-u-2-homer-db-provisioning-design.md` | This file | new |

Estimated diff: ~200 LOC added, ~25 LOC modified across 7 files (1 doc + 4 code + 2 tests). Approximately 40% smaller than PR-U-1.

## 5. Exact string replacements / API changes

### 5.1 Terraform: `terraform/cloudsql.tf` — append after line 215

```hcl
# -- Postgres HOMER user, databases (PR-U-2) ----------------------------------
# HOMER (heplify-server + homer-app) uses TWO Postgres databases on the shared
# CloudSQL Postgres instance:
#   - homer_data: capture rows (SIP packets, RTCP, logs) written by heplify-server
#   - homer_config: dashboards, alarms, users, settings managed by homer-app
# A dedicated `homer` Postgres user owns both. Distinct from `bin-manager` so
# credentials can be rotated independently and audit logs distinguish HOMER
# writes from application writes.

resource "random_password" "postgres_homer" {
  length           = 24
  special          = true
  override_special = "!*+-._~"
}

resource "google_sql_database" "voipbin_postgres_homer_data" {
  name      = "homer_data"
  instance  = google_sql_database_instance.voipbin_postgres.name
  charset   = "UTF8"
  collation = "en_US.UTF8"
}

resource "google_sql_database" "voipbin_postgres_homer_config" {
  name      = "homer_config"
  instance  = google_sql_database_instance.voipbin_postgres.name
  charset   = "UTF8"
  collation = "en_US.UTF8"
}

resource "google_sql_user" "voipbin_postgres_homer" {
  name     = "homer"
  instance = google_sql_database_instance.voipbin_postgres.name
  password = random_password.postgres_homer.result
}
```

### 5.2 Terraform: `terraform/outputs.tf` — append after line 120

```hcl
output "cloudsql_postgres_password_homer" {
  description = "Random password for the homer Postgres application user (heplify-server + homer-app)."
  value       = random_password.postgres_homer.result
  sensitive   = true
}
```

### 5.3 Postgres CREATE-privilege resolution (homer user permissions)

In Cloud SQL Postgres, `google_sql_user` resources are granted the `cloudsqlsuperuser` role by default. The `cloudsqlsuperuser` role inherits `CREATE` privilege on databases and `CREATE ON SCHEMA public` for any database it can connect to. Reference: GCP doc "Postgres users and roles" — `cloudsqlsuperuser` is granted to all `google_sql_user`-created users automatically. This means the `homer` user CAN run DDL inside `homer_data` and `homer_config` for first-boot migration without a separate GRANT.

Confirmation via existing precedent: `bin-manager` Postgres user (terraform/cloudsql.tf:211, output L117) runs Go-framework migrations on `bin_manager` DB successfully in production today using exactly this pattern (no manual GRANT). The heplify-server `dbrotate`/auto-migration is functionally equivalent (DDL via `cloudsqlsuperuser` privileges).

**Decision:** No separate GRANT script needed. If first dogfood proves otherwise (heplify logs show `permission denied for database`), document a one-shot recovery in §8 §Risks #R5 below.

### 5.4 `scripts/k8s.py` — rewrite L199-L217

Replace the entire PR-U-1 commented block + fallback chain with:

```python
        # PR-U-2: HOMER (heplify-server + homer-app) database credentials.
        # heplify-server writes capture rows to `homer_data`; homer-app reads
        # dashboards/config from `homer_config`. Both DBs live on the existing
        # CloudSQL Postgres instance (terraform/cloudsql.tf:145), owned by a
        # dedicated `homer` Postgres user provisioned by Terraform. The user
        # name is the literal string "homer" (PR-U-2 locked decision); the
        # password is the Terraform output `cloudsql_postgres_password_homer`,
        # harvested into terraform_outputs by reconcile_outputs.
        #
        # Preflight (scripts/preflight.py:check_homer_credentials_present)
        # asserts the password is non-empty when k8s/infrastructure/homer/
        # exists, so an empty value here cannot silently CrashLoop a freshly-
        # applied Pod.
        "PLACEHOLDER_HOMER_DB_USER": "homer",
        "PLACEHOLDER_HOMER_DB_PASS": terraform_outputs.get(
            "cloudsql_postgres_password_homer", ""
        ),
```

Rationale for hard-coding the user as `"homer"`:
- Symmetric with how the rest of the substitution map treats fixed identifiers (DB names like `homer_data` are baked into the manifest, not config-driven).
- Operator override is not useful — the Terraform resource creates user `homer`. Allowing a config override creates a foot-gun where the config says `heplify` but Terraform created `homer`.
- A future hardening PR adding `valueFrom.secretKeyRef` would move the username into a k8s Secret key, retiring this constant.

### 5.5 `scripts/preflight.py` — add public `check_homer_credentials_present()`

Match the existing idiom proven by `check_loadbalancer_addresses` at `scripts/preflight.py:383`: top-level public function taking `terraform_outputs: dict[str, str]` directly (NOT a fabricated `PipelineState` type — verified: no such class exists in pipeline.py or preflight.py). The check raises `PreflightError` on failure.

```python
# scripts/preflight.py — append after check_loadbalancer_addresses (~L403)

# Standalone import to avoid pulling all of scripts/k8s.py at preflight time.
# K8S_DIR semantics MUST match the K8S_DIR constant in scripts/k8s.py:16.
# Using a local Path computation here keeps preflight.py independent of k8s.py
# (preventing a cyclic import — k8s.py already imports preflight for the
# LoadBalancer check at k8s.py:352-355).
_K8S_DIR = Path(__file__).resolve().parent.parent / "k8s"


def check_homer_credentials_present(terraform_outputs: dict[str, str]) -> None:
    """PR-U-2: assert HOMER Postgres password is harvested before k8s_apply.

    The HOMER manifest set under k8s/infrastructure/homer/ embeds the password
    via PLACEHOLDER_HOMER_DB_PASS substitution. An empty password renders as
    an empty DSN, heplify-server crashes on Postgres connect, and the Pod
    enters CrashLoopBackOff. This check makes the failure explicit at preflight
    instead of silent at apply.

    No-op when k8s/infrastructure/homer/ is absent (custom install profiles
    may exclude HOMER).
    """
    homer_dir = _K8S_DIR / "infrastructure" / "homer"
    if not homer_dir.exists():
        return
    pw = terraform_outputs.get("cloudsql_postgres_password_homer", "")
    if not pw:
        raise PreflightError(
            "HOMER Postgres password is empty in terraform_outputs. "
            "Run `voipbin-install apply --stage reconcile_outputs` to harvest "
            "it from Terraform, or confirm `terraform apply` succeeded for "
            "google_sql_user.voipbin_postgres_homer."
        )
```

**Orchestration call site (locked):** Add the call inside `scripts/k8s.py:k8s_apply()` immediately after the existing `check_loadbalancer_addresses` block (around L373, after `if missing: ... return False`). Pattern:

```python
# scripts/k8s.py:k8s_apply — modify the existing preflight import + add call
from scripts.preflight import (
    check_homer_credentials_present,
    check_loadbalancer_addresses,
    check_nodeport_availability,
)
# ... existing check_loadbalancer_addresses block ...
# After LB check, before manifest substitution & kubectl apply:
try:
    check_homer_credentials_present(terraform_outputs)
except PreflightError as exc:
    print_error(str(exc))
    return False
```

This places the check **after `reconcile_outputs` has populated `terraform_outputs`** (because `pipeline.py:APPLY_STAGES` runs `reconcile_outputs` immediately before `k8s_apply`, see L42-L43) and **before any manifest substitution happens** inside `k8s_apply`. Different temporal slot from `check_cloudsql_private_ip` (which runs from `diagnosis.py:run_pre_apply_checks` BEFORE Terraform).

The `PreflightError` import in `k8s.py` (already present at L353-L355 area? — verify at impl time; if absent, add to the same import block).

### 5.6 Wire-field checklist — actual env block enumeration

Sourced from on-disk `k8s/infrastructure/homer/deployment.yaml` (PR-U-1 shipped).

**Container 1: `heplify` (sipcapture/heplify-server:1.30)**, env block L25-L70:

| Env var | Value source | Substituted? |
|---|---|---|
| `HEPLIFYSERVER_DBADDR` | `PLACEHOLDER_CLOUDSQL_POSTGRES_PRIVATE_IP:5432` | yes (PR-D1 token) |
| `HEPLIFYSERVER_DBDRIVER` | `postgres` | no, literal |
| `HEPLIFYSERVER_DBSHEMA` | `homer7` | no, literal **(typo is intentional — upstream binary reads `DBSHEMA` not `DBSCHEMA`)** |
| `HEPLIFYSERVER_DBDROPDAYS` | `"7"` | no, literal |
| `HEPLIFYSERVER_DBUSER` | `PLACEHOLDER_HOMER_DB_USER` | **PR-U-2 wires** → `homer` |
| `HEPLIFYSERVER_DBPASS` | `PLACEHOLDER_HOMER_DB_PASS` | **PR-U-2 wires** → TF output |
| `HEPLIFYSERVER_DBDATATABLE` | `homer_data` | no, literal |
| `HEPLIFYSERVER_DBCONFTABLE` | `homer_config` | no, literal |
| `HEPLIFYSERVER_ESADDR` | (empty) | no, literal |
| `HEPLIFYSERVER_CONFIGHTTPADDR` | `0.0.0.0:9090` | no, literal |
| `HEPLIFYSERVER_HEPADDR` | `0.0.0.0:9060` | no, literal |
| `HEPLIFYSERVER_HEPTCPADDR` | `0.0.0.0:9060` | no, literal |
| `HEPLIFYSERVER_HEPTLSADDR` | `0.0.0.0:9061` | no, literal |
| `HEPLIFYSERVER_LOGLVL` | `error` | no, literal |
| `HEPLIFYSERVER_LOGSTD` | `"true"` | no, literal |
| `HEPLIFYSERVER_PROMADDR` | `0.0.0.0:9096` | no, literal |
| `HEPLIFYSERVER_PROMTARGETIP` | (empty) | no, literal |
| `HEPLIFYSERVER_PROMTARGETNAME` | (empty) | no, literal |

**Container 2: `homer-webapp` (pchero/homer-app:0.0.4)**, env block L92-L102:

| Env var | Value source | Substituted? |
|---|---|---|
| `DB_HOST` | `PLACEHOLDER_CLOUDSQL_POSTGRES_PRIVATE_IP` | yes (PR-D1 token) |
| `DB_USER` | `PLACEHOLDER_HOMER_DB_USER` | **PR-U-2 wires** → `homer` |
| `DB_PASS` | `PLACEHOLDER_HOMER_DB_PASS` | **PR-U-2 wires** → TF output |
| `HOMER_ENABLE_API` | `"true"` | no, literal |
| `HOMER_LOGLEVEL` | `debug` | no, literal |

**Verification:** `grep -n PLACEHOLDER_HOMER_DB k8s/infrastructure/homer/deployment.yaml` returns exactly 4 hits (heplify L46/L48 + homer-webapp L96/L98). PR-U-2 wires both occurrences of USER and both of PASS through one substitution-map entry each.

**Gaps identified by enumeration:**
- homer-webapp env block is missing a `DB_NAME` / `DB_CONFIG_DATABASE` style entry. The upstream `homer-app/0.0.4` defaults to `homer_config` for the config database (verified separately — common in HOMER 7.x). If first dogfood proves the default is different, the manifest needs an additional env var. Documented in §Risks #R6.
- heplify env has no `HEPLIFYSERVER_DBSSLMODE`. Cloud SQL private IP path supports `sslmode=disable` by default and heplify-server's Postgres driver defaults to `disable` when not specified. Production voip-homer manifest matches this absence.

### 5.7 Producer→consumer trace table (skill-mandated)

| Producer change | Consumer file | Consumer read path | Verification |
|---|---|---|---|
| `terraform/outputs.tf` adds `cloudsql_postgres_password_homer` (sensitive) | `scripts/k8s.py:_build_substitution_map()` reads via `terraform_outputs.get("cloudsql_postgres_password_homer", "")` | substitution at apply time | `grep -n cloudsql_postgres_password_homer scripts/` returns 1 hit in k8s.py + 1 hit in preflight.py post-PR |
| `scripts/k8s.py` rewrites `PLACEHOLDER_HOMER_DB_PASS` resolution | `k8s/infrastructure/homer/deployment.yaml` L48 (heplify) + L98 (homer-webapp) contain the literal token | apply-time text substitution | `grep PLACEHOLDER_HOMER_DB_PASS k8s/` returns 2 hits (heplify + homer-webapp) |
| `scripts/k8s.py` rewrites `PLACEHOLDER_HOMER_DB_USER` resolution | `k8s/infrastructure/homer/deployment.yaml` L46 (heplify) + L96 (homer-webapp) contain the literal token | apply-time text substitution | `grep PLACEHOLDER_HOMER_DB_USER k8s/` returns 2 hits |
| `terraform/cloudsql.tf` adds 4 resources (1 password + 2 dbs + 1 user) | Terraform state | `terraform apply` provisions; reconcile_outputs harvests password | `grep -n postgres_homer terraform/` returns 4 hits across cloudsql.tf + outputs.tf |
| `scripts/preflight.py` adds `check_homer_credentials_present` | called by preflight orchestration before `k8s_apply` stage | raises `PreflightError` on empty password | unit test instantiates state with empty `terraform_outputs` and HOMER dir present, asserts raise |

No dead defaults. Every producer change has a consumer read path. Both substitution tokens already live in the manifest file (shipped by PR-U-1, 4 occurrences total); PR-U-2 only swaps the value behind them.

## 6. Copy/decision rationale

- **User name `homer` (not `heplify`):** matches production voip-homer convention, distinguishes the *role* (HOMER Postgres consumer) from the *components* (heplify-server + homer-app share the same DB user).
- **Two databases vs one:** matches heplify-server defaults (`HEPLIFYSERVER_DBDATATABLE=homer_data` / `HEPLIFYSERVER_DBCONFTABLE=homer_config`). Could consolidate to one with table prefixes, but separation gives cleaner backup boundaries.
- **No `valueFrom.secretKeyRef`:** PR-U-1 lock. Inline `value:` substitution matches the installer's existing substitution semantics. Hardening PR can flip later.
- **Sensitive output, not config persistence:** `TestSensitiveOutputs` (in `tests/test_pr_d2a_cloudsql_resources.py`) enforces the discipline; PR-U-2 adds one more entry to the expected dict.
- **Preflight check is directory-existence-gated:** install configurations that exclude HOMER (future minimal-install option) should not trip the preflight.
- **No reconcile registry entries:** existing `check_exists_in_gcp` is rc-only (confirmed at `scripts/terraform_reconcile.py:192-207`); `gcloud sql users list` always returns rc=0 even when the user is absent. Adding HOMER user entries would propagate this latent false-positive. Conservative: defer until a future PR fixes the heuristic uniformly across all Postgres user entries.

## 7. Verification plan

### 7.1 Static checks (pre-commit)

1. `python -m pytest tests/ -q` — full suite green (expect 734 + 19 new = ~753).
2. `bash scripts/dev/check-plan-sensitive.sh docs/plans/2026-05-13-pr-u-2-homer-db-provisioning-design.md` — sensitive scan PASS.
3. `cd terraform && terraform fmt -check` — no fmt diffs.
4. `cd terraform && terraform validate` — Terraform syntax OK (use `-backend=false` for CI-style validation if backend creds unavailable).
5. `grep -rn 'PLACEHOLDER_HOMER_DB_' .` — exactly 2 substitution-map hits in `scripts/k8s.py` + 4 manifest hits in `k8s/infrastructure/homer/deployment.yaml` + design doc references.

### 7.2 Test enumeration (new file `tests/test_pr_u_2_homer_db_provisioning.py`)

| Class | Tests | Purpose |
|---|---|---|
| `TestTerraformShape` | 4 | cloudsql.tf contains the 4 new resources by exact name and address |
| `TestTerraformOutputs` | 2 | outputs.tf contains `cloudsql_postgres_password_homer` block AND it is `sensitive = true` |
| `TestSubstitutionMapWiring` | 3 | `PLACEHOLDER_HOMER_DB_USER` returns literal `"homer"`; `PLACEHOLDER_HOMER_DB_PASS` returns Terraform output when present; returns empty string when absent (preflight will catch) |
| `TestPreflightGate` | 3 | Empty password + HOMER dir present → raises `PreflightError`; non-empty password + HOMER dir present → passes; empty password + HOMER dir absent → passes (no-op) |
| `TestPreflightRegistration` | 1 | Read `scripts/k8s.py:k8s_apply` source; assert it imports `check_homer_credentials_present` AND invokes it after the `check_loadbalancer_addresses` block. Source-grep test, no runtime mocking |
| `TestSensitiveOutputsGuard` | 1 | `TestSensitiveOutputs.EXPECTED` (in test_pr_d2a) still rejects writing `cloudsql_postgres_password_homer` to config.yaml via FIELD_MAP (cross-file integration check) |
| `TestPlaceholderInvariantHolds` | 1 | After PR-U-2 substitution rewrite, `test_all_placeholder_tokens_resolved` still finds zero unresolved tokens |
| `TestEndToEndRender` | 4 | **LOCKED: real on-disk `k8s/infrastructure/homer/deployment.yaml` fixture** (not in-memory). After substitution via `_build_substitution_map`, rendered file: (a) contains `value: homer` in heplify env DBUSER, (b) contains `value: homer` in homer-webapp DBUSER, (c) DBPASS substituted to fixture password, (d) no literal `PLACEHOLDER_HOMER_*` remains |

Total: 19 new tests. Combined with PR-U-1's 15 → cumulative HOMER test coverage 34 cases.

### 7.3 Mutant-injection harness

Standard pattern (file-backup based revert, per skill). 15 mutants minimum, target ≥12/15 catch. **Production-file restore equality assert is non-negotiable** (lessons from skill: PR-D2b nuked WIP via `git checkout` — never use git as revert mechanism):

| # | Mutation | Expected catcher |
|---|---|---|
| 1 | rename TF resource `voipbin_postgres_homer` → `voipbin_postgres_homerX` | TestTerraformShape |
| 2 | drop `sensitive = true` from output | TestTerraformOutputs |
| 3 | remove `google_sql_database.voipbin_postgres_homer_data` resource | TestTerraformShape |
| 4 | remove `google_sql_database.voipbin_postgres_homer_config` resource | TestTerraformShape |
| 5 | typo `homer_data` → `homer_dta` in TF database `name` field | TestTerraformShape (exact-name regex) |
| 6 | rename `random_password.postgres_homer` → `postgres_homerXX` | TestTerraformShape |
| 7 | hard-code `PLACEHOLDER_HOMER_DB_USER` to `"voipbin"` in k8s.py | TestSubstitutionMapWiring |
| 8 | resolve `PLACEHOLDER_HOMER_DB_PASS` from wrong TF key (typo) | TestSubstitutionMapWiring |
| 9 | invert preflight raise → swallow exception | TestPreflightGate |
| 10 | remove HOMER-dir gate in preflight (always raise) | TestPreflightGate |
| 11 | drop `cloudsql_postgres_password_homer` from `TestSensitiveOutputs.EXPECTED` dict (security regression — output stops being asserted as sensitive) | TestSensitiveOutputsGuard or full-suite |
| 12 | replace `terraform_outputs.get("...")` with `""` literal | TestSubstitutionMapWiring |
| 13 | rename TF user `homer` → `heplify` in `google_sql_user.voipbin_postgres_homer.name` | TestTerraformShape + TestEndToEndRender (k8s.py still says "homer", mismatches) |
| 14 | rename TF output `cloudsql_postgres_password_homer` → `cloudsql_postgres_password_homerX` | TestTerraformOutputs |
| 15 | drop the `check_homer_credentials_present(terraform_outputs)` call from `scripts/k8s.py:k8s_apply` orchestration | TestPreflightRegistration (source-grep finds invocation missing) |

Acceptance gate: ≥12/15 caught; restore equality assert passes; production files identical post-harness.

### 7.4 Dogfood-readiness check (post-merge, not in PR gate)

After PR-U-2 merges and a real `voipbin-install apply` runs:
1. `terraform plan` shows 4 new resources created, 0 destroyed.
2. After apply: `gcloud sql databases list --instance=voipbin-install-dev-postgres --project=voipbin-install-dev` includes `homer_data` and `homer_config`.
3. `gcloud sql users list --instance=voipbin-install-dev-postgres --project=voipbin-install-dev` includes `homer`.
4. `kubectl get pod -n infrastructure -l app=heplify-server` → Pod reaches Ready=2/2 within 90s (heplify schema migration + homer-webapp boot).
5. `kubectl logs -n infrastructure deploy/heplify-deployment -c heplify` should NOT contain `pq: password authentication failed` or `permission denied for database`.
6. `kubectl port-forward -n infrastructure svc/homer-app 9080:80` + browser → homer-app login page renders.

These are dogfood-phase observations, NOT PR-merge gates.

## 8. Rollout / risk

| # | Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|---|
| R1 | Terraform apply order: user-before-database creates a transient inconsistency | Low | one apply may re-run | Terraform dependency graph handles automatically; `random_password` → `database` → `user` is the typical ordering |
| R2 | `cloudsql_postgres_password_homer` is harvested but not propagated to k8s_apply because `reconcile_outputs` ran before this PR's TF code landed | Low | preflight catches explicitly | preflight check raises with operator-friendly message instructing `--stage reconcile_outputs` |
| R3 | heplify-server's auto-migration silently fails | Low | empty `homer_data` table, no captures stored | Dogfood: `kubectl logs` shows migration progress; fallback is to run upstream migration SQL one-shot via `gcloud sql import` |
| R4 | Existing fresh installs that need `terraform import` for HOMER resources (e.g. operator manually created them) will not be auto-imported | Expected | requires manual `terraform import` per resource | Documented in operations runbook; affects no dogfood scenario |
| R5 | `cloudsqlsuperuser` role inheritance assumption is wrong on some Cloud SQL Postgres version | Low | heplify migration fails with `permission denied` | One-shot recovery: `gcloud sql connect voipbin-install-dev-postgres --user=postgres` then `GRANT ALL ON DATABASE homer_data, homer_config TO homer;`. Documented in dogfood runbook |
| R6 | homer-app/0.0.4 default for the config DB is not `homer_config` | Low | homer-app fails to find dashboards table | First dogfood reveals; fix is to add `DB_NAME=homer_config` env var to homer-webapp container (PR-U-2 hotfix or follow-up PR) |
| R7 | Password rotation requires Pod restart | Expected | brief downtime | Standard pattern; documented in operations runbook |
| R8 | Cross-PR drift: PR-U-3 (Kamailio sidecar) merges before PR-U-2's dogfood completes | Expected | irrelevant — PR-U-3 only touches Kamailio docker-compose | Sequence merges: PR-U-2 first, dogfood, then PR-U-3 |

## 9. Open questions (for iter-3 reviewer, if any)

All iter-2 blockers (B1 PipelineState, B2 K8S_DIR, B3 mutant-#15 catcher, B4 EndToEnd fixture) are resolved in v3. No remaining open questions.

## 10. Approval status

- [x] Draft v1 written 2026-05-13
- [x] Iter-1 design review completed (7 REAL findings)
- [x] v2 fixes applied 2026-05-13
- [x] Iter-2 design review completed (4 REAL findings)
- [x] v3 fixes applied 2026-05-13
- [ ] APPROVED (pending min-2 satisfied; ready for implementation)

---

## 11. Iter-1 review response summary (v1 → v2)

| Iter-1 issue | Resolved in v2 | Section |
|---|---|---|
| 1. `gcloud sql users list` rc-only false-positive | Dropped reconcile registry entries entirely (former §2.4 / §5.3 / TestReconcileRegistry) — deferred to a future PR that fixes `check_exists_in_gcp` uniformly | §2 (former §2.4 removed), §3 Non-goals (+1 line), §6 (+rationale line), §4 (file table reduced), §Risks R4 |
| 2. Ready=1/2 → Ready=1/1 contradiction (actual: 2/2 since 2 containers) | Corrected: Pod reaches Ready=2/2 (heplify + homer-webapp containers) | §1 (last sentence), §2.8, §7.4 step 4 |
| 3. Preflight function `_check_*` → public `check_*` | Renamed to `check_homer_credentials_present`; idiom now matches existing `check_cloudsql_private_ip` | §5.5 |
| 4. Non-existent `FIELD_MAP_PASSWORD_GUARD` identifier | Replaced with real `TestSensitiveOutputs.EXPECTED` at `tests/test_pr_d2a_cloudsql_resources.py:262-273` | §4, §7.2 |
| 5. Wire-field checklist not actually cross-checked | Enumerated both containers' env blocks line-by-line from deployment.yaml; identified gaps (R6 DB_NAME), confirmed PLACEHOLDER tokens count (4 hits) | §5.6 (now complete) |
| 6. CREATE/migration permission unresolved | Documented `cloudsqlsuperuser` inheritance + bin-manager precedent; recovery script in R5 | §5.3, §Risks R5 |
| 7. Test count arithmetic 734+15 vs 18 new | Corrected to 734 + 18 = 752 | §7.1 step 1 |

All 7 iter-1 findings addressed. No silent rejections.

## 12. Iter-2 review response summary (v2 → v3)

| Iter-2 issue | Resolved in v3 | Section |
|---|---|---|
| B1. Fabricated `PipelineState` type | Replaced with `terraform_outputs: dict[str, str]` signature matching `check_loadbalancer_addresses` at preflight.py:383 | §5.5 code block, §7.2 TestPreflightGate |
| B2. `K8S_DIR` not available in preflight.py | Added local `_K8S_DIR = Path(__file__).resolve().parent.parent / "k8s"` constant (avoids cyclic import — k8s.py already imports preflight) | §5.5 code block + comment |
| B3. Mutant #15 had no test | Added `TestPreflightRegistration` (1 case, source-grep) to §7.2; bumped total tests 18→19; updated §7.1 arithmetic 752→753; mutant #15 entry now cites the concrete catcher | §7.2, §7.3 row 15, §7.1 step 1 |
| B4. EndToEnd substitution mechanism unlocked | LOCKED to real on-disk fixture `k8s/infrastructure/homer/deployment.yaml`; §9 Q3 resolved | §7.2 TestEndToEndRender, §9 (closed) |

All 4 iter-2 findings addressed. No silent rejections.

---

## Decisions locked 2026-05-13 (carried from PR-U-1 + new)

- HOMER Postgres user name: `homer` (hard-coded constant, no config override).
- Two databases: `homer_data` + `homer_config` (not consolidated).
- Inline `value:` substitution (no k8s Secret), per PR-U-1 lock.
- Sensitive Terraform output (`cloudsql_postgres_password_homer`), not config.yaml.
- Preflight gate: HOMER-dir-presence-gated password non-empty assertion.
- Reuse existing Postgres instance (no new instance).
- **No reconcile registry entries** for HOMER resources (defer until `check_exists_in_gcp` is fixed for `sql users list` rc-only edge).
