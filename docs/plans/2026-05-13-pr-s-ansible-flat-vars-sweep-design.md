# PR-S. ansible_runner flat-var sweep for kamailio_internal_lb_ip + rtpengine_socks

Status. Design v2 (draft, supersedes v1 after iter-1 review)
Author. Hermes (CPO)
Date. 2026-05-13 (v1), 2026-05-13 v2
Branch. NOJIRA-PR-S-ansible-flat-vars-sweep
Builds on. PR-D2c (closing PR of D-series).

## 1. Problem statement

PR-D2c smoke dogfood (2026-05-13) showed Kamailio container CrashLoops with `ERROR: Missing required environment variables: KAMAILIO_INTERNAL_LB_ADDR, RTPENGINE_SOCKS, HOMER_URI, REDIS_CACHE_ADDRESS`. Root-cause analysis splits the four into two categories.

**Hot-fix territory (this PR).**

- `KAMAILIO_INTERNAL_LB_ADDR`. terraform output `kamailio_internal_lb_ip` already produces a real value (`10.0.0.2` on dogfood, from static `google_compute_address.kamailio_lb_internal`). But `scripts/ansible_runner.py::_write_extra_vars` flat-vars `kamailio_external_lb_ip` only, not internal. Group_vars default is empty string → env.j2 renders empty value → docker-compose passes empty to container → container fails. Same shape as the PR-D2c `kamailio_auth_db_url` gap.
- `RTPENGINE_SOCKS`. Required format per the existing group_vars comment is `udp:<ip1>:22222 udp:<ip2>:22222`. terraform output `rtpengine_external_ips` (list) already exposes the IPs. No producer derives the joined string.

**Out of scope (GAP-44 territory, deferred to PR-R / PR-T / PR-U).**

- `redis_lb_ip` / `rabbitmq_lb_ip` / `asterisk_*_lb_ip`. k8s Service `type: LoadBalancer` IPs that only exist after `k8s_apply`. Cannot be fixed by adding a flat-var because pipeline order is `ansible_run → k8s_apply`.
- `HOMER_URI`. Operator-supplied external monitoring endpoint. Belongs in config schema + wizard step.

## 2. Production extraction

- Existing flat-var producers in `_write_extra_vars` (lines 67-118 of `scripts/ansible_runner.py` post-D2c). `kamailio_internal_ips`, `rtpengine_external_ips`, `kamailio_external_lb_ip`, `kamailio_auth_db_url`. No `kamailio_internal_lb_ip`, no `rtpengine_socks`.
- terraform output names. `kamailio_internal_lb_ip` (string), `rtpengine_external_ips` (list of strings).
- Consumer template. `ansible/roles/kamailio/templates/env.j2` lines 20 (`KAMAILIO_INTERNAL_LB_ADDR={{ kamailio_internal_lb_ip }}`) and 36 (`RTPENGINE_SOCKS={{ rtpengine_socks }}`).
- Group_vars defaults (post-D2c). `kamailio_internal_lb_ip: ""` in `inventory/group_vars/all.yml`, `rtpengine_socks: ""` in `inventory/group_vars/kamailio.yml`. Both are bare empty strings; extra-vars precedence will override.
- rtpengine_socks port. monorepo `voip-rtpengine` Helm chart and the existing `homer_uri` group_vars comment confirm port 22222 for ng control protocol.
- Why not fix in `ansible/inventory/gcp_inventory.py`. That inventory plugin already exposes `kamailio_internal_lb_ip` via `all.vars` (lines 116-118, 189). However Ansible precedence is `group_vars > inventory plugin all.vars`, so the explicit empty default in `inventory/group_vars/all.yml:24` shadows the plugin value. We fix at extra-vars (highest precedence) rather than touching group_vars or inventory plugin so the change is layer-correct and mirrors the PR-D2c pattern.

## 3. Producer→consumer trace

