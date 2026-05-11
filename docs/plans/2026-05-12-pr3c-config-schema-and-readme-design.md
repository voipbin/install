# PR #3c design: config schema + Terraform vars + verify + README rewrite

**Date:** 2026-05-12
**Author:** Hermes (CPO)
**Status:** Draft (iteration 2, addressing iter-1 review findings)
**Parent plan:** `docs/plans/2026-05-11-self-hosting-architecture-redesign.md`
**Prior PRs:** #9, #10, #11 (PR #3a), #12 (PR #3b)
**Branch:** `NOJIRA-PR3c-config-schema-and-readme`
**Target audience:** fresh self-hosting installers (no migration)

---

## 1. Goal

Close the redesign by cleaning up the user-facing surface that still
references the removed `ingress` / `cert-manager` topology, and ship
the production-readiness `verify` check that distinguishes the
fresh-install self-signed cert from an operator-supplied production
cert.

Concretely:
1. **`tls_strategy` enum reduction.** Drop `letsencrypt` and
   `gcp-managed` from the config schema, defaults, wizard, and
   Terraform validation. Remaining values: `self-signed` (default)
   and `byoc` (Bring Your Own Cert).
2. **`verify.check_tls_cert_is_production`.** New check that parses
   the cert in `voipbin-tls` Secret (per namespace) and returns:
   - `fail` if the cert is the installer-managed self-signed one (CN
     = `voipbin-self-signed`) AND `tls_strategy` is set to `self-signed`
     in `bin-manager` ns.
   - `warn` for `square-manager` ns under the same condition (Pod-level
     TLS via nginx sidecar; lower-impact but still flagged).
   - `pass` if the cert subject is anything other than the placeholder
     CN.
3. **README rewrite.** Remove all `ingress/`, `cert-manager`,
   `letsencrypt` references. New sections: per-Service LoadBalancer
   topology, 5 DNS A records, production cert replacement procedure,
   BYOC mode with explicit `kubectl create secret tls` commands for
   both `bin-manager` and `square-manager` namespaces (closes the
   PR #3b S5 follow-up — frontend Pods can't mount missing Secret in
   BYOC mode unless operator creates both copies).

### Decision lineage

