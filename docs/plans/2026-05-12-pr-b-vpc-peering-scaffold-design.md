# PR-B — Terraform VPC peering scaffold + static-IP reconcile registry

**Date:** 2026-05-12
**Author:** Hermes (CPO)
**Status:** Design v1
**Repo:** `voipbin/install`
**Branch:** `NOJIRA-PR-B-vpc-peering-scaffold`
**Parent:** main `20ce352` (PR-A merged as #16)
**Roadmap slot:** PR-B (Phase 2, parallel; no critical-path dependency on PR-A; Phase 2 also runs PR-F/G/H)
**LOC estimate:** ~250 (per roadmap v3 §6)
**Gaps closed:** GAP-03 (no VPC peering), GAP-10 (5 static IPs absent from reconcile registry)

## 1. Context

Production reference (extracted via `gcloud compute networks peerings list --project=<prod-project>`):
- VPC peering reserved range `google-managed-services-default` = `<prod-peering-cidr>` on the `default` VPC.
- `servicenetworking-googleapis-com` connection consumes it for managed services (Cloud SQL, Memorystore, etc.).
- Install repo uses a custom `${env}-vpc` (`terraform/network.tf:2`), so peering must attach to the custom network, not `default`.

Today (`terraform/network.tf`) provisions only:
- `google_compute_network.voipbin` (custom-mode VPC).
- `google_compute_subnetwork.voipbin_main` (primary + pods + services secondary ranges).

No VPC peering exists. Cloud SQL is still public-IP (`terraform/cloudsql.tf:21-24`); PR-C will flip it to private. PR-B prepares the peering scaffold so PR-C is a focused two-line `ip_configuration` change.

Separately, `terraform/static_addresses.tf:20-26` reserves 5 regional external static IPs via a `for_each` (`api-manager`, `hook-manager`, `admin`, `talk`, `meet`) but `scripts/terraform_reconcile.py::build_registry` does NOT include them. Partial-apply failures that create the addresses but fail mid-`terraform apply` leave operators with 409 conflicts on retry.

## 2. Scope

### 2.1 `terraform/network.tf` — additions

```hcl
# Reserved IP range for VPC peering with Google managed services.
# Matches production's /20 prefix on google-managed-services-default.
resource "google_compute_global_address" "cloudsql_peering" {
  name          = "${var.env}-cloudsql-peering"
  purpose       = "VPC_PEERING"
  address_type  = "INTERNAL"
  prefix_length = var.cloudsql_peering_prefix_length
  network       = google_compute_network.voipbin.id

  depends_on = [time_sleep.api_propagation]
}

# Service Networking connection enables VPC peering with Google managed
# services (Cloud SQL Private IP, Memorystore, etc.).
resource "google_service_networking_connection" "voipbin" {
  network                 = google_compute_network.voipbin.id
  service                 = "servicenetworking.googleapis.com"
  reserved_peering_ranges = [google_compute_global_address.cloudsql_peering.name]
  deletion_policy         = "ABANDON"
}
```

`deletion_policy = "ABANDON"` is the canonical Terraform idiom for `google_service_networking_connection` — destroy operations leave the connection in place (re-applies are idempotent; manual `gcloud services vpc-peerings delete` cleans up on full teardown).

### 2.2 `terraform/variables.tf` — addition

```hcl
variable "cloudsql_peering_prefix_length" {
  description = "Prefix length for the VPC-peering reserved IP range used by Cloud SQL Private IP. Default 20 matches production."
  type        = number
  default     = 20

  validation {
    condition     = var.cloudsql_peering_prefix_length >= 8 && var.cloudsql_peering_prefix_length <= 29
    error_message = "cloudsql_peering_prefix_length must be between 8 and 29."
  }
}
```

### 2.3 `terraform/outputs.tf` — addition

```hcl
output "cloudsql_peering_range_cidr" {
  description = "CIDR of the reserved VPC-peering range. PR-C reconcile_outputs writes this into config.cloudsql_private_ip_cidr."
  value       = "${google_compute_global_address.cloudsql_peering.address}/${google_compute_global_address.cloudsql_peering.prefix_length}"
}
```

No existing outputs renamed/deleted in PR-B (PR-C handles `cloudsql_ip` deletion).

### 2.4 `scripts/terraform_reconcile.py::build_registry` — 6 new entries

Add to the existing flat-list registry (`terraform_reconcile.py:63-316`, same shape as `cloudsql.voipbin` entry at lines ~278-284):

**VPC peering (1 entry)**:
- `tf_address = "google_compute_global_address.cloudsql_peering"`
  - `gcloud_check = ["gcloud", "compute", "addresses", "describe", "${env}-cloudsql-peering", "--global", "--project", project]`
  - `import_id = "projects/${project}/global/addresses/${env}-cloudsql-peering"`

`google_service_networking_connection.voipbin` is **intentionally NOT registered** in reconcile. `gcloud services vpc-peerings list` returns rc=0 with empty stdout when no peering exists, which would false-positive `check_exists_in_gcp` (`scripts/reconcile.py:37-42` treats rc==0 as "exists") and trigger spurious `terraform import` of a non-existent resource. Terraform import for `google_service_networking_connection` is also notoriously fragile (the import ID format `projects/<p>/global/networks/<vpc>:<service>` is undocumented and provider-version-sensitive). The recovery path for an orphaned service-networking connection is operator-driven (see §4 Risks); registry automation does not help.

**Static IPs (5 entries via for_each address `google_compute_address.external_service["<key>"]`)**:
- `api-manager`, `hook-manager`, `admin`, `talk`, `meet`
- Each:
  - `gcloud_check = ["gcloud", "compute", "addresses", "describe", "<key>-static-ip", "--region", region, "--project", project]`
  - `import_id = "projects/${project}/regions/${region}/addresses/<key>-static-ip"`

### 2.5 No changes

- `terraform/cloudsql.tf` — PR-C scope.
- `scripts/pipeline.py` — APPLY_STAGES already correct after PR-A.
- `scripts/terraform_reconcile.py::outputs` — `FIELD_MAP` stays empty in PR-B (PR-C adds first entries).
- `config/schema.py` — no new config keys (`cloudsql_peering_range_cidr` is reconcile-only).

### 2.6 Tests — `tests/test_pr_b_vpc_peering_scaffold.py` (~7 tests)

1. **`test_network_tf_has_global_address`** — parse `terraform/network.tf`, assert `google_compute_global_address.cloudsql_peering` exists with `purpose = "VPC_PEERING"`.
2. **`test_network_tf_has_service_networking_connection`** — assert `google_service_networking_connection.voipbin` exists with `deletion_policy = "ABANDON"`.
3. **`test_peering_prefix_variable`** — `cloudsql_peering_prefix_length` declared in `terraform/variables.tf`, default 20, with validation block.
4. **`test_peering_range_cidr_output`** — `cloudsql_peering_range_cidr` declared in `terraform/outputs.tf`.
5. **`test_registry_includes_peering_global_address`** — `build_registry(InstallerConfig)` returns an entry for `google_compute_global_address.cloudsql_peering`.
6. **`test_registry_excludes_service_networking_connection`** — `build_registry(...)` does **not** contain `google_service_networking_connection.voipbin` (regression guard; see §2.4 rationale).
7. **`test_registry_includes_static_ips`** — registry includes all 5 `google_compute_address.external_service["<key>"]` entries for `{api-manager, hook-manager, admin, talk, meet}`.

Mock strategy: parse Terraform HCL via simple regex / file-content assertions (matches existing `tests/test_*_terraform_*.py` patterns). Registry tests use real `build_registry(InstallerConfig())` against a minimal config fixture.

## 3. Out of scope

- Any `cloudsql.tf` private-IP flip (PR-C).
- Postgres instance (PR-D1).
- Per-app users (PR-D2).
- Operator-supplied `cloudsql_private_ip` removal (PR-E).
- `cloudsql_ip` public-IP output deletion (PR-C).
- `reconcile_outputs` FIELD_MAP wiring (PR-C is first consumer).
- DNS, GCS, RabbitMQ changes (PR-F/G/H).

## 4. Risks

- **`google_service_networking_connection` is sticky on destroy**: mitigated by `deletion_policy = "ABANDON"`. Operators running `voipbin-install destroy` may need to `gcloud services vpc-peerings delete --network ${env}-vpc --service servicenetworking.googleapis.com` to fully clean up. Documented in PR-J operator docs.
- **Orphaned service-networking connection blocks re-apply**: because `google_service_networking_connection.voipbin` is deliberately excluded from the reconcile registry (see §2.4 rationale), a partial-apply failure that creates the connection but loses local state will cause the next `terraform apply` to fail with a 409 "already exists" on the resource. **Manual recovery**: operator runs `gcloud services vpc-peerings delete --network=${env}-vpc --service=servicenetworking.googleapis.com --project=<project>` before retrying. Document this in the PR-J runbook alongside the destroy-cleanup note above. Rationale for the manual cleanup over auto-import: see §2.4 — `gcloud services vpc-peerings list` cannot reliably signal absence (rc=0 on empty stdout) and terraform import for this resource type is unreliable.
- **/20 default allocation**: with custom VPC's primary subnet at `10.0.0.0/16`, pods `10.1.0.0/16`, services `10.2.0.0/20`, the auto-allocated /20 (`google_compute_global_address` with `address_type = INTERNAL` and no explicit `address`) picks from unused 10.x. If operator's project has a tight network plan, override via `cloudsql_peering_prefix_length`.
- **Partial-apply orphan**: if `terraform apply` creates the global address but fails on the service-networking connection, reconcile_imports will re-import the address on next apply. With this PR's registry additions, also true for the 5 static IPs that PR #2 forgot.
- **PR-B alone is non-functional on fresh install**: creates the peering scaffold but Cloud SQL is still public-IP, so the preflight `check_cloudsql_private_ip` still fails (this is intentional — PR-C completes the flip).

## 5. Test plan summary

7 new tests + 0 updates. Target: 395 (post-PR-A) + 7 = **402 tests passing**.

## 6. Smoke dogfood

PR-B alone cannot complete a fresh install (preflight blocks on Cloud SQL public IP sentinel). Smoke dogfood is **deferred to post-PR-C merge** when the full PR-A→PR-B→PR-C chain enables a fresh `terraform apply` to provision private-IP Cloud SQL + peering + per-service IPs.

PR-B verifications without dogfood:
- `terraform validate` succeeds in CI.
- `terraform plan` against a fresh project shows the 2 new resources + 5 newly-registered IPs.
- pytest 402 green.

## 7. Checklist

- [x] Scope grounded in file:line (network.tf:1-28, variables.tf, outputs.tf, static_addresses.tf:10-26, terraform_reconcile.py:63-316)
- [x] Production reference matched (/20 peering, ABANDON deletion policy)
- [x] 6 new registry entries enumerated (service-networking connection intentionally excluded — see §2.4 / §4)
- [x] Variable + validation defined
- [x] Output for PR-C downstream consumer defined
- [x] No scope creep into PR-C/D/E territory
- [x] Test plan: 7 tests, mocked terraform parsing
- [x] PR-G/PR-H independence verified (no file overlap)
- [ ] Design review iter 1
- [ ] Design review iter 2