| Producer change | Consumer file. line | Read path | Verification |
| --- | --- | --- | --- |
| New ansible flat-var `kamailio_internal_lb_ip` (overrides empty group_vars default) | `ansible/roles/kamailio/templates/env.j2:20` `KAMAILIO_INTERNAL_LB_ADDR={{ kamailio_internal_lb_ip }}` | env.j2 already reads the var. extra-vars JSON > group_vars precedence. | `grep -n kamailio_internal_lb_ip ansible/roles/kamailio/templates/env.j2 inventory/group_vars/all.yml` |
| New ansible flat-var `rtpengine_socks` derived from `rtpengine_external_ips` | `ansible/roles/kamailio/templates/env.j2:36` `RTPENGINE_SOCKS={{ rtpengine_socks }}` | Same. | `grep -n rtpengine_socks ansible/roles/kamailio/templates/env.j2 inventory/group_vars/kamailio.yml` |

No new k8s, no new terraform, no new schema. Pure producer-side fix.

## 4. Implementation diff

Target. `scripts/ansible_runner.py::_write_extra_vars`.

After the existing `kamailio_external_lb_ip` line (post-D2c, line ~114), add.

```python
    ansible_vars["kamailio_internal_lb_ip"] = terraform_outputs.get(
        "kamailio_internal_lb_ip", ""
    )
    ansible_vars["rtpengine_socks"] = _build_rtpengine_socks(terraform_outputs)
```

Helper near `_build_kamailio_auth_db_url`.

```python
def _build_rtpengine_socks(terraform_outputs: dict[str, Any]) -> str:
    """Return the RTPENGINE_SOCKS string for env.j2 template.

    Format. space-separated `udp:<ip>:22222` per ng-protocol endpoint. Sourced
    from terraform output `rtpengine_external_ips` (list of strings). Returns
    "" if the list is missing or empty so dev / early-apply flows do not
    crash; group_vars/kamailio.yml then keeps Kamailio's existing fallback.
    Port 22222 is the rtpengine ng control protocol UDP port (confirmed via
    the existing kamailio.yml group_vars comment).
    """
    ips = terraform_outputs.get("rtpengine_external_ips", []) or []
    if not isinstance(ips, list):
        return ""
    parts = [f"udp:{ip}:22222" for ip in ips if isinstance(ip, str) and ip.strip()]
    return " ".join(parts)
```

## 5. Test plan (8 cases, 3 classes)

File. `tests/test_pr_s_ansible_flat_vars.py`.

| Class | Cases | What it verifies |
| --- | --- | --- |
| `TestBuildRtpengineSocks` | 6 | (a) single IP returns `udp:1.2.3.4:22222`. (b) multi-IP returns space-joined string in input order. (c) empty list returns `""`. (d) non-list / `None` returns `""` (defensive). (e) mixed list `["1.2.3.4", "", "  ", "5.6.7.8"]` returns `"udp:1.2.3.4:22222 udp:5.6.7.8:22222"` (locks `ip.strip()` element filter). (f) heterogeneous types `[1, "1.2.3.4", None]` returns `"udp:1.2.3.4:22222"` (locks `isinstance(ip, str)` element filter). |
| `TestWriteExtraVarsIncludesKamailioInternalLbIp` | 1 | `_write_extra_vars` JSON top-level `kamailio_internal_lb_ip` matches the terraform output value. |
| `TestWriteExtraVarsIncludesRtpengineSocks` | 1 | `_write_extra_vars` JSON top-level `rtpengine_socks` matches `_build_rtpengine_socks` derivation. |

Total. 8 cases.

## 6. Synthetic injection mutants (gate ≥ 4)

