# PR-AC-2 — Kamailio forwarded-LB-IP local route (Design v2)

**Branch:** NOJIRA-PR-AC-2-kamailio-lb-ip-route
**Author:** Hermes (CPO) on behalf of pchero
**Date:** 2026-05-13
**Status:** v2 (addresses R1 CHANGES_REQUESTED)

## Problem

Dogfood iter#13 surfaced a kamailio container fatal:

```
ERROR: udp_init(): bind(8, ...) on 34.134.25.116: Cannot assign requested address
```

The GCE VM has only the internal IP (10.0.0.3) on its NIC. The external IP
34.134.25.116 is owned by a GCP Network Load Balancer (target-pool forwarding-rule)
that forwards SIP/TLS/WSS traffic to the instance. Kamailio runs with
`network_mode: host` and tries to `listen=udp:34.134.25.116:5060`, which the
kernel rejects because that address is not bound to any local interface.

## Production parity finding

Production (voipbin-production, europe-west4) runs the same kamailio config,
same docker host-network mode, with the same target-pool forwarding-rule
pattern, and Kamailio binds the external LB IP successfully.

Comparison (captured live, 2026-05-13):

| Item | Production | Dev (failing) |
|---|---|---|
| GCP forwarding-rule + target-pool | identical | identical |
| Instance attached to target-pool | yes | yes |
| `ip_nonlocal_bind` sysctl | `0` | `0` |
| `lo` alias for LB IP | none | none |
| **`google-guest-agent.service`** | **active** | **inactive (dead)** |
| **`ip route show table local`** entry for LB IP | **present** (`local 34.90.68.237 dev ens4 proto 66 scope host`) | **absent** |
| Kamailio bind on LB IP | success | fail |

The decisive difference is the kernel's `local` route table: production has

```
local 34.90.68.237 dev ens4 table local proto 66 scope host
```

added by `google-guest-agent` (journal: `Changing forwarded IPs for ... from
[] to ["10.164.0.20" "34.90.68.237"] by adding [...]`). This is the
AnyIP-style mechanism: with the entry present the kernel treats the IP as
locally owned and `bind()` succeeds without `ip_nonlocal_bind=1` or a `lo:`
alias.

On dev, the newer `google-guest-agent 20260423.01` package on Debian 12
launches a manager that repeatedly fails to start its core plugin
(`context deadline exceeded` in `google-guest-agent-manager` journal), so the
forwarded-IP module never runs and the route is never added.

Install repo will take ownership of the route itself rather than chase
guest-agent fragility through systemd unit manipulation. Production is
unaffected (idempotent no-op).

## Decision

Add an ansible task block in the `kamailio` role that, before docker compose
pull/up:

1. Writes a `voipbin-kamailio-lb-routes.service` systemd oneshot unit that
   adds `local <LB_IP> dev <iface> scope host` for the external + internal
   LB IPs on every boot (reboot persistence).
2. Runs the service immediately (idempotent: pre-check via
   `ip route show table local match <IP>/32`).

Reasoning for systemd-on-boot: `ip route add` is runtime-only. On dev where
guest-agent doesn't auto-add, a reboot would erase the route and kamailio
would crashloop until the next ansible-pull. We will not rely on
ansible-pull running pre-compose-up on every boot because (a) ansible-pull is
not currently a systemd timer in this role, (b) docker compose's restart
policy may bring kamailio up before any ansible run.

## Scope (what changes)

### File-level changes

1. `ansible/roles/kamailio/templates/voipbin-kamailio-lb-routes.service.j2` (new)
   - systemd oneshot unit, `Type=oneshot`, `RemainAfterExit=yes`
   - `Before=docker.service`, `After=network-online.target`, `Wants=network-online.target`
   - Single `ExecStart=/usr/local/sbin/voipbin-kamailio-lb-routes` (script that handles loop + pre-check + add)
   - Unit is the install repo's owned artifact; production parity will get same template via separate PR

2. `ansible/roles/kamailio/templates/voipbin-kamailio-lb-routes.sh.j2` (new)
   - bash script (rendered with vars)
   - For each non-empty LB IP, pre-check
     `ip route show table local match <IP>/32 type local | grep -q .`; if
     absent, `ip route add local <IP> dev <iface> scope host`.
   - No `|| true`. Exit code from `ip route add` propagates (script exits non-zero
     on any real failure).
   - Output: per-IP `added` / `present` line for journal observability.

3. `ansible/roles/kamailio/tasks/main.yml`
   - New task block (placed between "Generate .env" and "Pull latest Docker images"):
     - Template the shell script to `/usr/local/sbin/voipbin-kamailio-lb-routes` mode `0755`
     - Template the systemd unit to `/etc/systemd/system/voipbin-kamailio-lb-routes.service` mode `0644`
     - `systemd: daemon_reload: yes`
     - `systemd: name=voipbin-kamailio-lb-routes, enabled=yes, state=started`
   - All tasks `become: true` (explicit; do not rely on role-level only — R1 M11).
   - All tasks tagged `lb-route` and `deploy`.

