# PR-F — DNS records per-service realignment + `hook.<domain>`

**Date:** 2026-05-12
**Author:** Hermes (CPO)
**Status:** Design v1
**Repo:** `voipbin/install`
**Branch:** `NOJIRA-PR-F-dns-per-service-realignment`
**Parent:** main `20ce352`
**Roadmap slot:** Roadmap v3 §6 PR-F (A-3, GAP-18, CPO decision #6)
**LOC estimate:** ~100

## 1. Context

`terraform/dns.tf` (lines 13–65) currently points all four web-tier DNS A records — `api.<domain>`, `admin.<domain>`, `talk.<domain>`, `meet.<domain>` — at the **same** address: `google_compute_address.kamailio_lb_external.address`. That IP is the SIP edge LB and has nothing to do with the HTTP-tier Services; the records were never re-pointed when per-service external static IPs landed in `terraform/static_addresses.tf` (PR #2 of the self-hosting redesign).

The per-service reservations already exist as `google_compute_address.external_service` (a `for_each` over the local set `{api-manager, hook-manager, admin, talk, meet}`) and are referenced from K8s Service annotations via `kubernetes.io/load-balancer-static-ip-name` in PR #3a. They simply aren't wired into DNS.

Net effect today (production bug, A-3 / GAP-18):
- `api`, `admin`, `talk`, `meet` resolve to the Kamailio SIP edge IP rather than their respective HTTP/Web LBs. End users hitting `https://api.<domain>` land on the SIP TCP/UDP edge.
- `sip.<domain>` → `kamailio_lb_external` is correct (the only DNS record that does what it claims).
- There is **no** DNS record for `hook.<domain>` even though `hook-manager` has both a reserved static IP and (per roadmap) an externally exposed Service for webhook delivery (CPO decision #6).

This PR realigns the four broken records onto their correct per-service addresses, leaves `sip` alone, and adds a new `hook` A record.

## 2. Scope

### 2.1 `terraform/dns.tf` — re-point the four broken A records

For each of the existing `google_dns_record_set` resources `api`, `admin`, `talk`, `meet`, replace the `rrdatas` reference. All other attributes (`count`, `name`, `type`, `ttl`, `managed_zone`) stay untouched.

| Resource | Current `rrdatas` | New `rrdatas` |
|---|---|---|
| `google_dns_record_set.api`   | `[google_compute_address.kamailio_lb_external.address]` | `[google_compute_address.external_service["api-manager"].address]` |
| `google_dns_record_set.admin` | `[google_compute_address.kamailio_lb_external.address]` | `[google_compute_address.external_service["admin"].address]` |
| `google_dns_record_set.talk`  | `[google_compute_address.kamailio_lb_external.address]` | `[google_compute_address.external_service["talk"].address]` |
| `google_dns_record_set.meet`  | `[google_compute_address.kamailio_lb_external.address]` | `[google_compute_address.external_service["meet"].address]` |
| `google_dns_record_set.sip`   | `[google_compute_address.kamailio_lb_external.address]` | **unchanged** (correct as-is) |

Note: per `terraform/static_addresses.tf` the for_each key for the API service is **`"api-manager"`** (hyphen, full name). The Roadmap-v3 task description used the shorthand `api_lb_external` — that resource does not exist in this codebase; the canonical reference is `google_compute_address.external_service["api-manager"]`. Same convention for `hook-manager`.

### 2.2 `terraform/dns.tf` — add `hook.<domain>` A record

Append a new resource, mirroring the existing block style:

```hcl
# DNS A record: hook.<domain> (webhook delivery edge — CPO decision #6)
resource "google_dns_record_set" "hook" {
  count = var.dns_mode == "auto" ? 1 : 0

  name         = "hook.${var.domain}."
  type         = "A"
  ttl          = 300
  managed_zone = google_dns_managed_zone.voipbin[0].name
  rrdatas      = [google_compute_address.external_service["hook-manager"].address]
}
```

Ordering: placed between `meet` and `sip` to keep web-tier records grouped and `sip` last (preserves the "HTTP block then SIP block" visual grouping that this PR is establishing).

### 2.3 Tests — `tests/terraform/test_dns_per_service.py` (new)

Add a small test module that parses `terraform/dns.tf` via `python-hcl2` (already a test dependency — confirm during implementation; if missing, fall back to a regex-based parser as in `test_static_addresses.py`).

Cases (≥7):

1. **`test_api_record_points_to_api_manager_static_ip`** — `rrdatas` of `google_dns_record_set.api` references `external_service["api-manager"]`.
2. **`test_admin_record_points_to_admin_static_ip`** — references `external_service["admin"]`.
3. **`test_talk_record_points_to_talk_static_ip`** — references `external_service["talk"]`.
4. **`test_meet_record_points_to_meet_static_ip`** — references `external_service["meet"]`.
5. **`test_sip_record_still_points_to_kamailio_lb_external`** — regression guard for the one record that must NOT change.
6. **`test_hook_record_exists_and_points_to_hook_manager`** — new record present, correct target, `count`/`ttl`/`type` match siblings.
7. **`test_no_web_tier_record_references_kamailio_lb_external`** — sweep: of `{api, admin, talk, meet, hook}`, none reference `kamailio_lb_external` (catches future regressions of the original bug).

Optional 8th: **`test_dns_records_gated_on_dns_mode_auto`** — every record_set has `count = var.dns_mode == "auto" ? 1 : 0`.

Mock strategy: pure file parsing; no `terraform` binary, no GCP. Matches the existing pattern in `tests/terraform/`.

### 2.4 Documentation

- `README.md` — verified during design: the existing "DNS Records" table (lines ~234–250) already lists all five subdomains (`api`, `hook`, `admin`, `talk`, `meet`) with the correct per-service mapping. No README change required by this PR.
- `docs/operations/dns.md` — does not exist in this repo (verified). N/A.
- `CHANGELOG` / release notes entry: "Fixes A-3 / GAP-18: `api`/`admin`/`talk`/`meet` A records now resolve to their per-service external IPs (previously pointed at the Kamailio SIP edge). New `hook.<domain>` A record added for webhook delivery."

## 3. Out of scope

- Changing `sip.<domain>` (already correct).
- Adding/removing static IP reservations in `terraform/static_addresses.tf` (the five per-service addresses already exist; `hook-manager` is among them).
- K8s Service-side wiring (`kubernetes.io/load-balancer-static-ip-name`) — handled in PR #3a, already in place.
- Cert-manager / TLS issuance for `hook.<domain>` — separate workstream; this PR only publishes DNS.
- `reconcile_outputs` field mappings that would copy these addresses into `config.yaml` (PRs C/D/G).
- Any change to the Kamailio external LB, the SIP edge, or Kamailio routing.
- Migration of pre-existing operator zones with stale A records — see §4.

## 4. Risks

- **DNS cutover blast radius.** Once applied, the four web-tier records flip to new IPs within `ttl=300s`. Operators with traffic on `api/admin/talk/meet` will see clients re-resolve within ~5 min. Mitigation: roadmap-v3 §7 dogfood on `voipbin-install-dev` (no production data) before merge; document the TTL window in the changelog.
- **Per-service LBs not yet healthy at apply time.** The `external_service` static IPs exist as bare reservations until a K8s Service binds them. If DNS is updated before the K8s Services are reconciled, `https://api.<domain>` will resolve to an IP that drops connections. Mitigation: pipeline order already runs `terraform_apply` before `k8s_apply`, but DNS records become authoritative immediately. Acceptable risk for a fresh install (everything is broken in parallel anyway); for an in-place upgrade, operator runbook should note the ordering. Document in changelog.
- **State drift on existing installs.** Operators who manually edited their managed zone to work around the bug will see `terraform plan` overwrite their hand-edits. Mitigation: this is the desired behavior; call it out in release notes.
- **Wrong for_each key.** `static_addresses.tf` uses `api-manager` and `hook-manager` (hyphen, full name) but `admin`/`talk`/`meet` are bare. Easy to typo — tests §2.3 cases 1–4 guard exact key strings.
- **`hook-manager` Service not yet externally exposed.** If PR #3a does not annotate the `hook-manager` K8s Service for the reserved static IP, the new `hook.<domain>` record will resolve to a parked IP. Verify during dogfood; if missing, file a follow-up rather than blocking this PR (DNS reservation is independently correct and harmless).
- **`google_dns_record_set` replacement vs in-place update.** Provider behavior: changing `rrdatas` is an in-place update, not destroy+create. Confirmed by reading provider docs / behavior of `terraform plan` on a hand-crafted apply (sanity check during implementation).

## 5. Test plan summary

≥7 new HCL-parsing unit tests in `tests/terraform/test_dns_per_service.py`. No changes to existing tests expected — current suite does not assert DNS targets. Confirm `pytest tests/terraform/ -q` is green. LOC budget: ~100 (≈10 TF + ~80 tests + ~10 comments).

## 6. Smoke dogfood (post-merge)

Per roadmap v3 §7 on `voipbin-install-dev` (no destroy):

1. `voipbin-install init` → `voipbin-install apply` end-to-end on a clean project, OR `terraform apply` against an existing dev project to exercise the in-place `rrdatas` update path.
2. Verify with `gcloud dns record-sets list --zone=<env>-dns-zone`:
   - `api.<domain>`   → `external_service["api-manager"]`  address
   - `admin.<domain>` → `external_service["admin"]`        address
   - `talk.<domain>`  → `external_service["talk"]`         address
   - `meet.<domain>`  → `external_service["meet"]`         address
   - `sip.<domain>`   → `kamailio_lb_external`             address (unchanged)
   - `hook.<domain>`  → `external_service["hook-manager"]` address (new)
3. `dig +short api.<domain> @<one-of-zone-NS>` returns the expected per-service IP.
4. Confirm `terraform plan` is clean on a second run (idempotency). ~15 min.

## 7. Checklist

- [x] Exact resource names verified against `terraform/dns.tf` and `terraform/static_addresses.tf`
- [x] `sip` record preserved (regression test included)
- [x] `hook.<domain>` added with `external_service["hook-manager"]` (CPO decision #6)
- [x] Test plan ≥7 cases, mock-only, no `terraform` binary, no GCP
- [x] Risks (cutover TTL, K8s readiness, state drift) documented
- [x] Dogfood-readiness state confirmed
- [ ] Design review iter 1
- [ ] Design review iter 2
