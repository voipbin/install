# PR-AD: TLS Operator Guide + ACME Workaround

**Status:** DRAFT v3 (R2 CHANGES_REQUESTED 반영)
**Author:** Hermes (CPO)
**Date:** 2026-05-14
**Branch:** `NOJIRA-PR-AD-tls-operator-guide`

---

## 1. Background

The original PR-AD carry-forward item was "non-Kamailio TLS (admin SPA, talk,
meet via shared CA)." The intent was to automatically distribute the
installer-managed Kamailio CA cert to the frontend workloads.

**Decision (2026-05-14):** TLS certificate issuance and domain configuration
is the operator's responsibility. The installer will not automate ACME or
distribute CA certs to non-Kamailio services. Instead:

1. README gets a concrete "Obtaining TLS Certificates" section guiding
   operators to use Let's Encrypt (Certbot) or any other CA, then connect
   the result via the existing `byoc` / `manual` paths.
2. Internal code references to "PR-AC" and "ACME mode reserved" are cleaned
   up to reflect the final decision: ACME is out of scope indefinitely.
3. No installer logic changes for frontend TLS — the existing `self-signed`
   placeholder + `byoc` upgrade path already handles the workaround
   adequately. The README makes this explicit.

---

## 2. Scope

### In scope

**A. README.md — new section "Obtaining TLS Certificates"**

Add a new top-level section between "TLS Strategy" and "Image Policy" covering:
- Two-system overview (frontend vs Kamailio — independent cert systems)
- Let's Encrypt via Certbot: standalone HTTP challenge, DNS challenge
- Generic CA-issued cert (any provider)
- How to feed the result into VoIPBin: `byoc` mode or "Production Cert Replacement"
- Kamailio-specific note: `cert export-ca` + `cert_mode: manual`
- DNS prerequisite warning for Certbot standalone mode

**B. README.md — update "TLS Strategy" section**

Add a note: automated ACME/Let's Encrypt renewal is not handled by the
installer; operators manage cert lifecycle externally.

**C. README.md — add `cert` command section under "Commands"**

The `cert` subcommand group (status, renew, export-ca, clean-staging) was
added in PR-AA but is undocumented in the README. Add a brief `cert` section
so that the internal link `#cert----manage-kamailio-tls-certificates`
resolves correctly.

**D. config/schema.py — cert_mode description cleanup**

Remove "ACME mode is reserved for PR-AC and is not yet supported." Replace
with: "To use a CA-issued cert for Kamailio, set cert_mode=manual and supply
certs via cert_manual_dir. ACME is not supported."

**E. scripts/cert_lifecycle.py — ACME error message + docstring cleanup**

Three locations:

1. Error message (line ~389): change from
   `"cert_mode=acme requires PR-AC; for now use self_signed or manual"`
   to
   `"cert_mode=acme is not supported. Use cert_mode=self_signed or cert_mode=manual. For CA-issued certs, see 'Obtaining TLS Certificates' in README.md."`

2. `CertLifecycleError` class docstring (line ~52): remove
   "ACME requested before PR-AC" from the examples list.

3. `seed_kamailio_certs` docstring (line ~378): change
   "Raises CertLifecycleError for: ACME mode (PR-AC), ..." to
   "Raises CertLifecycleError for: ACME mode (not supported), ..."

**F. scripts/config.py — validate() ACME block cleanup**

`InstallerConfig.validate()` contains a hardcoded special-case for
`cert_mode == "acme"` (lines ~100-112) that pre-empts jsonschema and
produces a bespoke error message containing "PR-AC". This is the source of
the `"PR-AC" in joined` assertion in `test_pr_z_config_schema.py`.

Update two locations in `config.py`:

1. Comment (line ~102): change
   `"cert_mode=acme with a clearer hint pointing at PR-AC."` to
   `"cert_mode=acme with a clear error message (ACME is not supported)."`