4. `tests/test_pr_ac_2_kamailio_lb_ip_route.py` (new)
   - Static assertions on `main.yml`:
     - Three new tasks present with expected names
     - Order: before "Pull latest Docker images", after "Generate .env file"
     - All three have `become: true` (R1 M11)
     - All three have `lb-route` tag
   - Static assertions on `voipbin-kamailio-lb-routes.sh.j2`:
     - Uses `ip route show table local match {{ip}}/32 type local` (R1 M10 hardening)
     - Uses `ip route add local {{ip}} dev {{iface}} scope host` exactly (R1 M7 type + M8 scope)
     - Uses `ansible_default_ipv4.interface` variable (R1 M9)
     - References `kamailio_external_lb_ip` and `kamailio_internal_lb_ip`
     - No `|| true` anywhere (R1 main point #1)
     - Empty IP guarded (per-IP `[ -n "$ip" ]`)
   - Static assertions on `voipbin-kamailio-lb-routes.service.j2`:
     - `Type=oneshot`, `RemainAfterExit=yes`
     - `Before=docker.service`
     - `After=network-online.target` + `Wants=network-online.target`
   - Mutant matrix: 11 mutations (programmatic):
     - M1 drop external task
     - M2 drop internal task
     - M3 wrong variable name (`kamailio_external_ip` typo)
     - M4 task ordering after compose-pull
     - M5 missing empty-string guard
     - M6 missing pre-check (would always try add → loud failure on second run)
     - **M7 route type `unicast` instead of `local`**
     - **M8 route scope `link` instead of `host`**
     - **M9 hardcoded `dev ens4` instead of `ansible_default_ipv4.interface`**
     - **M10 pre-check via plain `grep` substring (no `match /32 type local`)**
     - **M11 missing `become: true` on tasks**

5. `scripts/dev/pr_ac_2_mutant_harness.py` (new)
   - Programmatic generation + assertion; 11/11 must fail static test.

### Production safety (expanded per R1)

- Kernel returns `RTNETLINK answers: File exists (EEXIST)` if a duplicate
  `local <IP> dev <iface> scope host` route is added (regardless of `proto`).
  Our pre-check via `ip route show table local match <IP>/32 type local` will
  match guest-agent's `proto 66` entry on production and skip the add — true
  idempotency. We no longer mask `ip route add` failures with `|| true`, so
  any unexpected failure (wrong iface, permission, table missing) surfaces
  loudly in journal + systemd-unit-failed state.

- On production (guest-agent active, routes already there): the systemd
  oneshot will run, pre-check matches, script logs `present`, exit 0.
  daemon_reload + enable are idempotent. No drift.

- On production (hypothetical future where guest-agent breaks the same way
  dev did): our oneshot adds the route on boot. We are no longer at the
  mercy of guest-agent's release schedule.

- **Operational canary (R3 review):** On production the shim is expected to
  remain a no-op and journal entries should always say `route present: ...`.
  A `route added: ...` line in production journal is the canary that
  guest-agent's forwarded-IP module has regressed and the shim is now the
  sole mechanism keeping Kamailio's bind alive. Pages/alerts on this line
  are an operational followup (separate ticket).

## Out of scope (with explicit followup tracking)

- monorepo `voip-kamailio-ansible` parity (porting the same systemd unit
  there). **Followup:** filed as monorepo issue
  `voipbin/monorepo-voip#PARITY-AC-2` (placeholder ID; PR description will
  reference the real issue once filed). Production is currently protected by
  guest-agent runtime but loses protection on any guest-agent regression; the
  parity PR closes that residual risk.
- guest-agent core-plugin failure on Debian 12 + 20260423.01 — operational
  backlog, not blocking dogfood.
- D1/D4 (image pinning, guest-agent version pin) — long-term parity work,
  separate ticket.
- IPv6 LB IPs — not in scope today (no IPv6 forwarding-rule yet). If
  introduced, an `ip -6 route` branch must be added; assumption documented
  here and asserted in the script header comment.
- Multi-NIC instances — assumption: single-NIC (`ansible_default_ipv4.interface`
  is the only NIC). Asserted in the script header comment; multi-NIC would
  require enumerating per-LB destination interface and is out of scope.

## Verification gate (tightened per R1)

Before merge, an actual-execution smoke test will be run:

- **Host:** `instance-kamailio-voipbin-us-central1-a-0` (dev), via gcloud ssh
- **Command sequence (operator runs from the worktree):**
  ```bash
  cd ~/gitvoipbin/install/.worktrees/NOJIRA-PR-AC-2-kamailio-lb-ip-route/ansible
  ANSIBLE_NOCOWS=1 ansible-playbook playbooks/site.yml \
    --extra-vars "@<path-to-extra-vars-from-pipeline>" \
    -e gcp_project=voipbin-install-dev -e gcp_zone=us-central1-a \
    --tags lb-route -v
  ```
- **Expected:**
  - First run: 3 tasks `changed=true` (template script, template unit, systemd start)
  - Post-run on VM: `systemctl is-active voipbin-kamailio-lb-routes` → `active`
  - Post-run on VM: `ip route show table local match 34.134.25.116/32 type local` returns a line
  - Re-run: 0 changed
- **Evidence:** Operator pastes the two `ip route show` outputs and the
  re-run `PLAY RECAP` (`ok=N changed=0`) as a PR comment on PR-AC-2.
- **Then:** Full apply (`./voipbin-install apply --auto-approve`) must show
  iter#14 reaching past `udp_init` without `Cannot assign requested address`.

This extends the install repo's existing actual-execution gate
(`scripts/dev/check-plan-sensitive.sh`) to a new ansible task that touches host
networking. A skill update (`cli-actual-execution-smoke`) will be carried
forward in the PR-AC skill-patch round.

## Review checklist (v2)

- [x] R1 #1 — `|| true` removed; script propagates exit code
- [x] R1 #2 — systemd oneshot for reboot persistence; ordered Before=docker.service
- [x] R1 #3 — mutant matrix expanded with M7, M8, M9, M10, M11 (now 11 total)
- [x] R1 #4 — monorepo parity followup ID placeholder added (real ID before merge)
- [x] R1 #5 — EEXIST behavior documented in Production safety
- [x] R1 #6 — Verification gate tightened with exact host, command, expected output, evidence location

Ready for R2.