| Mutant | Trips |
| --- | --- |
| 1. `_build_rtpengine_socks` joins on `,` instead of ` ` | `TestBuildRtpengineSocks` (b) |
| 2. Port literal 22222 changed to 33333 | `TestBuildRtpengineSocks` (a) |
| 3. `_write_extra_vars` skips kamailio_internal_lb_ip insertion | `TestWriteExtraVarsIncludesKamailioInternalLbIp` |
| 4. `_write_extra_vars` skips rtpengine_socks insertion | `TestWriteExtraVarsIncludesRtpengineSocks` |
| 5. Helper omits the `udp:` prefix | `TestBuildRtpengineSocks` (a) |
| 6. Helper raises on empty list (drops the `or []` empty-input guard so `not isinstance(ips, list)` path is exercised differently) | `TestBuildRtpengineSocks` (c) |
| 7. Helper drops `ip.strip()` element filter (empty/whitespace strings get joined as `udp::22222`) | `TestBuildRtpengineSocks` (e) |
| 8. Helper drops `isinstance(ip, str)` element filter (non-string elements crash the f-string or join as `udp:1:22222`) | `TestBuildRtpengineSocks` (f) |

Target. 8/8.

## 7. Smoke dogfood (after merge)

Re-run the same PR-D2c smoke sequence on `voipbin-install-dev`. Expected post-PR-S outcome.

- `KAMAILIO_INTERNAL_LB_ADDR=10.0.0.2` in `.env` (was empty).
- `RTPENGINE_SOCKS=udp:34.44.164.191:22222` in `.env` (was empty).
- Kamailio container still CrashLoops (GAP-44 vars `HOMER_URI`, `REDIS_CACHE_ADDRESS`, `ASTERISK_*_LB_ADDR` still empty). This is **expected**. PR-S closes only the hot-fix-shaped 2 of 4 missing vars; the remaining 2 categories belong to PR-R/PR-T/PR-U.

Acceptance for PR-S is the .env line population, not container health.

## 8. Verification

- pytest baseline+6 green.
- terraform fmt unchanged (no terraform changes).
- sensitive scan clean.
- mutant ≥ 4 (target 8/8).
- design review iter 1+2. PR review iter 1+2+3.
- main drift check before push/merge.

## 9. Risk / rollback

| Risk | Mitigation |
| --- | --- |
| rtpengine_external_ips could be a single-element list with empty string (terraform bug) | helper filters by `ip.strip()`. |
| Port 22222 wrong for this deployment | Verified via existing group_vars comment + monorepo rtpengine source. |
| Helper consumes a future renamed terraform output | grep guard in design §3; rename would surface in PR review iter 1. |

Rollback. `git revert` of the merge re-renders `.env` with empty `KAMAILIO_INTERNAL_LB_ADDR` and `RTPENGINE_SOCKS`. No data loss.

## 10. Open questions

None.

## Iter-N review response summary

### Iter 1 (design review, 2026-05-13)

iter-1 findings (1-5) and resolution.

- I1. §2/§3 missing precedence note about `gcp_inventory.py` `all.vars` already exposing kamailio_internal_lb_ip → resolved. Added §2 bullet "Why not fix in gcp_inventory.py" with the group_vars > inventory-plugin precedence explanation.
- I2. §5 missing case for `ip.strip()` element filter → resolved. Added TestBuildRtpengineSocks (e) with mixed `["1.2.3.4", "", "  ", "5.6.7.8"]` input.
- I3. §5 missing case for `isinstance(ip, str)` filter → resolved. Added TestBuildRtpengineSocks (f) with `[1, "1.2.3.4", None]` input.
- I4. §6 mutant table needs ≥8 with two new rows → resolved. Added mutants 7 and 8; target now 8/8.
- I5. §6 mutant 6 wording ambiguity → resolved. Clarified to "drops the `or []` empty-input guard so `not isinstance(ips, list)` path is exercised differently".

### Iter 2 (design review, 2026-05-13)

iter-2 findings → APPROVED with no actionable items. iter 2 explicitly verified (a) rtpengine_count=0 path, (b) missing output key, (c) mock signature drift, (d) stylistic precedent matches PR-D2c, (e) §7 standalone verifiability, (f) `udp:` literal correctness against rtpengine ng protocol. No changes.

### Iter 3 (design review, pending)

Awaiting iter 3 review.
