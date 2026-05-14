# DESIGN-PR-AG-2: Fix reconcile_outputs to overwrite stale IPs after wipeout

## Problem

`scripts/terraform_reconcile.py::outputs()` skips updating a config field if it
already has a non-sentinel value:

```python
current = config.get(mapping.cfg_key)
if not current or current == CLOUDSQL_PRIVATE_IP_SENTINEL:
    config.set(mapping.cfg_key, value)
```

On a fresh install this is correct: the sentinel `cloudsql-private.invalid` means
"never been set". But after a **wipeout + re-apply**, Terraform creates new Cloud
SQL instances with new private IPs. The old IPs remain in `config.yaml` from the
previous run. The "already set" guard prevents overwriting them, so Kamailio
connects to a dead IP and fails to start.

Observed in dogfood iter (2026-05-14):
- Destroy + fresh apply → Cloud SQL MySQL IP changed `10.19.32.3` → `10.36.80.4`
- `reconcile_outputs` skipped update (old IP already in config.yaml)
- Kamailio `auth_db` module could not connect → container unhealthy

## Decision

**Always overwrite** the config field when Terraform emits a valid, differing
value. All entries in FIELD_MAP are unambiguously Terraform-owned infra outputs
(IPs, bucket names) — operators do not hand-set these. When Terraform emits a
new valid value, it is authoritative and the old value is stale by definition.

Add a code comment to FIELD_MAP asserting this invariant so future contributors
know not to add operator-settable fields there.

## Change

### `scripts/terraform_reconcile.py`

1. Replace the skip guard with an overwrite-when-different check:

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

   Note: `not current` previously guarded `None`/`""` cases. The new check
   `current != value` handles those implicitly — a valid non-empty Terraform
   value always differs from `None` or `""`, so those paths are preserved.

2. Remove the now-unused `CLOUDSQL_PRIVATE_IP_SENTINEL` import from the
   `outputs()` function scope.

3. Update the `outputs()` docstring: remove the clause *"skipping any field the
   operator has already set"* — replace with *"Terraform values are authoritative
   and overwrite any previously stored value when they differ."*

4. Add a comment above `FIELD_MAP` stating:
   *"All entries must be Terraform-owned outputs. Operator-settable config fields
   must NOT be added here — values are overwritten unconditionally on each apply."*

### `tests/test_pr_e_config_cleanup.py`

The existing test `test_reconcile_outputs_skips_when_operator_override` asserts
the old (now-wrong) behavior that a non-sentinel value is preserved. This test
must be **replaced** with a test asserting the correct new behavior:

```python
def test_reconcile_outputs_overwrites_stale_operator_value():
    """Stale/operator-looking value in config.yaml is overwritten by Terraform output."""
    store = {"cloudsql_private_ip": "10.99.99.99"}
    tf = {"cloudsql_mysql_private_ip": "10.0.0.5", ...}
    outputs(cfg, tf)
    assert store["cloudsql_private_ip"] == "10.0.0.5"  # Terraform wins
```

## Tests

Add to `tests/test_terraform_reconcile.py`:

- **T-AG-1:** `outputs()` with pre-existing non-sentinel IP value → field IS
  overwritten with new Terraform value (covers wipeout scenario for both IP and
  bucket-name fields).
- **T-AG-2:** `outputs()` when Terraform value equals current value → `changed`
  is False, `config.save()` not called (stable deploy no-op).
- **T-AG-3:** `outputs()` when Terraform value is empty/None → field is skipped
  (existing behaviour preserved).

Replace in `tests/test_pr_e_config_cleanup.py`:
- `test_reconcile_outputs_skips_when_operator_override` → replaced with
  `test_reconcile_outputs_overwrites_stale_operator_value` (as above).

Replace in `tests/test_pipeline_reconcile_split.py`:
- `TestReconcileOutputs::test_reconcile_outputs_skips_when_config_already_set`
  encodes the same now-wrong invariant (`assert_not_called()` after emitting a
  differing value). Must be replaced with two tests:
  - `test_reconcile_outputs_overwrites_when_value_differs`: Terraform emits a
    value different from the current config → `config.set` IS called.
  - `test_reconcile_outputs_noop_when_value_identical`: Terraform emits the same
    value as already in config → `config.set` NOT called, `changed` is False.

## Risk

Low. The only behavioural change: fields with existing values are now updated
when Terraform emits a different valid value. All FIELD_MAP entries are
Terraform-owned, so this is always correct. Validators gate invalid values before
they reach `config.set`.

## Alternatives Rejected

- **Per-field `force` flag:** Overly complex — all FIELD_MAP entries are
  Terraform-owned.
- **Wipeout-detection flag:** Fragile state machine; one-line fix is sufficient.
