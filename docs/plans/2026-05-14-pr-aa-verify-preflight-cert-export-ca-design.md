# PR-AA: `verify` Cert Preflight Check + `cert export-ca` Subcommand

**Status:** DRAFT v3 (R2 CHANGES_REQUESTED 반영)
**Author:** Hermes (CPO)  
**Date:** 2026-05-14  
**Branch:** `NOJIRA-PR-AA-verify-preflight-cert-export-ca`

---

## 1. Background

PR-Z shipped the Kamailio TLS cert provisioning pipeline (`cert_provision` stage).
PR-AA carry-forwards two operator-facing gaps identified during dogfood:

1. **Verify preflight cert check:** Before `ansible_run` deploys certs to the Kamailio VM,
   there is no gate confirming `cert_provision` actually populated the cert state.
   If cert_provision is skipped or soft-fails, the ansible role pushes empty/placeholder
   PEM files to `/opt/kamailio-docker/certs/`, silently breaking Kamailio TLS.

2. **`cert export-ca` subcommand:** Operators running in `self_signed` mode have no way
   to extract the installer-managed CA certificate. This is required for:
   - Installing the CA as a trusted root on SIP clients and WebRTC browsers
   - PR-AD (shared-CA TLS for admin SPA, talk, meet)
   - Troubleshooting trust chain failures

### Stage ordering context

`APPLY_STAGES` in `pipeline.py` is:
```
terraform_init → reconcile_imports → terraform_apply → reconcile_outputs
→ k8s_apply → reconcile_k8s_outputs → cert_provision → ansible_run
```

Kamailio certs live in SOPS `secrets.yaml` and are deployed to the Kamailio VM via
`ansible_run`. They are NOT embedded in any k8s manifest (only `KAMAILIO_INTERNAL_LB_ADDRESS`
appears in `k8s/voip/secret.yaml`). Therefore the correct preflight hook point is
`_run_ansible`, which fires after `cert_provision`.

---

## 2. Scope

### In scope

- `check_cert_provisioned()` in `scripts/preflight.py`:
  - Called from `_run_ansible` before the live ansible run (skipped on `dry_run=True`)
  - Raises `PreflightError` if `cert_state` is absent or `actual_mode` is absent
  - Raises `PreflightError` if `actual_mode == "self_signed"` but `ca_fingerprint_sha256` is absent
  - Raises `PreflightError` if any SAN in `cert_state.san_list` is missing from `leaf_certs`
  - Raises `PreflightError` if any leaf cert entry lacks `fingerprint_sha256`

- `load_secrets_for_cert(config: InstallerConfig)` moved from `pipeline._load_secrets_for_cert_stage`
  to `scripts/secretmgr.py` (no import cycle: secretmgr already imports from display, secret_schema,
  utils; adding scripts.config adds no cycle)

- `cmd_cert_export_ca(output_path, as_der)` in `scripts/commands/cert.py`:
  - Loads `InstallerConfig`, then decrypts secrets.yaml via `load_secrets_for_cert`
  - Extracts `KAMAILIO_CA_CERT_KEY` (= `"KAMAILIO_CA_CERT_BASE64"`)
  - Mode guard: exits 1 if `cert_state.actual_mode != "self_signed"`
  - CLI: `voipbin-install cert export-ca [--out FILE] [--der]`

- Tests: `tests/test_pr_aa_cert_preflight.py` + `tests/test_pr_aa_cert_export_ca.py`

- CLI wiring in `scripts/cli.py`

### Out of scope

- ACME mode support (PR-AC)
- non-Kamailio TLS distribution to admin SPA, talk, meet (PR-AD)
- Pushing CA to k8s ConfigMap automatically
- Post-deploy `verify` cert check (already handled by `check_tls_cert_is_production`)

---

## 3. Design

### 3.1 `check_cert_provisioned`

**Location:** `scripts/preflight.py`

**Signature:**
```python
def check_cert_provisioned() -> None:
    """Raise PreflightError if cert_provision has not run or left incomplete state.

    Called from _run_ansible inside the `if not dry_run:` block, so it is
    never reached on any dry_run path — including the ansible --check path
    (which only runs when outputs contain kamailio_internal_ips).

    No config argument needed: reads from load_state() only.
    """
```