2. Error message (lines ~104-108): change from
   ```python
   "cert_mode=acme is not yet supported. The ACME / Let's "
   "Encrypt path is tracked in PR-AC. For now use "
   "cert_mode=self_signed (default) or cert_mode=manual."
   ```
   to
   ```python
   "cert_mode=acme is not supported. "
   "Use cert_mode=self_signed (default) or cert_mode=manual. "
   "For CA-issued certs, see 'Obtaining TLS Certificates' in README.md."
   ```

**G. tests/test_pr_z_cert_lifecycle.py — update ACME test**

`TestAcmeRejection::test_acme_raises_with_pr_ac_mention`:
- Rename to `test_acme_mode_raises`
- Change `match="PR-AC"` to `match="not supported"`

**H. tests/test_pr_z_config_schema.py — update ACME schema test**

`test_rejects_acme_with_pr_ac_hint`:
- Rename to `test_rejects_acme_cert_mode`
- The test currently asserts `"PR-AC" in joined`. Since `"acme"` is not in
  the schema enum, jsonschema will reject it with a schema validation error
  (not the description string). Confirm what the actual error text contains
  and update the assertion to match. The assertion `assert errors` (cert_mode
  acme is rejected) is correct and must stay; only the `"PR-AC" in joined`
  string check needs updating.

**H. docs/operations/tls-certificates.md — new operations doc**

Full TLS lifecycle reference (frontend + Kamailio), renewal procedure,
troubleshooting.

**I. docs/operations/README.md — add TLS section**

Add a "TLS certificates" bullet linking to `tls-certificates.md`.

**J. docs/operator/cert.md — stale PR-AC references cleanup**

Three locations:

1. Line ~138 (out of scope note): change
   `"ACME (Let's Encrypt) automation. Tracked under PR-AC. Setting cert_mode: acme is rejected at config validation with a message pointing to that PR."`
   to
   `"ACME (Let's Encrypt) automation. Not supported. Setting cert_mode: acme is rejected at config validation. Use cert_mode=self_signed or cert_mode=manual; see 'Obtaining TLS Certificates' in README.md for external cert options."`

2. Line ~153 (troubleshooting): change
   `"cert_provision failed with \"acme cert_mode requires PR-AC\": set cert_mode back to self_signed or manual."`
   to
   `"cert_provision failed with \"cert_mode=acme is not supported\": set cert_mode back to self_signed or manual."`

3. Line ~134 (rolling-restart note): `"PR-AC will add a rolling-restart strategy"` — this refers to PR-AC-2 (Kamailio LB route), unrelated to ACME. Leave as-is.

### Out of scope

- Any installer automation of ACME / cert renewal
- Distributing Kamailio CA to frontend workloads automatically
- Changes to Kamailio cert_mode logic beyond message cleanup
- Changes to k8s manifests

---

## 3. Design

### 3.1 README structure change

Current order:
```
TLS Strategy → Image Policy → DNS Records → Production Cert Replacement → BYOC Mode → Commands
```

New order:
```
TLS Strategy → Obtaining TLS Certificates (NEW) → Image Policy → DNS Records
→ Production Cert Replacement → BYOC Mode → Commands (with cert section added)
```

### 3.2 "Obtaining TLS Certificates" section content

```markdown
## Obtaining TLS Certificates

VoIPBin requires TLS certificates from two independent systems:

| System | Config key | Default | Used by |
|--------|-----------|---------|---------|
| Frontend (admin, talk, meet, api, hook) | `tls_strategy` | `self-signed` (hyphen) | nginx sidecars, Go binaries |
| Kamailio SIP proxy | `cert_mode` | `self_signed` (underscore) | SIP/TLS, SIPS, WebRTC DTLS |

Both systems accept operator-supplied certificates. The installer does **not**
automate certificate issuance or renewal. Operators are responsible for
obtaining, renewing, and injecting certificates.

### Option 1 — Let's Encrypt (recommended for production)

Use [Certbot](https://certbot.eff.org/) to obtain a free, publicly trusted
certificate.

**Prerequisite:** All domain names passed to Certbot must resolve (via DNS A
records) to the machine running the standalone challenge before you run the
command. Create DNS records first (see [DNS Records](#dns-records)), wait for
propagation, then run Certbot.

**Standalone HTTP challenge** (requires port 80 reachable on the Certbot host):

```bash
certbot certonly --standalone \
  -d api.example.com \
  -d hook.example.com \
  -d admin.example.com \
  -d talk.example.com \
  -d meet.example.com
