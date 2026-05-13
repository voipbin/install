# DESIGN: PR-AE-2 — Kamailio LB Address Source Fix

## Problem

`scripts/k8s.py:_build_substitution_map()` reads the Kamailio internal LB IP only
from `config.yaml`:

```python
kamailio_lb_address = config.get("kamailio_internal_lb_address", "")
```

`config.yaml` does not contain `kamailio_internal_lb_address` (the key is never
populated by the installer pipeline). Terraform outputs the same value under the
key `kamailio_internal_lb_ip` (see `terraform/outputs.tf:138`), but `k8s.py`
never reads `terraform_outputs`.

Result: `PLACEHOLDER_KAMAILIO_INTERNAL_LB_ADDRESS` survives substitution as a
literal string in `k8s/voip/secret.yaml`, causing the voip namespace secret to
carry a garbage `KAMAILIO_INTERNAL_LB_ADDRESS` value. Observed as a warning in
iter#15 log: `KAMAILIO_INTERNAL_LB_ADDRESS: PLACEHOLDER_KAMAILIO_INTERNAL_LB_ADDRESS`.

## Root Cause

Key mismatch between terraform output (`kamailio_internal_lb_ip`) and config lookup
(`kamailio_internal_lb_address`). Terraform outputs are already passed into
`_build_substitution_map()` as the `terraform_outputs` argument but were not used
for this particular token.

## Fix

Add `terraform_outputs.get("kamailio_internal_lb_ip", "")` as a fallback source,
with the config value taking precedence (for operator override):

```python
# Before
kamailio_lb_address = config.get("kamailio_internal_lb_address", "")

# After
kamailio_lb_address = (
    config.get("kamailio_internal_lb_address", "")
    or terraform_outputs.get("kamailio_internal_lb_ip", "")
)
```

`config.yaml` override is preserved for operators who need to supply a static value.

## Test Strategy

`tests/test_pr_ae2_kamailio_lb_address_source.py`

| Test | What it checks |
|------|----------------|
| `test_config_value_used_when_present` | config key takes precedence over terraform output |
| `test_terraform_output_used_as_fallback` | when config key absent, terraform_outputs value is used |
| `test_empty_config_falls_back_to_terraform` | empty string config triggers fallback |
| `test_both_absent_yields_empty` | both absent → empty string, no exception |
| `test_placeholder_not_in_rendered_manifest` | after substitution with a real IP, no PLACEHOLDER_ remains in k8s/voip/secret.yaml |

Mutant harness: swap precedence / remove fallback / return empty → must fail tests.

## Files Changed

- `scripts/k8s.py` — 2-line change to `_build_substitution_map()` (kamailio_lb_address assignment)
- `tests/test_pr_ae2_kamailio_lb_address_source.py` — new test file (5 tests + mutant harness)

## Non-Goals

- No change to `config.yaml` schema or documentation in this PR
- No change to terraform (outputs.tf already has the correct key)
- No change to `ansible_runner.py` (already uses `kamailio_internal_lb_ip` correctly)