**Algorithm:**
1. Lazy import and state load:
   ```python
   from scripts.pipeline import load_state
   state = load_state()
   cert_state = state.get("cert_state") or {}
   ```
2. If `cert_state` is empty or `cert_state.get("actual_mode")` is absent:
   ```
   raise PreflightError(
       "cert_provision has not run or failed. "
       "Re-run with: voipbin-install cert renew"
   )
   ```
3. `mode = cert_state["actual_mode"]`
4. If `mode == "self_signed"` and not `cert_state.get("ca_fingerprint_sha256")`:
   ```
   raise PreflightError(
       "cert_state.actual_mode=self_signed but CA fingerprint is absent. "
       "Re-run: voipbin-install cert renew"
   )
   ```
5. `san_list = cert_state.get("san_list") or []`
6. `leaf_certs = cert_state.get("leaf_certs") or {}`
7. For each `san` in `san_list`:
   - If `san` not in `leaf_certs`:
     ```
     raise PreflightError(
         f"cert_provision: leaf cert missing for SAN {san!r}. "
         "Re-run: voipbin-install cert renew"
     )
     ```
   - If not `leaf_certs[san].get("fingerprint_sha256")`:
     ```
     raise PreflightError(
         f"cert_provision: leaf cert for {san!r} has no fingerprint — "
         "cert state may be corrupted. Re-run: voipbin-install cert renew"
     )
     ```
8. Return (no exception = success).

**Note on step 7 vs empty san_list:** If `san_list == []` the loop is a no-op.
This edge case is intentionally allowed — an empty SAN list means `cert_provision`
ran and produced no certs (a degenerate but not illegal state). The ansible role
will simply deploy no certs, which is correct behaviour for a domain with no
Kamailio endpoints yet.

**Pipeline wiring** in `scripts/pipeline.py`, `_run_ansible`:
```python
if not dry_run:
    from scripts.preflight import (
        PreflightError,
        check_cert_provisioned,
        check_oslogin_setup,
    )
    err = check_oslogin_setup()
    if err is not None:
        print_error(err)
        return False
    try:
        check_cert_provisioned()
    except PreflightError as exc:
        print_error(str(exc))
        return False
```
Placed immediately after the existing OS Login check, before the `ansible_run` call.
Both imports from `scripts.preflight` are combined into a single `from` statement.

**Error severity:** Hard fail (returns `False` from `_run_ansible`). Deploying without
a valid cert state causes Kamailio TLS to fail silently.

### 3.2 `load_secrets_for_cert` (secretmgr.py refactor)

Extract `_load_secrets_for_cert_stage` from `pipeline.py` into `scripts/secretmgr.py`
as a public function:

```python
def load_secrets_for_cert(config) -> dict:
    """Decrypt secrets.yaml via sops and return the dict, or {} if absent.

    `config` must have a `secrets_path` attribute (InstallerConfig).
    Kept in secretmgr.py to avoid a pipeline → commands import cycle.
    """
    secrets_path = config.secrets_path
    if not secrets_path.exists():
        return {}
    parsed = decrypt_with_sops(secrets_path)
    return parsed if isinstance(parsed, dict) else {}
```

Update `pipeline.py` to call `from scripts.secretmgr import load_secrets_for_cert`
and remove `_load_secrets_for_cert_stage`.

### 3.3 `cmd_cert_export_ca`

**Location:** `scripts/commands/cert.py`

**Signature:**
```python
def cmd_cert_export_ca(
    output_path: str | None = None,
    as_der: bool = False,
) -> int:
    """Export the installer-managed CA certificate to stdout or a file.

    Only valid when cert_state.actual_mode == "self_signed".
    """
```

**Algorithm:**
1. Load state and config:
   ```python
   state = load_state()
   cert_state = state.get("cert_state") or {}
   config = InstallerConfig()
   if not config.exists():
       print_error("No configuration found. Run `voipbin-install init` first.")
       return 1
   config.load()
   ```