- pchero (PR #11/#12 sessions): split PR #3 into 3a/3b/3c. 3a + 3b
  shipped infrastructure; 3c is the config-schema cleanup + docs.
- pchero (PR #12 iter 2 review): BYOC frontend Pod ContainerCreating
  is a real UX hole; closed by README guidance in 3c rather than
  install code change (operator can run `kubectl create secret tls`
  before `init`).

---

## 2. Non-goals (in PR #3c)

- No new Kubernetes manifests.
- No `scripts/tls_bootstrap.py` changes. BYOC mode already takes the
  `skipped-prefilled` branch and leaves `voipbin-tls` Secret creation
  to the operator (per PR #11 + PR #12 design).
- No production-parity workloads (number-renew, monitoring-tests,
  Prometheus/Grafana/Alertmanager/Heplify) — PR #4.
- No `cloudsql-proxy` removal — PR #5.
- No monorepo changes.

---

## 3. Changes

### 3.1 `config/schema.py`: reduce `tls_strategy` enum

```python
"tls_strategy": {
    "type": "string",
    "enum": ["self-signed", "byoc"],
    "description": "TLS certificate strategy",
},
```

### 3.2 `config/defaults.py`: trim `TLS_STRATEGIES`

```python
TLS_STRATEGIES = [
    {
        "id": "self-signed",
        "name": "Self-signed (installer-managed)",
        "note": "Fresh install ready out of the box; replace before production",
    },
    {
        "id": "byoc",
        "name": "Bring Your Own Cert",
        "note": "Provide cert/key via voipbin-secret + voipbin-tls Secrets",
    },
]
```

Default config in `scripts/config.py:87` becomes
`"tls_strategy": "self-signed"`.

### 3.3 `scripts/wizard.py`

Wizard option list shrinks automatically because it iterates
`TLS_STRATEGIES`. Default index logic continues to work (default=1).

### 3.4 `terraform/variables.tf`

```hcl
variable "tls_strategy" {
  description = "TLS certificate strategy: 'self-signed' (installer-managed bootstrap) or 'byoc' (operator provides cert via voipbin-secret + voipbin-tls Secrets)"
  type        = string
  default     = "self-signed"
  validation {
    condition     = contains(["self-signed", "byoc"], var.tls_strategy)
    error_message = "tls_strategy must be either 'self-signed' or 'byoc'."
  }
}
```

### 3.5 `tests/test_config.py` and `tests/test_terraform.py`

Update fixtures to use `tls_strategy: "self-signed"`. Add validation
tests for the new enum (rejects `letsencrypt`, `gcp-managed`).

### 3.6 `scripts/verify.py`: add `check_tls_cert_is_production`

```python
def check_tls_cert_is_production(
    namespaces: tuple[str, ...] = ("bin-manager", "square-manager"),
    tls_secret_name: str = "voipbin-tls",
    opaque_secret_name: str = "voipbin-secret",
    opaque_secret_namespace: str = "bin-manager",
    tls_strategy: str = "self-signed",
    placeholder_cn: str | None = None,
) -> dict:
    """Verify the active TLS cert chain is operator-supplied, not the
    installer-managed self-signed placeholder.

    Inspects TWO sources because production cert replacement is a
    two-secret procedure (PR #3a + PR #3b design):
      1. `voipbin-tls` Secret in each configured namespace (tls.crt
         data field). Consumed by frontend nginx sidecar (PR #3b)
         and forward-looking consumers.
      2. `voipbin-secret.SSL_CERT_BASE64` in `bin-manager` ns.
         Consumed by bin-api-manager / bin-hook-manager Go binaries
         as env-vars (PR #3a + PR #3b).

    `placeholder_cn` defaults to
    `scripts.tls_bootstrap.CN_PLACEHOLDER` (single source of truth —
    matches the constant the bootstrap function uses when generating
    the self-signed cert). Pass `None` to use the default.

    `tls_strategy` is read from `config.yaml` by the caller in
    `scripts/commands/verify.py` and forwarded here. It gates the
    severity of "Secret missing" outcomes:
      - `tls_strategy == "self-signed"`: missing voipbin-tls Secret →
        warn (bootstrap will create on next init).
      - `tls_strategy == "byoc"`: missing voipbin-tls Secret →
        fail (operator forgot to provision; production-not-ready).

    Returns per-source result. Top-level status:
      - `fail` if ANY cert (across both sources) has Subject CN
        equal to `placeholder_cn`. Reason: in production, ALL three
        Pod consumers must serve a real cert. A single placeholder
        leaks through anywhere → production-not-ready.
      - `fail` if any Secret is missing in BYOC mode (see above).
      - `warn` if any Secret is missing in self-signed mode, or
        cert is unparseable.
      - `pass` if all 3 certs parse and have non-placeholder CN.

    Implementation note: missing namespace (e.g., operator skipped
    PR #3b and `square-manager` was never created) → skip that source,
    not crash. The check is best-effort; missing infrastructure
    bubbles up via other checks.
    """
```

Severity rationale: `tls_bootstrap.py` writes the SAME cert pair to
all three sources (both `voipbin-tls` Secrets + `voipbin-secret` SSL
keys) in a single generation pass. So in normal `self-signed` mode
all three are the placeholder. But the production-cert-replacement
procedure (README §3.7) is a 3-step `kubectl` sequence — an operator
who replaces 2 of 3 but forgets the third will have one Pod set
serving real cert and the other serving placeholder. The check must
fail loudly so the operator does not ship to production in this
partial state. Per-namespace `warn`/`fail` distinction from iter 1
was incorrect and is collapsed to a single severity (`fail` on any
placeholder, full stop).

Wire into `run_all_checks` after `check_static_ips_reserved`. Wire
into `scripts/commands/verify.py` so `voipbin-install verify
--check=tls_cert_is_production` works.

### 3.7 README rewrite

**Remove all references to:** `ingress/`, `cert-manager`,
`letsencrypt`, `Let's Encrypt`, "ClusterIssuer".

**Add or update sections (in this order):**

1. **Architecture.** 5 per-Service GCP LoadBalancers, per-Service
   reserved static IP, no ingress controller, operator-managed DNS.
2. **TLS.** New section: installer-managed self-signed cert for
   bring-up; BYOC mode for production-ready bring-up.
3. **Quickstart.** 5 steps to a working install with the self-signed
   bootstrap (Terraform apply → init → apply → DNS records → first
   login). Cert replacement is documented as a separate "Before
   production traffic" section, not as a hard Quickstart step,
   because most operators will validate the install with the
   self-signed cert first.
4. **DNS Records.** 5 entries (api / hook / admin / talk / meet
   subdomains, all pointing at their reserved LB IPs).
5. **Production Cert Replacement.** Three-source procedure
   (voipbin-secret env keys, bin-manager voipbin-tls,
   square-manager voipbin-tls). Operator must update ALL THREE or
   `verify` will fail with mixed-state warning.

   ```bash
   # macOS note: 'base64 -w0' below is GNU. On macOS, replace with
   # 'base64 -i <file> | tr -d "\n"' or install coreutils.
   
   # 1. Place CA-issued cert and key files locally.
   #    e.g., /tmp/voipbin.crt + /tmp/voipbin.key
   
   # 2. Replace the bin-manager Pod-level cert (api-manager, hook-manager
   #    read cert/key from voipbin-secret as env-vars).
   kubectl -n bin-manager patch secret voipbin-secret \
     --type=merge \
     -p "{\"data\":{\"SSL_CERT_BASE64\":\"$(base64 -w0 /tmp/voipbin.crt)\",\"SSL_PRIVKEY_BASE64\":\"$(base64 -w0 /tmp/voipbin.key)\"}}"
   
   # 3. Replace the bin-manager voipbin-tls Secret (sidecar consumers).
   kubectl -n bin-manager create secret tls voipbin-tls \
     --cert=/tmp/voipbin.crt --key=/tmp/voipbin.key \
     --dry-run=client -o yaml | kubectl apply -f -
   
   # 4. Replace the square-manager voipbin-tls Secret (frontend nginx
   #    sidecars in admin/talk/meet).
   kubectl -n square-manager create secret tls voipbin-tls \
     --cert=/tmp/voipbin.crt --key=/tmp/voipbin.key \
     --dry-run=client -o yaml | kubectl apply -f -
   
   # 5. Roll the consumers so they pick up the new cert.
   kubectl -n bin-manager rollout restart deployment/api-manager deployment/hook-manager
   kubectl -n square-manager rollout restart deployment/admin deployment/talk deployment/meet
   
   # 6. Run verify to confirm.
   ./voipbin-install verify --check=tls_cert_is_production
   ```

6. **BYOC Mode (advanced).** Operator wants to skip the self-signed
   bootstrap entirely and serve a real cert from the first install.

   - Set `tls_strategy: byoc` in `config.yaml` before running `apply`.
   - Bootstrap detects populated `voipbin-secret.SSL_CERT_BASE64` and
     `SSL_PRIVKEY_BASE64` and skips all writes (atomic-pair contract,
     PR #3a §5.2.1). Operator is responsible for creating all 3
     Secret sources before the affected Pods reach Ready.
   - Procedure (each `apply --stage` invocation runs exactly ONE
     stage; APPLY_STAGES is
     `terraform_init → terraform_reconcile → terraform_apply →
     ansible_run → k8s_apply`):
     ```bash
     # 1. Run init to write config.yaml + secrets.yaml (no GKE apply yet).
     ./voipbin-install init
     # Edit config.yaml: set tls_strategy: byoc
     
     # 2. Provision GKE cluster + KMS (everything before k8s_apply).
     ./voipbin-install apply --stage terraform_init
     ./voipbin-install apply --stage terraform_reconcile
     ./voipbin-install apply --stage terraform_apply
     ./voipbin-install apply --stage ansible_run
     
     # 3. Create both namespaces.
     kubectl create namespace bin-manager
     kubectl create namespace square-manager
     
     # 4. Create voipbin-secret with operator-supplied SSL keys.
     #    The install-shipped k8s/backend/secret.yaml uses stringData
     #    with 6 non-SSL placeholder keys (JWT_KEY etc.). When
     #    k8s_apply runs in step 6, kubectl 3-way-merge preserves
     #    operator-set keys not declared in the manifest, so the
     #    SSL_*_BASE64 keys created here survive. (The 6 placeholder
     #    keys WILL be overwritten by the manifest; SOPS-decrypted
     #    real values for those keys come in via a later
     #    SOPS-aware substitution step in scripts/k8s.py, not from
     #    this manual Secret.)
     kubectl -n bin-manager create secret generic voipbin-secret \
       --from-literal=SSL_CERT_BASE64=$(base64 -w0 /tmp/your.crt) \
       --from-literal=SSL_PRIVKEY_BASE64=$(base64 -w0 /tmp/your.key)
     
     # 5. Create voipbin-tls Secret in BOTH namespaces.
     for ns in bin-manager square-manager; do
       kubectl -n $ns create secret tls voipbin-tls \
         --cert=/tmp/your.crt --key=/tmp/your.key
     done
     
     # 6. Continue install. Bootstrap detects populated SSL keys
     #    and skips its self-signed generation.
     ./voipbin-install apply --stage k8s_apply
     
     # 7. Verify production cert is in place across all 3 sources.
     ./voipbin-install verify --check=tls_cert_is_production
     ```
   - macOS note: `base64 -w0` is GNU only. On macOS use
     `base64 -i /tmp/your.crt | tr -d '\\n'` instead (or install
     coreutils via Homebrew).

7. **Verify.** New `tls_cert_is_production` check; expected output;
   how to interpret `warn` vs `fail`.

**Sections to remove:**
- Any mention of `k8s/ingress/` directory layout (removed in PR #3a).
- `cert-manager` cluster-issuer references (removed in PR #3a).
- `letsencrypt` / `gcp-managed` strategy descriptions.

### 3.8 `scripts/k8s.py:212` comment

`if tls_strategy != "byoc":` — keep the `!= "byoc"` form. With the
enum reduced to `["self-signed", "byoc"]`, the negation is equivalent
to `== "self-signed"`. Either form is correct; `!= "byoc"` is
retained because the bootstrap-vs-skip decision is semantically
"if NOT in BYOC mode, run the installer-managed bootstrap." Update
the inline comment to reference the new 2-value enum.

### 3.9 `scripts/k8s.py:187` docstring

Update to reference new enum values, no `letsencrypt`/`gcp-managed`.

---

## 4. Risks

| # | Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|---|
| 1 | Existing operator's `config.yaml` has `tls_strategy: letsencrypt` and breaks schema validation on next `init` | n/a | n/a | Fresh-installer-only repo (no migration). README + CHANGELOG note. |
| 2 | Terraform `validation` rejects existing tfvars that set `tls_strategy = "letsencrypt"` | n/a | n/a | Same — fresh-installer-only. README mentions Terraform variable change. |
| 3 | `check_tls_cert_is_production` parses cert incorrectly if cert is malformed | Low | Low | Use `cryptography.x509.load_pem_x509_certificate`; wrap in try/except. NotFound for Secret → `warn` ("voipbin-tls Secret missing in ns; BYOC mode operator must create"). |
| 4 | README BYOC procedure is intricate and error-prone for operators | Med | Med | Provide an explicit `voipbin-install byoc-prep` helper script? Defer: too much scope. README walks the operator through it. |
| 5 | Production cert replacement procedure misses an edge case (e.g., voipbin-secret merge-patch base64 quoting on macOS) | Low | Low | README uses portable `base64 -w0` (GNU) and notes the `base64 | tr -d '\n'` workaround for macOS BSD `base64`. |
| 6 | Removing `letsencrypt` enum value subtly breaks a dormant code path | Low | Med | `grep -rn "letsencrypt\|gcp-managed" .` in implementation step. Audit for stale references. |
| 7 | `verify.check_tls_cert_is_production` per-ns iteration may hit a missing namespace and crash | Low | Low | Defensive: treat ns NotFound as "skip" not "fail". |

---

## 5. Implementation order

**Step 1: Pre-edit audit.**
- `grep -rn "letsencrypt\|gcp-managed\|cert-manager" .` (excluding
  `__pycache__`, `.git`, `docs/plans/`). Catalog every reference;
  delete or update each.

**Step 2:** `config/schema.py` enum reduction + `config/defaults.py`
`TLS_STRATEGIES` trim + default `tls_strategy` flip to `self-signed`
in `scripts/config.py`.

**Step 3:** `terraform/variables.tf` validation update.

**Step 4:** `scripts/verify.py` `check_tls_cert_is_production` +
wiring into `run_all_checks` and `scripts/commands/verify.py`.

**Step 5:** Tests:
- `tests/test_config.py` + `tests/test_terraform.py` fixture updates.
- `tests/test_verify.py` add cases for `check_tls_cert_is_production`
  (cert with placeholder CN → fail/warn per ns; cert with real CN →
  pass; Secret NotFound → warn; cryptography parse error → warn).
- Schema validation test rejecting `letsencrypt`.

**Step 6:** README rewrite per §3.7.

**Step 7:** Sensitive-data audit (`scripts/dev/check-plan-sensitive.sh`)
for design doc + README.

**Step 8:** Full test suite + `voipbin-install init --dry-run` smoke
to ensure wizard renders the new TLS strategy menu.

---

## 6. Tests

| File | Test additions |
|---|---|
| `tests/test_config.py` | (1) accepts `self-signed`; (2) accepts `byoc`; (3) rejects `letsencrypt`; (4) rejects `gcp-managed`; (5) default is `self-signed`. |
| `tests/test_terraform.py` | (1) validation passes for `self-signed`; (2) for `byoc`; (3) fails for `letsencrypt`. |
| `tests/test_verify.py` | (1) all 3 sources have placeholder CN → fail; (2) all 3 have real CN → pass; (3) mixed (voipbin-tls real, voipbin-secret SSL placeholder) → fail; (4) Secret NotFound in any source → warn; (5) malformed PEM → warn; (6) missing namespace → skip (not crash); (7) CN_PLACEHOLDER constant matches scripts.tls_bootstrap.CN_PLACEHOLDER. |
| `tests/test_wizard.py` (if exists) | `TLS_STRATEGIES` list has exactly 2 entries. |

Target: 349 + ~12 ≈ 361 passing.

---

## 7. Open questions

1. ~~Should we keep `letsencrypt` as a deprecated-but-accepted value
   to ease later re-introduction?~~ **No.** Fresh-installer-only; no
   compat constraint. Re-introduce in a later PR when/if cert-manager
   is added back as an optional overlay.
2. ~~Should BYOC mode get a helper command `voipbin-install
   byoc-prep`?~~ **No, deferred.** README walks operator through it;
   helper command is a future PR if operator feedback demands it.

No open questions remain.

---

## 8. Review checklist

- [ ] `config/schema.py` enum is `["self-signed", "byoc"]`.
- [ ] `config/defaults.py` `TLS_STRATEGIES` has 2 entries.
- [ ] `scripts/config.py` default `tls_strategy` is `self-signed`.
- [ ] `terraform/variables.tf` validation accepts `self-signed` AND `byoc`, rejects `letsencrypt` AND `gcp-managed`.
- [ ] `scripts/verify.py` has `check_tls_cert_is_production` that inspects BOTH `voipbin-tls` Secrets AND `voipbin-secret.SSL_CERT_BASE64`.
- [ ] CN placeholder string is sourced from `scripts.tls_bootstrap.CN_PLACEHOLDER` (single source of truth).
- [ ] `verify` CLI exposes the new check via `--check=tls_cert_is_production`.
- [ ] README contains no `ingress`, `cert-manager`, `letsencrypt`, or `Let's Encrypt` references.
- [ ] README has 5-host DNS records section.
- [ ] README "Production Cert Replacement" covers all THREE sources with macOS base64 note.
- [ ] README BYOC walkthrough uses real CLI flags (no `--no-deploy`).
- [ ] README BYOC walkthrough enumerates all 5 stages
      (terraform_init, terraform_reconcile, terraform_apply,
      ansible_run, k8s_apply) — no skipped stages.
- [ ] README has been line-by-line audited for stale references to
      ingress/cert-manager/letsencrypt (not just keyword grep — every
      existing section reviewed).
- [ ] Production Cert Replacement (§3.7 step 5) prefers
      `kubectl create --dry-run | kubectl apply -f -` to preserve
      operator intent visibly; loss of bootstrap labels/annotations
      is acceptable for the replacement procedure (Secret is
      operator-managed after replacement, not bootstrap-managed).
- [ ] No PR #4/#5 scope leakage.
- [ ] No real production IDs, IPs, domain names, or Cloud SQL instance names anywhere.
- [ ] 361 tests passing.
