# DESIGN: PR-AE — reconcile_imports SA not-found false-positive fix

**Status:** v2 (iter-2 review addressed)  
**Branch:** NOJIRA-PR-AE-reconcile-sa-not-found-fix  
**Date:** 2026-05-14  
**Author:** Hermes (CPO)

---

## 1. Problem

On a fresh wipeout-and-retest cycle (all GCP resources destroyed), `voipbin-install apply`
fails at the `reconcile_imports` stage with:

```
✗ Import failed for:
  google_service_account.sa_gke_nodes   # unverified
  google_service_account.sa_kamailio    # unverified
  google_service_account.sa_rtpengine   # unverified
```

Root cause: two compounding bugs in `scripts/terraform_reconcile.py`.

### Bug A — gcloud SA check returns PERMISSION_DENIED, not "not found"

When a service account does not exist, GCP IAM returns:

```
ERROR: PERMISSION_DENIED: Permission 'iam.serviceAccounts.get' denied
on resource (or it may not exist).
```

The phrase `"or it may not exist"` in the stderr does NOT match any entry in
`_NOT_FOUND_PHRASES`:

```python
_NOT_FOUND_PHRASES = ("not found", "notfound", "not_found",
                      "does not exist", "404", "no such")
```

`check_exists_in_gcp` therefore returns `(False, False)` — meaning "check
failed for unknown reason" — and the resource is added to `conflicts` with
`unverified=True`.

### Bug B — unverified resources are imported unconditionally

`reconcile_imports` treats `unverified=True` entries the same as confirmed-existing
resources: it runs `terraform import` on them. On a fresh install, the SA does not
yet exist in GCP, so import fails with:

```
Error: Cannot import non-existent remote object
```

This causes `failures` to be non-empty, and the function returns `False`,
halting the pipeline.

### Impact

Every fresh wipeout-and-retest cycle hits this. Not a resume (existing infra)
regression — only occurs when SA resources are absent.

---

## 2. Locked design decisions

### Fix A — add `"or it may not exist"` to `_NOT_FOUND_PHRASES`

GCP IAM's PERMISSION_DENIED message reliably contains the substring
`"or it may not exist"` when the resource is absent and the caller lacks
`iam.serviceAccounts.get` permission. Adding this phrase to `_NOT_FOUND_PHRASES`
causes `check_exists_in_gcp` to return `(False, True)` — meaning "does not
exist, check succeeded" — and the SA candidates are excluded from the import
list entirely.

```python
_NOT_FOUND_PHRASES = (
    "not found", "notfound", "not_found", "does not exist",
    "404", "no such",
    "or it may not exist",   # GCP IAM PERMISSION_DENIED for absent SA
)
```