2. Mode guard with None-aware message:
   ```python
   actual_mode = cert_state.get("actual_mode")
   if actual_mode is None:
       print_error(
           "cert_provision has not run yet. "
           "Try: voipbin-install apply"
       )
       return 1
   if actual_mode != "self_signed":
       print_error(
           "CA export is only available in self_signed mode. "
           "In manual mode the CA is managed externally."
       )
       return 1
   ```
3. Fingerprint sanity check:
   ```python
   if not cert_state.get("ca_fingerprint_sha256"):
       print_error(
           "cert_state does not contain CA fingerprint — "
           "cert_provision may not have run. Try: voipbin-install cert renew"
       )
       return 1
   ```
4. Load secrets:
   ```python
   from scripts.secretmgr import load_secrets_for_cert
   secrets = load_secrets_for_cert(config)
   if not secrets:
       print_error("secrets.yaml not found or empty — cannot export CA.")
       return 1
   ```
5. Extract CA cert:
   ```python
   from scripts.tls_bootstrap import KAMAILIO_CA_CERT_KEY
   ca_cert_b64 = secrets.get(KAMAILIO_CA_CERT_KEY)
   if not ca_cert_b64:
       print_error(
           f"{KAMAILIO_CA_CERT_KEY} not found in secrets — "
           "cert_provision may not have run. Try: voipbin-install cert renew"
       )
       return 1
   ```
6. Decode with error handling:
   ```python
   import base64, binascii
   try:
       pem_bytes = base64.b64decode(ca_cert_b64)
   except (binascii.Error, ValueError) as exc:
       print_error(f"CA cert in secrets is not valid base64: {exc}")
       return 1
   ```
7. Convert format if DER requested:
   ```python
   if as_der:
       if output_path is None and sys.stdout.isatty():
           print_error(
               "DER output to a terminal is not safe. Use --out FILE to write DER."
           )
           return 1
       from cryptography import x509
       from cryptography.hazmat.primitives.serialization import Encoding
       try:
           cert = x509.load_pem_x509_certificate(pem_bytes)
           output_bytes = cert.public_bytes(Encoding.DER)
       except Exception as exc:
           print_error(f"Failed to parse CA PEM for DER conversion: {exc}")
           return 1
   else:
       output_bytes = pem_bytes
   ```
8. Output:
   ```python
   if output_path:
       Path(output_path).write_bytes(output_bytes)
       print_success(f"CA certificate written to {output_path}")
   else:
       sys.stdout.buffer.write(output_bytes)
   return 0
   ```

### 3.4 CLI wiring

In `scripts/cli.py`, add under the `cert` group:

```python
@cert.command("export-ca")
@click.option(
    "--out", "output_path", default=None, metavar="FILE",
    help="Write CA certificate to FILE instead of stdout.",
)
@click.option(
    "--der", "as_der", is_flag=True, default=False,
    help="Output DER-encoded bytes (default: PEM). Requires --out when stdout is a TTY.",
)
def cert_export_ca(output_path, as_der):
    """Export the installer-managed CA certificate (self_signed mode only)."""
    rc = cmd_cert_export_ca(output_path=output_path, as_der=as_der)
    sys.exit(rc)
```

Also import `cmd_cert_export_ca` at the top of `cli.py`.

---

## 4. Test Plan

### 4.1 `tests/test_pr_aa_cert_preflight.py`