# Certs written to /etc/letsencrypt/live/api.example.com/
```

**DNS challenge** (no port 80 required; works with any DNS provider):

```bash
certbot certonly --manual \
  --preferred-challenges dns \
  -d "*.example.com" \
  -d example.com
# Follow prompts to add a TXT record at your registrar.
```

After issuance, follow the [Production Cert Replacement](#production-cert-replacement)
procedure to inject the cert into the cluster.

**Renewal:** Let's Encrypt certs expire after 90 days. Run `certbot renew`
before expiry, then re-run the replacement procedure to push the new cert
into the cluster. The installer does not automate this step.

### Option 2 — Commercial or self-managed CA

Purchase or issue a certificate from any CA. Obtain `fullchain.pem` (leaf +
intermediates) and `privkey.pem`. Then follow the
[Production Cert Replacement](#production-cert-replacement) or
[BYOC Mode](#byoc-mode) procedure.

### Kamailio certificate

Kamailio's cert is separate from the frontend cert. By default the installer
generates a self-signed CA and issues leaf certs for `sip.<domain>` and
`registrar.<domain>` (`cert_mode: self_signed`). SIP clients and WebRTC
browsers must trust this CA — export it with:

```bash
./voipbin-install cert export-ca --out ca.pem
```

To use a CA-issued cert for Kamailio instead, set `cert_mode: manual` in
`config.yaml` and provide per-SAN cert files under `cert_manual_dir`. See
[cert — Manage Kamailio TLS Certificates](#cert----manage-kamailio-tls-certificates).
```

### 3.3 "TLS Strategy" section addition

Append after the existing `byoc` bullet, before the `verify` code block:

```markdown
> **Note:** The installer does not automate certificate issuance or renewal.
> See [Obtaining TLS Certificates](#obtaining-tls-certificates) for how to
> obtain a CA-issued cert and inject it into the cluster.
```

### 3.4 `cert` command section in README

Add under `## Commands`, after the existing `destroy` / `status` sections:

```markdown
### cert -- Manage Kamailio TLS Certificates

Inspect, renew, and export the installer-managed Kamailio TLS certificates.

```bash
./voipbin-install cert status          # Show per-SAN expiry and CA fingerprint
./voipbin-install cert status --json   # JSON output
./voipbin-install cert renew           # Re-run cert_provision stage
./voipbin-install cert renew --force   # Clear cached state and force reissue
./voipbin-install cert export-ca       # Print CA certificate to stdout (PEM)
./voipbin-install cert export-ca --out ca.pem   # Write to file
./voipbin-install cert export-ca --der --out ca.der  # DER format
./voipbin-install cert clean-staging   # Remove temp cert-staging directory
```
```

### 3.5 schema.py canonical replacement string

```python
# Single canonical version (§2 Scope item D):
"Kamailio TLS cert provisioning mode. "
"'self_signed' (default) — installer generates a CA and issues per-SAN leaves "
"on every apply. "
"'manual' — operator supplies per-SAN fullchain.pem + privkey.pem under "
"cert_manual_dir/<san>/. "
"To use a CA-issued cert, set cert_mode=manual and supply certs via "
"cert_manual_dir. ACME is not supported."
```

### 3.6 cert_lifecycle.py canonical error message

```python
raise CertLifecycleError(
    "cert_mode=acme is not supported. "
    "Use cert_mode=self_signed or cert_mode=manual. "
    "For CA-issued certs, see 'Obtaining TLS Certificates' in README.md."
)
```

### 3.7 Test updates

