# PR-U-3 — Kamailio heplify-client sidecar + HOMER_URI wiring

Status: Draft v3 (iter-1 + iter-2 fixes applied)
Author: Hermes (CPO)
Date: 2026-05-13
Worktree: `~/gitvoipbin/install/.worktrees/NOJIRA-PR-U-3-kamailio-heplify-sidecar`
Branch: `NOJIRA-PR-U-3-kamailio-heplify-sidecar`
Predecessors: PR-U-1 (k8s manifests, merged 60a2038), PR-U-2 (DB + substitution, merged 1f9cf1f)
Successor: none. PR-U series closes with this PR.

---

## 1. Problem statement

PR-U-1 and PR-U-2 stood up the HOMER capture/storage stack on the GKE cluster (`heplify-deployment` Pod + Postgres DBs + sensitive password). The Kamailio side, however, is not yet emitting HEP packets, so HOMER receives nothing. `heplify_lb_ip` is harvested into `terraform_outputs` and injected into Ansible as a flat-var by PR-U-1, but `ansible/inventory/group_vars/kamailio.yml` still ships `homer_uri: ""` and `homer_enabled: "false"`, and no `heplify-client` capture sidecar exists in `ansible/roles/kamailio/templates/docker-compose.yml.j2`.

PR-U-3 closes the loop by porting the production voip-kamailio-docker pattern (`sipcapture/heplify:1.56` sidecar in `network_mode: host`) into the install repo's Ansible role, wiring `HOMER_URI` to the harvested `heplify_lb_ip:9060`, and flipping `homer_enabled` to `"true"` by default for dogfood. After PR-U-3 merges and `voipbin-install apply --stage ansible_run` runs against the dogfood VM, `heplify-client` should boot, sniff SIP traffic on the host interface, and forward HEP packets to the heplify-server internal LoadBalancer (which then writes capture rows into the `homer_data` Postgres database provisioned by PR-U-2).

## 2. Goals (numbered, testable)

1. Add a `heplify-client` service to `ansible/roles/kamailio/templates/docker-compose.yml.j2` mirroring the production pattern: `sipcapture/heplify:1.56` image, `network_mode: host`, `depends_on: kamailio`, json-file logging with 5m/2-file rotation, command-line args `-i any -hs ${HOMER_URI} -m SIP -dim REGISTER`. **The entire service block is wrapped in `{% if homer_enabled | bool and heplify_lb_ip %}` so operators disabling HOMER capture get NO sidecar in their rendered compose file (rather than a broken `-hs :9060` arg).**
2. Wire `homer_uri` in `ansible/inventory/group_vars/kamailio.yml` to render as `{{ heplify_lb_ip }}:9060` ONLY when `heplify_lb_ip` is non-empty. Empty string otherwise. The compose-level Jinja gate (G1) is the primary protection; this is defense-in-depth.
3. Flip default `homer_enabled` from `"false"` to `"true"` so the sidecar starts by default after `ansible_run`.
4. Pin sidecar image version (`1.56` literal) — matches production today.
5. Add Python-side preflight `check_kamailio_homer_uri_present(terraform_outputs, config)` that hard-fails when `heplify_lb_ip` is empty AND `config["homer_enabled"]` is not explicitly `false`. Operator override path: set `homer_enabled: false` in `config.yaml`. The preflight reads from `config`, NOT from Ansible extra-vars (extra-vars feed Ansible, not Python — clarified in iter-1).
6. Add `homer_enabled` as a real Python config key (default `True`) sourced from `config.yaml`. The Python preflight reads it; ansible_runner injects it as a flat-var into Ansible (`homer_enabled: "{{ homer_enabled | string | lower }}"` or equivalent) so Ansible's gate and Python's gate share one source of truth.
7. Add invariant + functional tests: compose template shape (sidecar block present with correct image/network/argv/depends_on, Jinja gate present), group_vars literals, preflight gate behavior, config-key default, ansible_runner flat-var wiring.
8. After PR-U-3 merges and a fresh `voipbin-install apply` cycle completes against dogfood: `docker ps` shows `kamailio-heplify` Up; `docker logs kamailio-heplify` shows packets forwarded; heplify-server logs show row insertions; homer-app UI lists captured traffic.
9. Keep PR strictly within Kamailio docker-compose + ansible config + preflight + tests + 1 config key. NO k8s manifest changes, NO Terraform changes, NO new substitution-map entries.