| ID | Test | Key assertion |
|----|------|---------------|
| P1 | Valid self_signed cert_state → no exception | positive; fixture includes ca_fingerprint_sha256 + leaf entries with fingerprint_sha256 |
| P2 | Valid manual cert_state (no ca_fingerprint_sha256) → no exception | positive; explicitly absent ca_fingerprint_sha256 must NOT raise in manual mode |
| P3 | Empty cert_state ({}) → PreflightError | negative |
| P4 | actual_mode absent → PreflightError | negative |
| P5 | self_signed + ca_fingerprint_sha256 absent → PreflightError | negative |
| P6 | manual + ca_fingerprint_sha256 absent → no exception | positive boundary |
| P7 | Leaf missing for a SAN → PreflightError | negative |
| P8 | Leaf present but fingerprint_sha256 absent → PreflightError | negative |
| P9 | cert_state with actual_mode set but san_list=[] → no exception | edge case: empty SAN list is allowed |
| W1 | _run_ansible calls check_cert_provisioned when dry_run=False | wiring: mock check_cert_provisioned, assert called |
| W2 | _run_ansible skips check_cert_provisioned when dry_run=True | wiring: mock check_cert_provisioned, assert not called |
| M1 | Mutant: remove actual_mode check → P3/P4 must catch | mutation |
| M2 | Mutant: remove ca_fingerprint_sha256 check → P5 must catch | mutation |

### 4.2 `tests/test_pr_aa_cert_export_ca.py`

| ID | Test | Key assertion |
|----|------|---------------|
| E1 | self_signed mode, stdout PEM → rc 0, output starts with `-----BEGIN CERTIFICATE-----` | positive |
| E2 | self_signed mode, --out FILE → rc 0, file written, success message | positive |
| E3 | self_signed mode, --der, --out FILE → rc 0, DER parseable as x509 | positive |
| E4 | --der without --out, stdout is TTY → rc 1, error message | negative |
| E5 | --der without --out, stdout is NOT TTY (pipe) → rc 0, DER bytes on stdout | positive boundary |
| E6 | actual_mode == "manual" → rc 1, "managed externally" message | negative |
| E7 | actual_mode is None (cert_provision not run) → rc 1, "not run yet" message (not manual mode message) | negative; verifies None-aware branch |
| E8 | cert_state missing ca_fingerprint_sha256 → rc 1 | negative |
| E9 | secrets empty → rc 1 | negative |
| E10 | KAMAILIO_CA_CERT_KEY absent from secrets → rc 1 | negative |
| E11 | KAMAILIO_CA_CERT_KEY is invalid base64 → rc 1 | negative |
| M1 | Mutant: skip mode check → E6 must fail | mutation |
| M2 | Mutant: skip secrets empty check → E9 must catch | mutation |

All tests: pure unit tests using `unittest.mock`. No sops/GCP required.

---

## 5. File Change Summary

| File | Change |
|------|--------|
| `scripts/preflight.py` | Add `check_cert_provisioned()` |
| `scripts/pipeline.py` | Wire `check_cert_provisioned` in `_run_ansible`; update `_load_secrets_for_cert_stage` → import from secretmgr |
| `scripts/secretmgr.py` | Add `load_secrets_for_cert(config)` (public, extracted from pipeline.py) |
| `scripts/commands/cert.py` | Add `cmd_cert_export_ca(output_path, as_der)` |
| `scripts/cli.py` | Add `cert export-ca` command + import |
| `tests/test_pr_aa_cert_preflight.py` | New: 13 tests (P1-P9, W1-W2, M1-M2) |
| `tests/test_pr_aa_cert_export_ca.py` | New: 13 tests (E1-E11, M1-M2) |
| `docs/plans/2026-05-14-pr-aa-verify-preflight-cert-export-ca-design.md` | This file |

---

## 6. Risks and Mitigations

| Risk | Mitigation |
|------|------------|
| `_run_ansible` cert preflight blocks operators whose cert_provision soft-failed silently | Clear error + `voipbin-install cert renew` remediation hint; dry_run always skips |
| Moving `_load_secrets_for_cert_stage` to secretmgr.py introduces import cycle | No cycle: secretmgr already imports from display, secret_schema, utils; adding config is safe |
| DER output to terminal corrupts shell session | Explicit `sys.stdout.isatty()` guard in algorithm (step 7); returns 1 with explanatory error |
| Bad base64 in KAMAILIO_CA_CERT_BASE64 causes uncaught exception | Step 6 wraps `base64.b64decode` in try/except; E11 exercises this path |
| W1/W2 tests become implementation-tied if _run_ansible refactors | Acceptable: pipeline wiring tests are value-positive even if they need updating |

---

## 7. Open Questions

None. Proceeding to implementation.
