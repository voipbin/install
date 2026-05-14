# DESIGN-PR-AG-1: Fix reconcile_outputs to overwrite stale IPs after wipeout

## Problem

`scripts/terraform_reconcile.py::outputs()` (line 824) skips updating a config
field if it already has a non-sentinel value:

```python
current = config.get(mapping.cfg_key)
if not current or current == CLOUDSQL_PRIVATE_IP_SENTINEL:
    config.set(mapping.cfg_key, value)
```

On a fresh install this is correct: the sentinel `cloudsql-private.invalid` means
"never been set". But after a **wipeout + re-apply**, terraform creates new Cloud
SQL instances with new private IPs. The old IPs remain in `config.yaml` from the
previous run. The "already set" guard prevents overwriting them, so Kamailio
connects to a dead IP and fails to start.

Observed in dogfood iter (2026-05-14):
- Destroy + fresh apply → Cloud SQL MySQL IP changed `10.19.32.3` → `10.36.80.4`
- `reconcile_outputs` skipped update (old IP already in config.yaml)
- Kamailio `auth_db` module could not connect → container unhealthy

## Decision

**Always overwrite** the config field when Terraform emits a valid value for it.
The guard was originally intended to protect manually-set operator values, but:

1. All fields in `FIELD_MAP` are infra-managed outputs (IPs, bucket names) that
   come exclusively from Terraform. Operators do not hand-set these.
2. If Terraform outputs a new valid IP, it is authoritative. The old value is
   stale by definition.
3. The sentinel check is therefore redundant — if Terraform outputs a valid IP,
   it always wins regardless of what is in config.yaml.

## Change

**File:** `scripts/terraform_reconcile.py`

Replace the skip guard with an unconditional overwrite, but only log a change
when the value actually differs:

```python
# Before
current = config.get(mapping.cfg_key)
if not current or current == CLOUDSQL_PRIVATE_IP_SENTINEL:
    config.set(mapping.cfg_key, value)
    changed = True

# After
current = config.get(mapping.cfg_key)
if current != value:
    config.set(mapping.cfg_key, value)
    changed = True
```

Remove the `CLOUDSQL_PRIVATE_IP_SENTINEL` import from this function — it is no
longer referenced.

## Tests

Add to `tests/test_terraform_reconcile.py`:

- **T-AG-1:** `outputs()` with a pre-existing non-sentinel value → field IS
  overwritten with new Terraform value (wipeout scenario).
- **T-AG-2:** `outputs()` when Terraform value equals current value → `changed`
  is False, `config.save()` not called (no-op on stable deploy).
- **T-AG-3:** `outputs()` when Terraform value is empty/None → field is skipped
  (existing behaviour preserved).

Existing tests must continue to pass.

## Risk

Low. The only behavioural change is: fields that already have a value are now
updated when Terraform emits a different value. This is the correct semantics for
infra-managed outputs. No operator-settable fields are in FIELD_MAP.

## Alternatives Rejected

- **Per-field `force` flag:** More complex, no use-case — all FIELD_MAP entries
  are Terraform-owned.
- **Wipeout-detection flag:** Fragile state machine, not worth the complexity for
  a one-line fix.