## 3. Non-goals (explicit scope cuts)

- Asterisk-side HEP capture (Asterisk does not run a heplify-client; future scope).
- RTP capture (`-m SIP` only; RTP is opt-in).
- Auth / SSO for homer-app UI.
- TLS for the HEP forwarder.
- Heplify-client log forwarding to GCP logs.
- Operator override of sidecar image version through config.yaml.
- Profile-level gate for whether Kamailio deploys at all. No `deploy_kamailio` config key exists today (verified iter-1); the install profile is determined by inventory selection, not by a config flag. The preflight does NOT gate on this — when called, it assumes Kamailio is being deployed.

## 4. Affected files (table: file → why)

| File | Why | Change type |
|---|---|---|
| `ansible/roles/kamailio/templates/docker-compose.yml.j2` | Add heplify-client service block, wrap in Jinja gate | modify |
| `ansible/inventory/group_vars/kamailio.yml` | `homer_uri` Jinja-gated render; `homer_enabled: "true"` default | modify (2 lines) |
| `ansible/inventory/group_vars/all.yml` | If `homer_enabled` should be visible across roles, declare here too (verify at impl) | verify (likely no-op) |
| `scripts/preflight.py` | Add `check_kamailio_homer_uri_present(terraform_outputs, config)` | append |
| `scripts/ansible_runner.py` | Invoke new preflight before `_write_extra_vars`; inject `homer_enabled` flat-var | modify |
| `scripts/config.py` (or `config/schema.py`) | Add `homer_enabled: bool = True` config key | modify |
| `tests/test_pr_u_3_kamailio_heplify_sidecar.py` | 17 new test cases (was 16 in v1) | new |
| `docs/plans/2026-05-13-pr-u-3-kamailio-heplify-sidecar-design.md` | This file | new |

Estimated diff: ~150 LOC added, ~15 LOC modified across 8 files (1 doc + 5 code + 1 test + 1 verify-noop = 8 rows; verify rows may collapse). Math: code count is 5 not 4.

## 5. Exact string replacements / API changes

### 5.1 `ansible/roles/kamailio/templates/docker-compose.yml.j2` — append before `volumes:` block

Insertion: after the `kamailio-exporter:` service block, before the top-level `volumes:` key (currently at L83).

```jinja
{% if homer_enabled | bool and heplify_lb_ip %}
  # HEP capture client for Homer (PR-U-3)
  # Mirrors production voip-kamailio-docker. Sniffs host-network SIP traffic
  # and forwards HEP packets to the heplify-server internal LoadBalancer
  # at $HOMER_URI. Block is gated on (homer_enabled AND heplify_lb_ip) so
  # operators who disable HOMER (or whose LB has not been harvested yet)
  # get NO sidecar in the rendered file, rather than a broken `-hs :9060`.
  heplify-client:
    image: sipcapture/heplify:1.56
    container_name: kamailio-heplify
    restart: unless-stopped
    network_mode: host
    command:
      - "-i"
      - "any"
      - "-hs"
      - "${HOMER_URI}"
      - "-m"
      - "SIP"
      - "-dim"
      - "REGISTER"
    depends_on:
      - kamailio
    logging:
      driver: json-file
      options:
        max-size: "5m"
        max-file: "2"
{% endif %}

```