**test_pr_z_cert_lifecycle.py:**
```python
# Before
class TestAcmeRejection:
    def test_acme_raises_with_pr_ac_mention(self):
        with pytest.raises(CertLifecycleError, match="PR-AC"):

# After
class TestAcmeRejection:
    def test_acme_mode_raises(self):
        with pytest.raises(CertLifecycleError, match="not supported"):
```

**test_pr_z_config_schema.py:**

`InstallerConfig.validate()` has a hardcoded special-case that pre-empts
jsonschema for `cert_mode == "acme"` and returns a hand-crafted error string
containing "PR-AC". The `"PR-AC" in joined` assertion is therefore currently
correct and passing — it tests the custom message, not the jsonschema enum error.

After updating `config.py`'s `validate()` (scope item F), the error string
will contain "not supported" instead. Update the test accordingly:

```python
# Before
def test_rejects_acme_with_pr_ac_hint(self):
    cfg = _mk_config(cert_mode="acme")
    errors = cfg.validate()
    assert errors, "expected validation errors"
    joined = " ".join(errors)
    assert "PR-AC" in joined

# After
def test_rejects_acme_cert_mode(self):
    cfg = _mk_config(cert_mode="acme")
    errors = cfg.validate()
    assert errors, "'acme' is not a valid cert_mode; expected validation errors"
    joined = " ".join(errors)
    assert "not supported" in joined
```

---

## 4. Test Plan

| ID | Verification | Method |
|----|-------------|--------|
| V1 | No `PR-AC` references remain in target files | `grep -rn 'PR-AC' scripts/config.py scripts/cert_lifecycle.py config/schema.py tests/test_pr_z_cert_lifecycle.py tests/test_pr_z_config_schema.py docs/operator/cert.md` → zero results. Note: `scripts/dev/pr_ac_2_mutant_harness.py` and `tests/test_pr_ac_*.py` contain "PR-AC-2" (a separate Kamailio LB route feature) and are explicitly excluded from this cleanup. |
| V2 | Full test suite passes | `python -m pytest tests/ -q --tb=no` → same pass count as baseline |
| V3 | README anchors resolve | Manual check: all internal links `#...` point to existing headings |
| V4 | `cert_mode=acme` still raises `CertLifecycleError` | `test_acme_mode_raises` passes with `match="not supported"` |
| V5 | `cert_mode=acme` still fails config validation | `test_rejects_acme_cert_mode` passes with `"not supported" in joined` |

---

## 5. File Change Summary

| File | Change |
|------|--------|
| `README.md` | Add "Obtaining TLS Certificates" section; add note to "TLS Strategy"; add `cert` command section under "Commands" |
| `config/schema.py` | Update cert_mode description (remove PR-AC reference, canonical string per §3.5) |
| `scripts/cert_lifecycle.py` | Update ACME error message (line ~389); clean up `CertLifecycleError` class docstring (line ~52); clean up `seed_kamailio_certs` docstring (line ~378) |
| `scripts/config.py` | Clean up `validate()` ACME block: update comment and error message to remove "PR-AC" (lines ~100-112) |
| `tests/test_pr_z_cert_lifecycle.py` | Rename `test_acme_raises_with_pr_ac_mention` → `test_acme_mode_raises`; update `match="PR-AC"` → `match="not supported"` |
| `tests/test_pr_z_config_schema.py` | Rename `test_rejects_acme_with_pr_ac_hint` → `test_rejects_acme_cert_mode`; update assertion to `"not supported" in joined` |
| `docs/operations/tls-certificates.md` | New file: full TLS lifecycle reference |
| `docs/operator/cert.md` | Update stale ACME out-of-scope note (line ~138) and troubleshooting entry (line ~153) to reflect "not supported" messaging |
| `docs/operations/README.md` | Add TLS section linking to `tls-certificates.md` |
| `docs/plans/2026-05-14-pr-ad-tls-operator-guide-design.md` | This file |

---

## 6. Open Questions

None. Proceeding to R2 review.
