# Reserve static IPs for external Services (no Service type flip yet)

**Status:** Draft (in review)
**Branch:** `NOJIRA-Reserve-static-ips-for-external-services`
**Author:** Hermes (CPO)
**Date:** 2026-05-11
**Parent plan:** `docs/plans/2026-05-11-self-hosting-architecture-redesign.md`
**Roadmap slot:** PR #2 of the parent plan's §10 split.

> Sensitive-data policy: this document is publicly viewable on the
> `voipbin/install` repository. It MUST NOT contain real production
> IPs, project IDs, instance names, or domain names. Use placeholders
> only (`example.com`, `203.0.113.x` per RFC 5737, `<your-project>`).
> The audit gate `scripts/dev/check-plan-sensitive.sh` (introduced in
> PR #9) is re-run on this plan before merge.

## 1. Problem statement

The parent plan (PR #9) commits the installer to a per-Service
`type=LoadBalancer` topology with one reserved GCP regional static IP
per externally exposed Service. Today the install repo has neither the
Terraform resources to reserve those addresses nor the placeholder
plumbing in the manifest renderer to emit the
`kubernetes.io/ingress.global-static-ip-name` annotation against a
known address name. Without this PR, PR #3a (the actual Service flip)
would have no addresses to point at.

This PR introduces the address reservations, the Terraform outputs
that expose their names, the placeholder tokens that thread those
names into manifest rendering, and a pre-flight check that warns when
the operator's GCP project would exceed its regional static-IP quota
during apply. It does NOT change any Service `type` and does NOT alter
any in-cluster manifest behavior. The reserved addresses simply exist
in GCP until PR #3a starts using them.

## 2. Goals

1. `terraform apply` reserves exactly 5 `google_compute_address`
   resources for the external Services named in the parent plan §4
   (`api-manager`, `hook-manager`, `admin`, `talk`, `meet`), each with
   name `<service>-static-ip` (per pchero decision §12.1 of the parent
   plan).
2. `terraform output -json` exposes those 5 addresses as
   `<service>_static_ip_name` and `<service>_static_ip_address`
   outputs so downstream tooling can read them.
3. `scripts/k8s.py::_build_substitution_map` is extended with 5 new
   `PLACEHOLDER_STATIC_IP_NAME_<SERVICE>` tokens that resolve to the
   reserved names. Manifests that need the annotation in a future PR
   can reference these tokens; no manifest references them in this PR.
4. A pre-flight check in `scripts/preflight.py` verifies the
   operator's project has at least 5 free regional static-IP slots in
   the chosen region (`STATIC_ADDRESSES` quota minus current
   consumption). On insufficient quota, the check fails with an
   actionable message that includes the gcloud command to request a
   quota increase.
5. `voipbin-install verify` reports the 5 reserved addresses' names
   and assigned IPs in a new `static_ips_reserved` check (display
   label `Static IPs`), so operators can copy/paste them into a DNS
   provider. (The DNS resolution verification itself lands in PR #5;
   PR #2 only reports the IPs.)
6. Sensitive-data audit gate passes on this plan.
7. No Service `type` changes. No manifest under `k8s/` references the
   new placeholders yet.

## 3. Non-goals

- Flipping any Service to `type=LoadBalancer`. That is PR #3a.
- Removing `nginx-ingress` or `cert-manager`. Those are PR #3b.
- Renaming `square-admin/talk/meet` to `admin/talk/meet`. That is
  PR #4.
- Adding any new namespace (`square-manager`, `monitoring`). Those are
  PRs #4 and #5 respectively.
- Verifying DNS resolution against the reserved IPs. That is PR #5.
- Removing `cloudsql-proxy`. That is PR #6.
- Documentation rewrite of README / dns-guide. That is PR #5.

## 4. Affected files

| File | Why |
|---|---|
| `terraform/static_addresses.tf` (new) | 5 `google_compute_address` resources for the external Services. |
| `terraform/outputs.tf` | Append 10 outputs (name + address per Service). |
| `terraform/variables.tf` | No change (`var.region` already exists). |
| `scripts/k8s.py` | Extend `_build_substitution_map` with 5 new `PLACEHOLDER_STATIC_IP_NAME_<SERVICE>` tokens reading from `terraform_outputs`. |
| `scripts/preflight.py` | Add `check_static_ip_quota(project_id, region)` helper. |
| `scripts/commands/init.py` | Wire the new quota check into the init-time pre-flight loop (after billing check). |
| `scripts/verify.py` | Add `import json` to module-level imports (currently missing). Add `check_static_ips_reserved(project_id, region)` that lists the 5 reserved addresses by name with their assigned IPs. |
| `scripts/commands/verify.py` | Add `static_ips_reserved` to the help string and `args_map`. |
| `tests/test_k8s.py` | Add tests for the new placeholder tokens (mock terraform_outputs). |
| `tests/test_preflight.py` | Add a test for `check_static_ip_quota` with a stubbed `gcloud` response. |
| `tests/test_verify.py` | Add a test for `check_static_ips_reserved` with a stubbed `gcloud` response. |

No `k8s/` directory file is touched in this PR.

## 5. Exact code changes

### 5.1 `terraform/static_addresses.tf` (new file)

```hcl
###############################################################################
# Regional static IPs for externally exposed Kubernetes Services.
#
# These addresses are referenced by Service annotations in PR #3a via
# kubernetes.io/ingress.global-static-ip-name. They exist in this PR
# only as reservations so the addresses are stable across the
# subsequent Service-type flip.
###############################################################################

locals {
  external_services = toset([
    "api-manager",
    "hook-manager",
    "admin",
    "talk",
    "meet",
  ])
}

resource "google_compute_address" "external_service" {
  for_each = local.external_services

  name         = "${each.key}-static-ip"
  region       = var.region
  address_type = "EXTERNAL"
}
```

Naming: `<service>-static-ip` exactly as confirmed by pchero in the
parent plan §12.1.

### 5.2 `terraform/outputs.tf` (append, after existing Load Balancers block)

```hcl
###############################################################################
# External Service Static IPs (PR #2 of self-hosting redesign)
###############################################################################

output "api_manager_static_ip_name" {
  description = "Reserved static-IP name for the api-manager Service annotation"
  value       = google_compute_address.external_service["api-manager"].name
}

output "api_manager_static_ip_address" {
  description = "Reserved static-IP address for api-manager"
  value       = google_compute_address.external_service["api-manager"].address
}

output "hook_manager_static_ip_name" {
  description = "Reserved static-IP name for the hook-manager Service annotation"
  value       = google_compute_address.external_service["hook-manager"].name
}

output "hook_manager_static_ip_address" {
  description = "Reserved static-IP address for hook-manager"
  value       = google_compute_address.external_service["hook-manager"].address
}

output "admin_static_ip_name" {
  description = "Reserved static-IP name for the admin Service annotation"
  value       = google_compute_address.external_service["admin"].name
}

output "admin_static_ip_address" {
  description = "Reserved static-IP address for admin"
  value       = google_compute_address.external_service["admin"].address
}

output "talk_static_ip_name" {
  description = "Reserved static-IP name for the talk Service annotation"
  value       = google_compute_address.external_service["talk"].name
}

output "talk_static_ip_address" {
  description = "Reserved static-IP address for talk"
  value       = google_compute_address.external_service["talk"].address
}

output "meet_static_ip_name" {
  description = "Reserved static-IP name for the meet Service annotation"
  value       = google_compute_address.external_service["meet"].name
}

output "meet_static_ip_address" {
  description = "Reserved static-IP address for meet"
  value       = google_compute_address.external_service["meet"].address
}
```

Output naming convention chosen to match the existing `kamailio_*`
and `rtpengine_*` output patterns in this file.

### 5.3 `scripts/k8s.py::_build_substitution_map` extension

Add these entries to the `return {...}` dict, after the existing
`# Terraform outputs` block:

```python
        # External Service static IPs (PR #2 of self-hosting redesign).
        # Manifests start referencing these in PR #3a; for now the
        # tokens resolve so the substitution map stays consistent.
        "PLACEHOLDER_STATIC_IP_NAME_API_MANAGER": terraform_outputs.get(
            "api_manager_static_ip_name", "api-manager-static-ip"
        ),
        "PLACEHOLDER_STATIC_IP_NAME_HOOK_MANAGER": terraform_outputs.get(
            "hook_manager_static_ip_name", "hook-manager-static-ip"
        ),
        "PLACEHOLDER_STATIC_IP_NAME_ADMIN": terraform_outputs.get(
            "admin_static_ip_name", "admin-static-ip"
        ),
        "PLACEHOLDER_STATIC_IP_NAME_TALK": terraform_outputs.get(
            "talk_static_ip_name", "talk-static-ip"
        ),
        "PLACEHOLDER_STATIC_IP_NAME_MEET": terraform_outputs.get(
            "meet_static_ip_name", "meet-static-ip"
        ),
```

Default fallback strings match the actual address names so a missing
`terraform_outputs` entry does not break manifest rendering when
PR #3a starts using them.

### 5.4 `scripts/preflight.py::check_static_ip_quota` (new function)

```python
def check_static_ip_quota(project_id: str, region: str, needed: int = 5) -> bool:
    """Check that the GCP project has at least *needed* regional
    static-IP slots free in *region*. Returns True on sufficient quota.

    Uses ``gcloud compute regions describe <region>`` and parses the
    STATIC_ADDRESSES quota. A return of False does not abort the
    install on its own; ``cmd_init`` decides whether to treat the
    shortage as fatal.
    """
    from scripts.utils import _validate_cmd_arg
    _validate_cmd_arg(project_id, "project_id")
    _validate_cmd_arg(region, "region")
    result = run_cmd(
        [
            "gcloud", "compute", "regions", "describe", region,
            "--project", project_id, "--format=json",
        ],
        timeout=30,
    )
    if result.returncode != 0:
        return False
    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError:
        return False
    for q in data.get("quotas", []):
        if q.get("metric") == "STATIC_ADDRESSES":
            limit = q.get("limit", 0)
            usage = q.get("usage", 0)
            return (limit - usage) >= needed
    return False
```

Imports a `json` (already imported at module top) and `run_cmd` from
the existing module imports. The dedicated test stubs `run_cmd` to
return a synthetic gcloud JSON.

### 5.5 `scripts/commands/init.py` wiring

After the existing GCP project + billing pre-flight calls, add:

```python
from scripts.preflight import check_static_ip_quota

# ... inside the init flow, after billing check passes ...
if not check_static_ip_quota(project_id, region, needed=5):
    print_warning(
        f"Region {region} may not have 5 free STATIC_ADDRESSES slots. "
        f"Request a quota increase before deploying: "
        f"gcloud compute regions describe {region} --project {project_id}"
    )
    # Non-fatal: operator may have headroom we cannot see, or may be
    # planning to deploy in a different region.
```

The check is non-fatal so legitimate edge cases (gcloud described
quota lags real quota, operator plans to apply with `-target` first,
etc.) do not block init. The warning matches the spirit of the
pre-flight warning style elsewhere in the file.

### 5.6 `scripts/verify.py::check_static_ips_reserved` (new function)

Module-level: add `import json` to the top of `scripts/verify.py`
(currently absent; the new function uses `json.loads` and
`json.JSONDecodeError`).

```python
def check_static_ips_reserved(project_id: str, region: str) -> dict:
    """Check that the 5 expected external-service static IPs are
    reserved in GCP. Returns a result dict.

    Lists `gcloud compute addresses list` filtered to the install's
    region; passes if all 5 expected names are present.
    """
    def _check():
        from scripts.utils import _validate_cmd_arg
        _validate_cmd_arg(project_id, "project_id")
        _validate_cmd_arg(region, "region")
        expected = {
            "api-manager-static-ip",
            "hook-manager-static-ip",
            "admin-static-ip",
            "talk-static-ip",
            "meet-static-ip",
        }
        cmd = [
            "gcloud", "compute", "addresses", "list",
            "--project", project_id,
            "--filter", f"region:{region}",
            "--format=json",
        ]
        result = run_cmd(cmd, capture=True, timeout=30)
        if result.returncode != 0:
            return "fail", f"gcloud error: {result.stderr.strip()}"
        try:
            data = json.loads(result.stdout)
        except json.JSONDecodeError:
            return "fail", "could not parse gcloud output"
        found = {a.get("name", "") for a in data}
        missing = expected - found
        if missing:
            return "warn", f"missing: {sorted(missing)}"
        # Build IP list for operator reference.
        ips = {
            a.get("name", ""): a.get("address", "")
            for a in data
            if a.get("name", "") in expected
        }
        ip_str = " ".join(f"{n}={a}" for n, a in sorted(ips.items()))
        return "pass", ip_str

    status, message, elapsed = _timed(_check)
    return _make_result("Static IPs", status, message, elapsed)
```

Added to the orchestrator (`run_all_checks`) in the same file:

```python
    # Static IPs reserved (PR #2 of self-hosting redesign)
    region = config.get("region", "")
    if project_id and region:
        results.append(check_static_ips_reserved(project_id, region))
```

### 5.7 `scripts/commands/verify.py` per-check wiring

In `args_map`, add:

```python
            "check_static_ips_reserved": (project_id, config_dict.get("region", "")),
```

In the available-checks error message, append `static_ips_reserved`.

### 5.8 `tests/`

Three new test functions:

- `test_k8s.py::test_substitution_map_includes_static_ip_tokens`:
  stubs `terraform_outputs` with the 10 new outputs, asserts all 5
  `PLACEHOLDER_STATIC_IP_NAME_*` tokens are present in the returned
  dict, and asserts the fallback values when `terraform_outputs` is
  empty.
- `test_preflight.py::test_check_static_ip_quota_sufficient` and
  `test_check_static_ip_quota_insufficient`: stub `run_cmd` to
  return a synthetic gcloud JSON with limit=8, usage=2 (pass) and
  limit=8, usage=5 (fail).
- `test_verify.py::test_check_static_ips_reserved_all_present` and
  `test_check_static_ips_reserved_missing`: stub `run_cmd` similarly.

Each test stubs at the `scripts.preflight.run_cmd` (or `scripts.verify.run_cmd`)
import boundary so no real `gcloud` calls happen in CI.

## 6. Wire-field / API surface checklist

This PR touches three external surfaces:

| Surface | Field / call | Source of truth |
|---|---|---|
| `gcloud compute regions describe <region>` JSON | `quotas[].metric == "STATIC_ADDRESSES"`, `.limit`, `.usage` | Google Cloud Compute Engine API documentation. |
| `gcloud compute addresses list --filter="region:<region>" --format=json` | array of `{ name, address, region, addressType }` | Compute Engine API. |
| `kubernetes.io/ingress.global-static-ip-name` annotation | string value matching the GCP address `name` | GKE LoadBalancer Service docs. No manifest references it in THIS PR; checklist included for forward compatibility with PR #3a. |

All three are read-only in this PR. No mutating GCP API calls are
added by code; mutating calls happen only through Terraform.

## 7. Verification plan

Per-edit checklist run from the worktree root:

1. `cd terraform && terraform fmt -check static_addresses.tf outputs.tf`
   succeeds.
2. `cd terraform && terraform validate` succeeds (requires a
   provider configured; in CI this is the standard `terraform init`
   then `terraform validate` flow). The PR description includes
   manual confirmation since the install repo's CI does not run
   Terraform.
3. `python3 -c "import ast; ast.parse(open('scripts/k8s.py').read());
   ast.parse(open('scripts/preflight.py').read());
   ast.parse(open('scripts/verify.py').read());
   ast.parse(open('scripts/commands/verify.py').read());
   ast.parse(open('scripts/commands/init.py').read())"`
   succeeds.
4. `pytest tests/test_k8s.py tests/test_preflight.py tests/test_verify.py`
   passes; new tests included.
5. Dry-run the orchestrator (no cluster needed) to confirm the new
   `Static IPs` check is emitted:

   ```bash
   cd <worktree-root>
   python3 -c "
   from scripts.verify import run_all_checks
   results = run_all_checks(
       {'gcp_project_id': 'fake-proj', 'zone': 'fake-zone-a', 'region': 'fake-region', 'domain': 'example.com'}
   )
   for r in results:
       print(r['name'], '->', r['status'])
   "
   ```

   Expected output: includes a line beginning with `Static IPs`.
   Status will be `fail` (no live gcloud) but the line MUST be
   present.

6. `bash scripts/dev/check-plan-sensitive.sh docs/plans/2026-05-11-reserve-static-ips.md`
   returns exit 0.

## 8. Sensitive-data audit (gate to merge)

Run `bash scripts/dev/check-plan-sensitive.sh
docs/plans/2026-05-11-reserve-static-ips.md` before push. Expected
exit 0. The plan uses only `example.com`, `203.0.113.x`,
`<your-project>`, `fake-proj`, `fake-zone-a`, `fake-region` as
placeholders; no real production identifiers.

## 9. Rollout / risk

1. **No behavior change for existing deployments.** Existing
   installs continue to use ingress + cert-manager. The 5 reserved
   addresses sit unused in GCP until PR #3a applies the annotation.
2. **Cost from the moment this PR's Terraform applies.** GCP charges
   for reserved static IPs that are NOT in use (small, ~$0.005/hour
   per address as of writing; verify against current pricing). With
   5 unused reservations the cost is non-zero from PR #2 apply until
   PR #3a apply. The README warning + the per-PR commit body call
   this out so operators can choose to delay PR #2 apply until they
   are ready to do PR #3a soon after.
3. **Quota false negative.** `gcloud compute regions describe` may
   return stale quota data. The pre-flight is non-fatal (warning)
   to avoid blocking a legitimate apply.
4. **Forward dependency.** PR #3a relies on the address names being
   exactly `<service>-static-ip`. The Terraform `for_each` makes the
   names deterministic; the outputs surface them; the placeholder
   defaults match.
5. **Test surface.** New tests use `unittest.mock.patch` against the
   `run_cmd` import in each module under test. Existing test
   patterns already do this; no new test infrastructure.

## 10. Open questions

None blocking. Parent plan §12 decisions already resolved the
naming, region, and ordering questions this PR depends on.

## 11. Approval status

- [ ] Sensitive-data audit (§8) passes locally
- [ ] Design approved by independent reviewer (loop min 2)
- [ ] PR approved by independent reviewer (loop min 3)
- [ ] Merged by pchero (CEO/CTO)