Notes:
- `network_mode: host` is required because heplify-client `-i any` sniffs the host's network interfaces directly. Production parity.
- `${HOMER_URI}` resolves from `env.j2` at docker-compose runtime; the value is `<heplify_lb_ip>:9060` (Ansible renders that string into env.j2 when `homer_enabled == "true"`).
- Logging driver is `json-file` (not `gcplogs` like kamailio/kamailio-exporter) — matches production. Operator can switch later if log volume becomes a concern.
- `depends_on: - kamailio` uses **short-form list syntax** intentionally. The neighboring `kamailio-exporter` service uses **long-form** `depends_on: kamailio: condition: service_healthy` because Compose treats long-form as authoritative for health-conditioned waits. The Kamailio service template does NOT declare a healthcheck (pre-existing state), so the long-form `service_healthy` would block indefinitely. Short-form `depends_on: - kamailio` waits only for the `service_started` event (default), which is correct for a sniffer that just needs the interface up. This is an intentional divergence from kamailio-exporter, not drift.
- The Jinja `{% if %}` evaluates at Ansible template-render time, before the file is written to the VM. The compose file on disk is either fully present (sidecar block) or fully absent (no sidecar block). Docker Compose never sees a partially-rendered file.
- `homer_enabled | bool` accepts `"true"`, `"True"`, `true`, `1` as truthy. Ansible's `| bool` filter is permissive.

### 5.2 `ansible/inventory/group_vars/kamailio.yml` — modify

Two lines, in their actual file positions (NOT adjacent in the source file: L27 and L41):

```yaml
# Line 27 — Homer monitoring section
homer_uri: "{% if heplify_lb_ip %}{{ heplify_lb_ip }}:9060{% endif %}"

# Line 41 — Homer HEP capture section
homer_enabled: "true"
```

Rationale:
- `homer_uri` empty-IP case renders to empty string (not `:9060`). Defense-in-depth alongside the compose-level gate at §5.1.
- `homer_enabled` is hardcoded to literal `"true"` in group_vars as the SAFE default. Operator override is via Ansible `--extra-vars homer_enabled=false` OR via `config.yaml` `homer_enabled: false` (the Python preflight reads config.yaml; ansible_runner injects the resulting value as an Ansible extra-var with precedence over group_vars). No `_flag` suffix indirection — matches PR-U-1's direct flat-var pattern for `heplify_lb_ip` (iter-2 finding B2).

### 5.3 `scripts/preflight.py` — append `check_kamailio_homer_uri_present`

```python
def check_kamailio_homer_uri_present(
    terraform_outputs: dict[str, str],
    config,
) -> None:
    """PR-U-3: assert Kamailio HEP capture has a destination address.

    When `config.homer_enabled` is True (default), the heplify-client
    sidecar in the Kamailio docker-compose needs ${HOMER_URI} to point at
    a real heplify-server LoadBalancer. The harvested `heplify_lb_ip` is
    the source of truth; if it is empty, the Jinja gate in group_vars
    renders an empty HOMER_URI and the sidecar would be skipped entirely
    by the compose-level `{% if homer_enabled and heplify_lb_ip %}` gate.
    That silent-skip is benign on its own, but the operator's intent was
    capture-on. Make the failure explicit at preflight.

    No-op when `config.homer_enabled` is explicitly False.
    """
    if not bool(config.get("homer_enabled", True)):
        return
    lb_ip = (terraform_outputs.get("heplify_lb_ip", "") or "").strip()
    if not lb_ip:
        raise PreflightError(
            "Kamailio HOMER capture is enabled (config.homer_enabled=true) "
            "but heplify_lb_ip is empty in terraform_outputs. Run "
            "`voipbin-install apply --stage reconcile_k8s_outputs` to "
            "harvest the heplify Service LoadBalancer address, or set "
            "`homer_enabled: false` in config.yaml to disable HEP capture."
        )
```

### 5.4 `scripts/ansible_runner.py` — invoke preflight + inject flat-var

Two modifications:

(a) At the top of `ansible_run()`, before `_write_extra_vars`:

```python
def ansible_run(config, terraform_outputs):
    from scripts.preflight import (
        PreflightError,
        check_kamailio_homer_uri_present,
    )

    # PR-U-3: HOMER capture preflight (hard fail).
    try:
        check_kamailio_homer_uri_present(terraform_outputs, config)
    except PreflightError as exc:
        print_error(str(exc))
        return False

    extra_vars_path = _write_extra_vars(config, terraform_outputs)
    # ... existing body unchanged
```