**Why not fix B separately?** Fix A is the correct classification fix. Fix B
(treating `non-existent remote object` import failure as non-fatal) would be
a second defense layer but is intentionally NOT added here — making import
failures of `unverified` resources non-fatal would hide real conflicts
(a resource that exists but caller can't verify). Defense-in-depth is a
future PR concern.

**Why not treat PERMISSION_DENIED itself as not-found?** The substring
`"permission_denied"` alone is too broad — a PERMISSION_DENIED on an
existing resource (caller lacks read but resource exists) would be wrongly
classified as "not found", hiding real conflicts. The combined phrase
`"or it may not exist"` is narrower and specific to the GCP IAM error path
that explicitly signals the resource may be absent.

### Fix A scope: `_NOT_FOUND_PHRASES` only

Single-line change. No changes to `check_exists_in_gcp`, `reconcile_imports`,
or the registry builder.

---

## 3. Test design

### Test T1 — unit: `check_exists_in_gcp` classifies GCP IAM PERMISSION_DENIED as not-found

```python
def test_check_exists_gcp_permission_denied_treated_as_not_found(monkeypatch):
    """GCP IAM returns PERMISSION_DENIED + 'or it may not exist' for absent
    service accounts. check_exists_in_gcp must classify this as
    (exists=False, check_succeeded=True) so the SA is excluded from imports."""
    from scripts.terraform_reconcile import check_exists_in_gcp
    from unittest.mock import patch

    class _R:
        returncode = 1
        stderr = ("ERROR: (gcloud.iam.service-accounts.describe) "
                  "PERMISSION_DENIED: Permission 'iam.serviceAccounts.get' "
                  "denied on resource (or it may not exist).")
        stdout = ""

    with patch("scripts.terraform_reconcile.run_cmd", return_value=_R()):
        exists, check_ok = check_exists_in_gcp(["gcloud", "iam", "service-accounts",
                                                  "describe", "test@project.iam.gserviceaccount.com"])
    assert not exists,    "SA that does not exist must be reported as absent"
    assert check_ok,      "check must be reported as succeeded (not unverified)"
```

### Test T2 — integration smoke: fresh-install reconcile with no SAs in GCP

Note (iter-2 fix): `build_registry` calls `config.get("zone")` for entries like
`google_container_cluster`. Without `zone`, import_id contains literal `"None"` which
`_validate_entry` rejects with `ReconcileRegistryError`. Config must include `zone`.

```python
def test_reconcile_imports_fresh_install_no_sa_conflict(monkeypatch):
    """On a fresh install where SAs do not exist yet, reconcile_imports must
    return True and NOT attempt import of SA resources."""
    from scripts.terraform_reconcile import imports as reconcile_imports
    from scripts.config import InstallerConfig
    from unittest.mock import patch, MagicMock
    import os

    cfg = InstallerConfig({
        "gcp_project_id": "voipbin-install-dev",
        "region": "us-central1",
        "zone": "us-central1-a",   # required: used in GKE import_id
        "env": "voipbin",
    })

    def fake_run_cmd(argv, **kwargs):
        r = MagicMock()
        r.stdout = ""
        # Simulate: SA gcloud check → PERMISSION_DENIED (not exist)
        if "service-accounts" in argv and "describe" in argv:
            r.returncode = 1
            r.stderr = ("ERROR: PERMISSION_DENIED: Permission "
                        "'iam.serviceAccounts.get' denied on resource "
                        "(or it may not exist).")
        # Simulate: KMS and other resources do not exist (normal not-found)
        else:
            r.returncode = 1
            r.stderr = "ERROR: Resource not found."
        return r

    with patch("scripts.terraform_reconcile.run_cmd", side_effect=fake_run_cmd), \
         patch("scripts.terraform_reconcile.terraform_state_list", return_value=set()):
        result = reconcile_imports(cfg, auto_approve=True)

    assert result, "reconcile_imports must return True when all candidates are absent (fresh install)"
```

### Test T4 — negative case: bare PERMISSION_DENIED (without "or it may not exist") stays unverified

```python
def test_check_exists_gcp_bare_permission_denied_is_unverified():
    """A PERMISSION_DENIED message WITHOUT 'or it may not exist' must NOT be
    classified as not-found — the resource may exist but caller lacks access.
    It must remain unverified (check_ok=False) so the conflict is surfaced."""
    from scripts.terraform_reconcile import check_exists_in_gcp
    from unittest.mock import patch

    class _R:
        returncode = 1
        stderr = "ERROR: PERMISSION_DENIED: Permission denied on resource."
        stdout = ""

    with patch("scripts.terraform_reconcile.run_cmd", return_value=_R()):
        exists, check_ok = check_exists_in_gcp(["gcloud", "iam", "service-accounts",
                                                  "describe", "test@project.iam.gserviceaccount.com"])
    assert not exists,    "resource not confirmed to exist"
    assert not check_ok,  "bare PERMISSION_DENIED must remain unverified"
```

### Test T3 — synthetic injection proof (mutant: remove the new phrase)

Remove `"or it may not exist"` from `_NOT_FOUND_PHRASES` and confirm T1 fails.
With the phrase removed, `check_exists_in_gcp` returns `(False, False)` → T1's
`assert check_ok` fails. Documented as programmatic harness in PR body.

---

## 4. Files changed

| File | Change |
|---|---|
| `scripts/terraform_reconcile.py` | Add `"or it may not exist"` to `_NOT_FOUND_PHRASES` (1 line) |
| `tests/test_terraform_reconcile.py` | Add T1 + T2 + T4 (3 new test cases) |

---

## 5. Risks and non-risks

- **Non-risk:** The new phrase is very specific to GCP IAM's error path. No
  other GCP API or gcloud command is known to emit this exact phrase for a
  "resource exists but caller cannot access" case. The risk of false negative
  (real conflict silently skipped) is considered negligible.
- **Risk to watch:** If a future GCP IAM change drops the `"or it may not exist"`
  clause from the PERMISSION_DENIED message, SAs would again be treated as
  `unverified` and imports would fail. The T1 unit test would catch this
  regression on any GCP-aware CI runner.

---

## 6. Checklist

- [ ] Design review iter-1 (APPROVED or CHANGES_REQUESTED)
- [ ] Design review iter-2 (min 2 required)
- [ ] Implementation
- [ ] Tests pass (`pytest tests/test_terraform_reconcile.py -q`)
- [ ] Full baseline green (`pytest tests/ -q`)
- [ ] Synthetic injection proof (T3 harness run)
- [ ] PR review iter-1
- [ ] PR review iter-2
- [ ] PR review iter-3 (min 3 required)
- [ ] Sensitive data audit (`bash scripts/dev/check-plan-sensitive.sh`)