(b) Inside `_write_extra_vars`, after the existing `heplify_lb_ip` line (current L173-175), add the direct flat-var (NO `_flag` suffix; matches PR-U-1's pattern):

```python
    # PR-U-3: HOMER capture toggle. Direct flat-var injection (no _flag
    # suffix indirection) — matches the heplify_lb_ip pattern just above.
    # Ansible extra-vars precedence overrides the group_vars default.
    ansible_vars["homer_enabled"] = (
        "true" if bool(config.get("homer_enabled", True)) else "false"
    )
```

### 5.5 Config schema addition

Add `homer_enabled` as a new boolean property in `config/schema.py:CONFIG_SCHEMA["properties"]`. **Critical iter-2 finding (B1)**: the schema has `"additionalProperties": False` at L111 and ZERO existing boolean properties — every current key is `type: string` or `type: integer`. We must explicitly declare the entry; without it, an operator setting `homer_enabled: false` in config.yaml gets a validation rejection.

Insertion (just before `tmp_bucket` or after, alphabetical does not appear enforced; place near other monitoring-related keys if any, else append at end of properties dict before the closing brace at L110):

```python
        "homer_enabled": {
            "type": "boolean",
            "description": (
                "Enable Kamailio HEP capture sidecar (heplify-client). "
                "When True, the kamailio docker-compose includes the "
                "sipcapture/heplify sidecar that forwards SIP traffic to "
                "the heplify-server LoadBalancer. Default True. Disable "
                "to skip HOMER capture entirely."
            ),
        },
```

**Default behavior:** jsonschema does NOT auto-inject defaults from the schema. The True default lives in `config.get("homer_enabled", True)` (both in the Python preflight at §5.3 and in ansible_runner at §5.4(b)). The schema entry exists ONLY to allow the key under `additionalProperties: False`. Iter-1 hint about "PR-D2/PR-E bool-key precedent" was wrong (verified: no existing boolean keys in this schema); the design declares the entry from scratch.

### 5.6 Wire-field checklist (heplify-client command args)

| Arg | Value | Production parity |
|---|---|---|
| `-i` | `any` | ✓ |
| `-hs` | `${HOMER_URI}` (== `<heplify_lb_ip>:9060`) | ✓ |
| `-m` | `SIP` | ✓ |
| `-dim` | `REGISTER` | ✓ |

Source: production `voip-kamailio-docker/docker-compose.yml:55-63`. All four args match exactly.

### 5.7 Producer→consumer trace table

| Producer change | Consumer file | Consumer read path | Verification |
|---|---|---|---|
| `homer_uri` Jinja-gated in group_vars | `env.j2:39` (`HOMER_URI={{ homer_uri }}`) | Jinja render at ansible_run | `grep -n homer_uri ansible/` returns 4 hits (def + env.j2 + 2 tests post-PR) |
| `homer_enabled` in group_vars (literal `"true"` baseline; overridden by ansible extra-var injected from `config["homer_enabled"]`) | `docker-compose.yml.j2` `{% if homer_enabled | bool ... %}` gate; ALSO consumed by `env.j2:40` (`HOMER_ENABLED={{ homer_enabled }}`) | Jinja render | After PR, `grep -n homer_enabled ansible/` returns def + compose-gate + env.j2 |
| heplify-client service block in compose template | docker-compose runtime on the Kamailio VM | rendered compose file | `docker compose config` on the VM shows or omits the block per gate |
| `heplify_lb_ip` flat-var (PR-U-1) | env.j2 indirectly via homer_uri; compose-level gate directly | Jinja eval | unchanged in this PR |
| `homer_enabled` flat-var (new, NO `_flag` suffix — matches PR-U-1 pattern) | group_vars `homer_enabled` line via extra-var precedence | Jinja eval | new test asserts flat-var emission |
| `check_kamailio_homer_uri_present` in preflight.py | `ansible_runner.py:ansible_run` invokes | hard-fail before playbook | 4 unit tests cover the matrix |
| `config["homer_enabled"]: bool = True` schema | Python preflight + ansible_runner flat-var | config.get | schema test asserts default |

No dead defaults. Every producer change has a consumer read path; both the compose-level gate and the env.j2 wiring read `homer_enabled`, both reading the same flat-var sourced from the same config key.

## 6. Copy/decision rationale

- **Sidecar pattern:** locked decision (Option A, 2026-05-13 CPO consultation).
- **`network_mode: host`:** required for `-i any` to see SIP packets on host interfaces.
- **Pin image to `1.56`:** matches production.
- **`homer_enabled` as a real Python config key:** iter-1 finding. Required so the Python preflight and the Ansible gate share one source of truth. Operator overrides via `config.yaml`, not via `--extra-vars` (latter doesn't reach the Python preflight).
- **Compose-level Jinja gate (`{% if homer_enabled and heplify_lb_ip %}`):** primary protection against the literal `:9060` failure mode. Defense-in-depth via group_vars Jinja conditional.
- **Default `homer_enabled: true`:** dogfood prioritizes works-out-of-box.
- **No profile gate (no `deploy_kamailio` key):** iter-1 finding. Install profile is selected by inventory, not config.

## 7. Verification plan

### 7.1 Static checks (pre-commit)

1. `python -m pytest tests/ -q` — full suite green (expect 754 + 17 new = ~771).
2. `bash scripts/dev/check-plan-sensitive.sh docs/plans/2026-05-13-pr-u-3-kamailio-heplify-sidecar-design.md` — sensitive scan PASS.
3. `ansible-playbook --syntax-check ansible/playbooks/kamailio.yml` (if Ansible CLI available in CI; else skip).
4. `grep -rn 'heplify-client\|sipcapture/heplify' ansible/` — 1 hit in docker-compose.yml.j2.
5. `grep -n 'homer_uri\|homer_enabled' ansible/inventory/group_vars/kamailio.yml` — 2 hits with new values.

### 7.2 Test enumeration (new file `tests/test_pr_u_3_kamailio_heplify_sidecar.py`)

| Class | Tests | Purpose |
|---|---|---|
| `TestDockerComposeShape` | 5 | sidecar block present with: `image: sipcapture/heplify:1.56`, `network_mode: host`, `restart: unless-stopped`, `depends_on:\n      - kamailio`, command argv list `-i any -hs ${HOMER_URI} -m SIP -dim REGISTER` |
| `TestComposeJinjaGate` | 1 | `{% if homer_enabled \| bool and heplify_lb_ip %}` wraps the sidecar block; `{% endif %}` closes correctly |
| `TestEnvJ2Wiring` | 2 | `HOMER_URI={{ homer_uri }}` regression guard; `HOMER_ENABLED={{ homer_enabled }}` regression guard |
| `TestGroupVarsDefaults` | 2 | `homer_uri: "{% if heplify_lb_ip %}{{ heplify_lb_ip }}:9060{% endif %}"` exact; `homer_enabled: "true"` literal (no flag suffix) |
| `TestPreflightGate` | 3 | empty IP + homer_enabled=true → raises; non-empty IP + homer_enabled=true → passes; empty IP + homer_enabled=false → passes |
| `TestPreflightRegistration` | 1 | `ansible_runner.ansible_run` imports + invokes `check_kamailio_homer_uri_present`. **Assertion mechanism**: source-grep on `scripts/ansible_runner.py` text for both `from scripts.preflight import` (with the function name) AND the `check_kamailio_homer_uri_present(terraform_outputs, config)` call. Same pattern as PR-U-2 `TestPreflightRegistration` |
| `TestAnsibleFlatVarWiring` | 2 | `heplify_lb_ip` flat-var emitted (PR-U-1 regression guard); `homer_enabled` flat-var emitted (new, direct key, no `_flag` suffix) |
| `TestConfigSchemaDefault` | 1 | `homer_enabled` config default is `True` (boolean) |

Total: 17 new tests.

### 7.3 Mutant-injection harness

15 mutants, file-backup revert. Gate ≥12/15.

| # | Mutation | Expected catcher |
|---|---|---|
| 1 | rename `heplify-client:` → `heplify-clientX:` | TestDockerComposeShape |
| 2 | bump `sipcapture/heplify:1.56` → `1.57` | TestDockerComposeShape |
| 3 | remove `network_mode: host` | TestDockerComposeShape |
| 4 | drop `depends_on: kamailio` | TestDockerComposeShape |
| 5 | replace `-i any` with `-i eth0` | TestDockerComposeShape |
| 6 | swap `-m SIP` → `-m RTCP` | TestDockerComposeShape |
| 7 | drop `-dim REGISTER` | TestDockerComposeShape |
| 8 | change group_vars `homer_uri` to `"127.0.0.1:9060"` | TestGroupVarsDefaults |
| 9 | flip group_vars `homer_enabled` literal from `"true"` to `"false"` | TestGroupVarsDefaults |
| 10 | invert preflight raise → swallow | TestPreflightGate |
| 11 | remove homer_enabled gate (always raise) | TestPreflightGate |
| 12 | drop preflight invocation from ansible_run | TestPreflightRegistration |
| 13 | drop `homer_enabled` flat-var emission | TestAnsibleFlatVarWiring |
| 14 | remove Jinja gate `{% if homer_enabled ... %}` (sidecar always rendered) | TestComposeJinjaGate |
| 15 | drop `restart: unless-stopped` | TestDockerComposeShape |

Acceptance: ≥12 caught.

### 7.4 Dogfood-readiness check (post-merge)

1. `voipbin-install apply` completes; preflight passes.
2. `docker ps` on Kamailio VM shows `kamailio-heplify` Up.
3. `docker logs kamailio-heplify --tail 50` non-empty, no panic.
4. Test SIP call → heplify-server logs show row insertions in `homer_data`.
5. homer-app UI lists the call in capture search.

## 8. Rollout / risk

| # | Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|---|
| R1 | heplify_lb_ip is pending on first apply | Med | preflight raises explicit error | Operator re-runs reconcile_k8s_outputs; error message specifies the stage |
| R2 | `network_mode: host` conflicts with other host-port consumer | Low | sidecar fails to start | Sniffer binds no port; no conflict |
| R3 | sipcapture/heplify Docker image becomes unavailable | Low | docker-compose pull fails | Stable in 2026; mirror to GCR if needed (future PR) |
| R4 | Sidecar high CPU at scale | Low (dogfood) | VM CPU spike | Production runs same image at scale |
| R5 | Operator sets `homer_enabled: false` in config.yaml | Expected | compose-level Jinja gate omits sidecar entirely; preflight no-ops | Documented operator path; no malformed args |
| R6 | Empty `heplify_lb_ip` with `homer_enabled=true` | Med | preflight raises BEFORE ansible_run starts | Compose-level + group_vars gates are defense-in-depth |
| R7 | Production heplify-client mounts a config file the install repo doesn't replicate | Low | sidecar uses defaults | Production passes config exclusively via command args; verified at production compose.yml |
| R8 | gcplogs vs json-file split | Low | minor ops UX | Production parity; documented |
| R9 | Existing `config.yaml` files don't have `homer_enabled` key | Med | `config.get("homer_enabled", True)` returns True default at every call site | jsonschema does NOT auto-inject defaults — the True fallback lives in the Python `.get()` calls at §5.3 and §5.4(b). Schema entry only allows the key under `additionalProperties: False`; missing-key case is handled in code |

## 9. Open questions (for iter-3 reviewer, if any)

All iter-2 blockers (B1 schema additionalProperties:False, B2 `_flag` indirection drop, B3 registration test mechanism, B4 depends_on short-form, B5 group_vars line positions) resolved in v3. No remaining open questions.

## 10. Approval status

- [x] Draft v1 written 2026-05-13
- [x] Iter-1 design review completed (7 REAL findings)
- [x] v2 fixes applied 2026-05-13
- [x] Iter-2 design review completed (5 REAL findings: 2 BLOCKER + 3 MINOR)
- [x] v3 fixes applied 2026-05-13
- [ ] APPROVED (pending min-2 satisfied; ready for implementation)

---

## 11. Iter-1 review response summary (v1 → v2)

| Iter-1 issue | Resolved in v2 | Section |
|---|---|---|
| 1. `homer_enabled` not a Python config key — preflight override path broken | Added `homer_enabled: bool = True` config schema key; preflight reads `config.get("homer_enabled", True)`; flat-var injected to Ansible via `homer_enabled_flag` | §2 G5/G6, §5.3, §5.4, §5.5, §4 (file table +1 row) |
| 2. `deploy_kamailio` key does not exist | Dropped the profile gate from preflight entirely; design §3 Non-goals adds explicit "no profile gate" line | §3, §5.3 (gate removed), §7.2 (TestPreflightGate dropped from 4 to 3 cases) |
| 3. R5 fact wrong — `:9060` literal failure | Added compose-level Jinja gate `{% if homer_enabled and heplify_lb_ip %}` (primary) + group_vars Jinja conditional on `homer_uri` (defense-in-depth). Sidecar is now omitted, not malformed | §5.1, §5.2, §8 R5 (rewritten), §8 R6 |
| 4. G2 unreachable | G2 rewritten to describe the actual gate hierarchy | §2 G2 |
| 5. Sidecar runs unconditionally | Compose-level Jinja gate added | §5.1 |
| 6. Mutant #6 catcher clarification | Catcher entries reviewed; #6 caught by TestDockerComposeShape (literal SIP regex) | §7.3 |
| 7. §4 file count vs row count mismatch | §4 counts updated (5 code files, not 4); estimated LOC bumped to ~150 from ~120 | §4 |

All 7 iter-1 findings addressed. No silent rejections.

## 12. Iter-2 review response summary (v2 → v3)

| Iter-2 issue | Resolved in v3 | Section |
|---|---|---|
| B1. `additionalProperties: False` + no bool precedent in CONFIG_SCHEMA — design said "verify at impl"; iter-2 confirmed schema rejects undeclared keys | Added explicit `"homer_enabled": {"type": "boolean", ...}` block in §5.5 with insertion site (`config/schema.py:L110` before closing brace). Also corrected R9 wording: default lives in `.get()` not in schema | §5.5 (rewritten), §8 R9 (rewritten) |
| B2. `homer_enabled_flag` indirection unnecessary, diverges from PR-U-1 | Dropped `_flag` suffix. Direct `ansible_vars["homer_enabled"]` injection matching PR-U-1's `heplify_lb_ip` pattern. group_vars `homer_enabled: "true"` is a literal baseline that extra-vars override | §5.2, §5.4(b), §5.7 trace table, §7.2 TestGroupVarsDefaults + TestAnsibleFlatVarWiring rows, §7.3 mutants #9 #13 |
| B3. TestPreflightRegistration mechanism unspecified | Added explicit assertion mechanism note: source-grep on `scripts/ansible_runner.py` text for import + invocation. Same pattern as PR-U-2 | §7.2 TestPreflightRegistration row |
| B4. depends_on short-form vs long-form drift | §5.1 Notes added explicit rationale: short-form is intentional because Kamailio has no healthcheck, long-form `service_healthy` would block indefinitely | §5.1 Notes |
| B5. §5.2 misleading adjacency | Clarified group_vars lines are at L27 and L41 (NOT adjacent) so impl preserves positions | §5.2 |

All 5 iter-2 findings addressed (2 BLOCKER + 3 MINOR). No silent rejections.

---

## Decisions locked 2026-05-13

- Sidecar pattern (Option A).
- `sipcapture/heplify:1.56`.
- `homer_enabled: true` default in Python config schema.
- `-i any -hs ${HOMER_URI} -m SIP -dim REGISTER`.
- Compose-level Jinja gate + group_vars defense-in-depth.
- Preflight at ansible_run time.
- No profile gate (no `deploy_kamailio` key).
