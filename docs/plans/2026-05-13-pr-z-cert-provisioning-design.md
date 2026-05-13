# PR-Z: Cert Provisioning Subsystem, Design v3

**Status**: v3 (revised after D3 revision-check + D4 feasibility-check)
**Date**: 2026-05-13
**Author**: Hermes CPO
**Parent**: v7 roadmap (`~/agent-hermes/notes/2026-05-13-install-redesign-v7-roadmap.md`)
**Predecessor**: v6 retrospective (Phase 4b freeze, TLS cert blocking ansible_run)
**Branch target**: `NOJIRA-PR-Z-cert-provisioning`

## §0a. v3 changes from v2 (D3 + D4 resolution)

v2 was APPROVED by D4 (feasibility) with 6 nits and REJECTED by D3 (revision-check) with 4 new MAJOR findings. v3 changes:

| Source | Finding | Resolution in v3 |
|---|---|---|
| D3 N-MAJ-1 | `tls_bootstrap.py` purity contract broken if it ingests state | **§6.0 split**: `tls_bootstrap.py` only gains pure crypto primitives (`_generate_ca()`, `_issue_leaf_signed_by_ca()`, `KAMAILIO_PAIRS`). All state-aware orchestration (load existing CA from secrets, decide reissue, write staging) moves to a NEW module `scripts/cert_lifecycle.py`. Pipeline stage calls `cert_lifecycle.seed_kamailio_certs`, not `tls_bootstrap`. |
| D3 N-MAJ-2 | Half-state policy for the 6 KAMAILIO_* keys unspecified | **§6.5 (new)**: Half-state policy — if any required key is missing for the chosen mode, treat ALL keys for that mode as missing and reissue from scratch. State.yaml.cert_state inconsistent with secrets.yaml ⇒ secrets.yaml is source of truth. Explicit `_audit_secret_completeness()` test target. |
| D3 N-MAJ-3 | §6.4 shred-overwrite is security theater on COW/SSD | **§6.4 rewritten**: honest reassessment. No shred attempt. We rely on (a) staging dir mode 0700, (b) `os.remove` + `os.rmdir`, (c) staging dir is per-workdir and ephemeral. Documented limitation: "On ext4/xfs/COW filesystems, removed file contents may persist in unallocated blocks until overwritten. This is acceptable for v7 since the workdir is operator-controlled and the same secrets exist sops-encrypted in secrets.yaml." |
| D3 N-MAJ-4 | synchronize `delete: yes` with dest = `certs_deploy_path` is dangerous if dest is misconfigured | **§6.3 hardened**: pre-flight assertion task (`assert certs_deploy_path is_absolute and starts with /opt/kamailio-docker/`). dest path-guard prevents accidental `rm -rf` of unrelated directories. |
| D4 F1 | `ansible/requirements.yml` missing; `ansible.posix.synchronize` unavailable | **§7 (added)**: create `ansible/requirements.yml` with `ansible.posix` collection. Pipeline `ansible_run` stage gains `ansible-galaxy collection install -r requirements.yml --force` preamble. |
| D4 F3 | Short-circuit logic doesn't verify staging on disk | **§6.2 hardened**: short-circuit gated on BOTH (a) `leaf_not_after - now > 30d` AND `san_list` unchanged AND (b) every required secrets.yaml key present AND decodable. Staging dir presence does NOT gate the short-circuit (staging is rebuilt every run from secrets). |
| D4 F2 | STAGE_LABELS table missing `cert_provision` entry | **§7**: explicit entry in pipeline.py STAGE_LABELS dict. |
| D4 nit-1..3 | Minor doc clarifications | folded into §6 / §7 prose |

## §0b. Outstanding nits accepted into PR body

These do NOT block design close; they become PR-body checklist items so reviewers can confirm them on code:

- D4 nit-4: `cleanup_cert_staging` must be wrapped in try/except so a cleanup error doesn't fail the apply post-hoc.
- D4 nit-5: log line on stage entry must redact base64 lengths only, not content.
- D4 nit-6: state.yaml writes must be atomic (temp + rename).

## §0. v2 changes from v1 (resolution of D1+D2 findings, preserved for trail)

v1 was rejected by both D1 (architecture/scope) and D2 (security/cost) reviewers. v2 changes:

| Source | Finding | Resolution |
|---|---|---|
| D1 C1 | SAN list unverified | **§3.1**: pinned by reading `voipbin/voip-kamailio:latest` image's `/templates/tls.cfg` directly. Authoritative SAN list = exactly `sip.<domain>` + `registrar.<domain>`. `DOMAIN_NAME_TRUNK` is dispatcher-routing only, not a TLS server_name. SAN drift test added. |
| D1 C2 | ACME UX silent fallback | **§4.2 + §6.1**: `cert_state` schema now splits `config_mode` vs `actual_mode`, adds `acme_pending` flag. `verify` and `apply` final banner loudly remind operator. |
| D1 M1 | Coexistence with `scripts/tls_bootstrap.py` | **STRATEGIC PIVOT**: v2 EXTENDS `tls_bootstrap.py` instead of creating a parallel subsystem. RSA-2048 + sops + base64 patterns reused. New SAN pair generators live alongside SSL_PAIRS. ~200 LOC instead of v1's ~400. |
| D1 M3 | Stale-SAN cleanup unspecified | **§6.3**: synchronize uses `delete: yes` scoped to `.cert-staging/`; obsolete SAN dirs purged. Test added. |
| D1 M4 | verify preflight scope vs PR-AA | **§2 out-of-scope**: preflight moved to PR-AA. PR-Z does NOT add preflight. |
| D1 M5 | Open questions Q1/Q2/Q5 must close at D1 | **§10**: Q1 closed (CA tied to workdir, GCP project change leaves CA), Q2 closed (destroy purges by default; `--keep-certs` flag), Q5 closed (YES, handler notifies). |
| D1 M6 | Missing files in §7 | **§7**: added `.gitignore` patch, `commands/cert.py`, `tests/fixtures/certs/`, `docs/operator/cert.md`, removal of `scripts/cert.py` (logic merges into `tls_bootstrap.py`). |
| D1 M7 | Crypto strength inconsistency | **§5 D7 (revised)**: RSA-2048 (matches existing `tls_bootstrap.py`). Justified: Kamailio's TLS handshake performance and operator client compat. CA validity bumped to 10y (was 1y), leaf 365d. |
| D1 M8 | Wildcard / internal-PKI personas missing | **§4 P3 (expanded)**: wildcard handling via per-SAN symlink. Internal-PKI uses `cert_manual_dir` + optional `internal_ca_bundle_pem`. Air-gapped: zero changes needed (cryptography lib is local). |
| D1 M9 | secret_schema.py interaction | **§7 (new)**: `scripts/secret_schema.py` adds `KAMAILIO_CERT_*` keys (mirroring SSL_CERT_API_BASE64 pattern). No `additionalProperties: false` breakage. |
| D1 M10 | k8s CA-trust ordering | **§2 out-of-scope (callout)**: v7 has no k8s service that needs to trust Kamailio's TLS. Forward-coupling for PR-AD logged. |
| D1 m1 | `state.yaml` privkey path semantics | N/A in v2 — privkey is in secrets.yaml (sops-encrypted by existing pipeline). state.yaml has only fingerprints. |
| D1 m3 | Mutant target 13/15 | **§7 (revised)**: target ≥14/15. |
| D1 m4 | §14 ellipsis paths | **§14**: fully-qualified paths in all gcloud examples. |
| D1 m5 | TestPipelineCertProvisionStage atomic-write case | **§7 (revised)**: added \"cert_provision raises mid-write ⇒ secrets.yaml NOT mutated\". |
| D2 H1 | `.sops.yaml` regex doesn't match | **§7**: `.sops.yaml` change NOT NEEDED. Certs land in secrets.yaml (existing sops scope). No new sops file. |
| D2 H2 | `.cert-staging/` not in `.gitignore` | **§7**: `.gitignore` updated. v2 staging dir is `.cert-staging/` only for plaintext leaf material that ansible_run consumes, ALWAYS cleaned up post-run. |
| D2 H3 | Plaintext leaf privkey lifecycle | **§6.4**: explicit cleanup hook after ansible_run succeeds. Risk row added §9. |
| D2 H4 | CA key compromise via workdir + ADC | **§9**: documented. No defense-in-depth in v7. Mitigation: per-install CA rotation on operator handoff (docs/operator/cert.md). |
| D2 M1 | Field name inconsistency | N/A in v2 — schema redesigned around secrets.yaml. |
| D2 M2 | RSA-4096 keygen latency | **§8**: RSA-2048 chosen; keygen ~50-100ms per leaf. |
| D2 M3 | CA validity > leaf + renewal window | **§5 D8 (revised)**: CA validity 10 years (3650d). Leaf 365d. Renewal threshold 30d. `ca_not_after > leaf_not_after + renewal_threshold` invariant test added. |
| D2 M4 | D10 SAN-leakage claim wrong | **§5 D10**: rationale corrected. Per-SAN chosen for role-layout compat, NOT security. |
| D2 M5 | Multi-replica notify | **§10 Q5 (closed)**: documented limitation. v7 = 1 replica only. PR-AC adds rolling restart strategy. |
| D2 M6 | Self-signed CA blocks WSS | **§9 + §13**: explicit risk row + operator doc. `apply` banner prints CA cert path + browser-trust install command. |
| D2 L1 | _load_ca_key_sops cache | N/A in v2 — sops decryption happens once per stage run via existing secretmgr.py. |
| D2 L2 | $30/mo cost miscalculation | **§8**: corrected to <$1/mo. |
| D2 L3 | File modes explicit | **§6**: `0700` parent dir, `0600` privkey, `0644` cert. Test added. |
| D2 L4 | Early-exit on valid certs | **§6.2**: `cert_provision` short-circuits if `leaf_not_after - now > 30d` AND SAN set unchanged. |

## §1. Goal

Make a fresh `voipbin-install apply --auto-approve` succeed end-to-end against the dogfood project (fake domain `dev.voipbin-install.example.com`) by automatically provisioning the TLS certificate files that Kamailio's `tls.so` module loads from `/certs/<SAN>/{fullchain,privkey}.pem`. Provide a forward-compatible path for real-domain operators (ACME, future PR-AC) and BYO-cert operators (manual, in PR-Z).

In one sentence: **install repo extends `scripts/tls_bootstrap.py` to also generate Kamailio per-SAN self-signed certificates, persisted in `secrets.yaml` (sops-encrypted), with the existing pipeline writing them to the VM during `ansible_run`**.

## §2. Scope

### In scope

- Extend `scripts/tls_bootstrap.py`: add `KAMAILIO_PAIRS` generator alongside existing `SSL_PAIRS`
- Self-signed CA generation (one per install), persisted in `secrets.yaml` under `KAMAILIO_CA_CERT_BASE64` + `KAMAILIO_CA_KEY_BASE64` (sops-encrypted by existing scope)
- Per-SAN leaf certs (`sip.<domain>` + `registrar.<domain>`) signed by the CA, persisted under `KAMAILIO_CERT_SIP_BASE64`, `KAMAILIO_PRIVKEY_SIP_BASE64`, `KAMAILIO_CERT_REGISTRAR_BASE64`, `KAMAILIO_PRIVKEY_REGISTRAR_BASE64`
- New pipeline stage `cert_provision` between `reconcile_k8s_outputs` and `ansible_run`
- New `voipbin-install cert {status, renew}` CLI subcommands (no `issue` subcommand in PR-Z; init already triggers issuance)
- `config/schema.py` field `cert_mode: enum[self_signed, manual]` with `self_signed` default (acme deferred to PR-AC)
- `config/schema.py` field `cert_manual_dir: str?` (required only if `cert_mode == manual`)
- Wizard prompt during `init` (default `self_signed`)
- Ansible role `kamailio` consumes the cert files from `<workdir>/.cert-staging/` and deploys to VM `<certs_deploy_path>/`
- `.cert-staging/` always cleaned up after `ansible_run` succeeds

### Out of scope (deferred to future PRs)

- Full ACME implementation (PR-AC): schema admits `cert_mode: acme` reserved value, validation rejects it with \"acme mode not yet supported; ship PR-AC\"
- Cert preflight check in `verify` (PR-AA)
- Cert rotation automation / cron / cert-manager (v8 candidate)
- TLS for non-Kamailio surfaces (rtpengine, admin SPA, talk/meet) (PR-AD)
- Internal-PKI bundle distribution to clients (PR-AD)
- k8s services trusting Kamailio's CA (no v7 service needs this)

### Non-goals

- Hardware Security Module / TPM integration
- TLS pinning / DANE / Certificate Transparency
- Multi-region cert distribution (single-region GCP only in v7)
- Operator-supplied private CA (use `cert_mode: manual` with externally-signed certs instead)

## §3. Background

### §3.1 SAN list pinned by image inspection (D1 C1 resolution)

Read directly from `voipbin/voip-kamailio:latest` /templates/tls.cfg on the dogfood VM:

```
[server:default]
server_name = *.registrar.BASE_DOMAIN
certificate = /certs/registrar.BASE_DOMAIN/fullchain.pem
private_key = /certs/registrar.BASE_DOMAIN/privkey.pem

[client:default]
server_name = sip.BASE_DOMAIN
certificate = /certs/sip.BASE_DOMAIN/fullchain.pem
private_key = /certs/sip.BASE_DOMAIN/privkey.pem

[server:any]
server_name = sip.BASE_DOMAIN
certificate = /certs/sip.BASE_DOMAIN/fullchain.pem
private_key = /certs/sip.BASE_DOMAIN/privkey.pem

[server:any]
server_name = *.registrar.BASE_DOMAIN
certificate = /certs/registrar.BASE_DOMAIN/fullchain.pem
private_key = /certs/registrar.BASE_DOMAIN/privkey.pem
```

Authoritative SAN list = **exactly two**:
- `sip.<domain>` (CN = `sip.<domain>`, SAN DNS = `sip.<domain>`)
- `registrar.<domain>` (CN = `registrar.<domain>`, SAN DNS = `registrar.<domain>` AND wildcard `*.registrar.<domain>` because the server_name uses the wildcard form)

`DOMAIN_NAME_TRUNK=trunk.<domain>` exists in `env.j2:50` but is consumed only by dispatcher routing logic, NOT by `tls_domain` server_name. NO TLS cert needed for `trunk.<domain>`.

A test `TestSanListIsExactlyTwo` will assert PR-Z's KAMAILIO_PAIRS contains exactly these two and no more.

### §3.2 Current code state references

| File | Line | Current behavior |
|---|---|---|
| `scripts/tls_bootstrap.py` | 41-44 | `SSL_PAIRS` constant for api/hook self-signed certs (existing) |
| `scripts/tls_bootstrap.py` | 59-91 | `_generate_self_signed_pair(hostname)` — RSA-2048 leaf, self-issued |
| `scripts/tls_bootstrap.py` | 103-143 | `seed_secrets_yaml()` mutates secrets dict in place |
| `ansible/roles/kamailio/templates/docker-compose.yml.j2` | 17 | `${CERTS_PATH:-./certs}:/certs:ro` mount |
| `ansible/roles/kamailio/templates/env.j2` | 9 | `CERTS_PATH={{ certs_deploy_path }}` |
| `ansible/inventory/group_vars/kamailio.yml` | 16 | `certs_deploy_path: "{{ kamailio_deploy_dir }}/certs"` |
| `ansible/roles/kamailio/tasks/main.yml` | 27-36 | creates `<certs_deploy_path>/sip.<domain>/` and `/registrar.<domain>/` directories, mode 0755 |
| `scripts/pipeline.py` APPLY_STAGES | 38-46 | no cert stage |
| `scripts/cli.py` | — | no `cert` subcommand |
| `scripts/secret_schema.py` | — | tracks SSL_*_BASE64 etc. with `additionalProperties: false` |
| `secrets.yaml` (operator workdir) | — | sops-encrypted, holds `JWT_KEY`, `SSL_*_BASE64`, etc. |
| `.sops.yaml` | — | `creation_rules: - path_regex: secrets\\.yaml$` (existing scope) |

## §4. Three operator personas (clarified)

### P1 — Dev / Local (dogfood, demo, fake domain)

**Trigger**: any first-time install. Default behavior.

**Behavior**: `init` wizard sets `cert_mode: self_signed` by default. `cert_provision` stage auto-generates per-install CA + per-SAN leaf certs into secrets.yaml. ansible_run deploys to VM. Browser/SIP-client trust requires operator to install the CA cert (printed at end of apply).

**Default**: `cert_mode: self_signed`.

### P2 — Self-host with real domain (deferred to PR-AC)

**Trigger**: operator chooses `cert_mode: acme` in v7+. v7's `cert_mode: acme` is REJECTED at config validation with: \"acme cert_mode requires PR-AC; for now use self_signed (browsers will warn but Kamailio works) or manual (BYO cert).\"

### P3 — Enterprise / BYO cert (in PR-Z)

**Trigger**: `cert_mode: manual` in config.yaml.

**Behavior**: operator supplies `cert_manual_dir` containing per-SAN subdirectories:

```
<cert_manual_dir>/
├── sip.<domain>/
│   ├── fullchain.pem
│   └── privkey.pem
└── registrar.<domain>/
    ├── fullchain.pem
    └── privkey.pem
```

`cert_provision` validates layout + PEM format + expiry. Encodes into the same `KAMAILIO_*_BASE64` secrets.yaml keys (no CA cert recorded — operator's CA is external). ansible_run deploys normally.

Wildcard cert: operator provides one PEM pair and the wizard offers \"use this for both SANs?\" — symlinks both per-SAN dirs to the same source.

Internal-PKI / private CA: same as manual mode. Validation does NOT enforce public-CA trust path (Kamailio with `verify_certificate = no` does not need it for v7).

Air-gapped: works unchanged (cryptography lib is local).

## §5. Decision matrix (revised)

| # | Decision | Choice (v2) | Rationale (v2) |
|---|---|---|---|
| D1 | Default mode for any domain | self-signed | dogfood-friendly; operator can override |
| D2 | Cert storage primary | secrets.yaml (sops-encrypted) | reuses existing infrastructure (sops + KMS); operator backup/migrate works for free |
| D3 | Self-signed CA scope | per-install (one CA → multi-SAN leaves) | enables single browser-trust import; matches PR-AD direction |
| D4 | CA persistence | secrets.yaml under `KAMAILIO_CA_KEY_BASE64` | sops-encrypted; backup/migrate aligned with secret pipeline |
| D5 | ACME provider | reject at config schema; defer to PR-AC | ACME complexity isolated; PR-Z ships in days |
| D6 | When does cert_provision stage run | between reconcile_k8s_outputs and ansible_run | certs needed only by Kamailio role; minimal dependency surface |
| D7 | RSA key size | RSA-2048 (matches existing `tls_bootstrap.py`) | uniform with existing self-signed system; Kamailio TLS handshake faster; operator clients widely compatible |
| D8 | Validity periods | CA = 3650 days (10y), leaf = 365 days | CA outlives leaf+renewal window (NIST/CA-B-Forum aligned for private trust anchors); leaf renewal cycle matches Let's Encrypt 90d ballpark but doubled for dogfood lower friction |
| D9 | CA private key encryption at rest | sops-encrypted (existing pipeline) | catastrophic leak mitigation; uniform with rest of secrets.yaml |
| D10 | Leaf cert structure | one cert per SAN (separate fullchain.pem per dir) | matches existing role layout; SNI-driven Kamailio works correctly; NOT a security improvement |
| D11 | Cert renewal threshold | 30 days remaining | operator response time; aligned with Let's Encrypt |
| D12 | Cert renewal automation | manual `voipbin-install cert renew` in v7 | self-signed 365d default reduces pressure; auto-rotate is v8 |
| D13 | Storage staging dir | `<workdir>/.cert-staging/` (cleaned post-ansible_run) | minimal plaintext-on-disk window; gitignored |
| D14 | `cert_state` in state.yaml | metadata only (mode, expiry, fingerprints, SAN list) — NO key material | state.yaml is plain (not sops); key material lives in secrets.yaml |

## §6. Architecture

### §6.0 Module split (D3 N-MAJ-1 resolution)

`scripts/tls_bootstrap.py` (existing) is a **pure crypto module**: given inputs, produce PEM bytes. No I/O on secrets.yaml, no state.yaml, no staging dir. v3 adds three pure helpers there:

- `KAMAILIO_PAIRS: list[CertPairSpec]` — declarative SAN spec
- `_generate_ca(common_name, validity_days) -> (ca_cert_pem, ca_key_pem)`
- `_issue_leaf_signed_by_ca(san, ca_cert_pem, ca_key_pem, validity_days) -> (leaf_pem, leaf_key_pem)`

`scripts/cert_lifecycle.py` (**NEW**) is the **state-aware orchestrator**. It:

- Reads secrets dict + state.yaml.cert_state
- Decides reissue vs short-circuit (§6.2)
- Audits secret completeness across the 6 KAMAILIO_* keys (§6.5)
- Calls `tls_bootstrap` primitives when reissue is required
- Writes back to secrets dict and to state.yaml.cert_state
- Materializes `.cert-staging/` from secrets dict for ansible
- Owns post-success cleanup hook

`scripts/pipeline.py:_run_cert_provision` is a thin wrapper that loads secrets, calls `cert_lifecycle.seed_kamailio_certs(...)`, then persists secrets via the existing secretmgr path.

This split protects the existing `tls_bootstrap.py` invariants used by api/hook certs and isolates new state risk to one new file.

```
                                 config.yaml
                                  cert_mode: self_signed | manual
                                  cert_manual_dir: ?
                                  domain: <domain>
                                          │
                                          ▼
┌──────────────────────────────────────────────────────────────────────┐
│ Pipeline stages (PR-Z adds cert_provision, marked ★)                 │
│                                                                      │
│ terraform_init                                                       │
│ reconcile_imports                                                    │
│ terraform_apply                                                      │
│ reconcile_outputs                                                    │
│ k8s_apply                                                            │
│ reconcile_k8s_outputs                                                │
│ ★ cert_provision  ─────► scripts/tls_bootstrap.py:seed_kamailio_certs│
│      │                       │                                       │
│      │                       ├─ mode self_signed                     │
│      │                       │   • short-circuit if state.yaml       │
│      │                       │     cert_state.leaf_not_after - now   │
│      │                       │     > 30d AND san set unchanged       │
│      │                       │   • load_or_generate_ca(secrets)      │
│      │                       │   • for each SAN: issue_leaf_cert()   │
│      │                       │   • write KAMAILIO_*_BASE64 keys to   │
│      │                       │     secrets dict                      │
│      │                       │   • update state.yaml.cert_state      │
│      │                       │                                       │
│      │                       └─ mode manual                          │
│      │                           • validate cert_manual_dir layout   │
│      │                           • PEM parse + expiry check          │
│      │                           • base64 + write to secrets dict    │
│      │                           • state.yaml.cert_state marks       │
│      │                             actual_mode = manual              │
│      │                                                               │
│      │                       Either path:                            │
│      │                         • secretmgr.encrypt(secrets.yaml)     │
│      │                         • secretmgr.decrypt → write           │
│      │                           <workdir>/.cert-staging/<SAN>/      │
│      │                           {fullchain,privkey}.pem             │
│      │                                                               │
│      ▼                                                               │
│ ansible_run ─────► role kamailio reads workdir/.cert-staging/        │
│                    deploys to VM <certs_deploy_path>/<SAN>/          │
│                    synchronize delete=yes (purges stale SAN dirs)    │
│                    handler: Recreate kamailio containers             │
│                    POST-SUCCESS: shred .cert-staging/                │
└──────────────────────────────────────────────────────────────────────┘
```

### §6.1 secrets.yaml schema additions

```yaml
# Existing keys (preserved):
JWT_KEY: <hex>
SSL_CERT_API_BASE64: <b64-pem>
SSL_PRIVKEY_API_BASE64: <b64-pem>
SSL_CERT_HOOK_BASE64: <b64-pem>
SSL_PRIVKEY_HOOK_BASE64: <b64-pem>

# New keys (PR-Z):
KAMAILIO_CA_CERT_BASE64: <b64-pem-of-self-signed-ca>     # only when self_signed
KAMAILIO_CA_KEY_BASE64: <b64-pem-of-ca-privkey>          # only when self_signed
KAMAILIO_CERT_SIP_BASE64: <b64-pem>
KAMAILIO_PRIVKEY_SIP_BASE64: <b64-pem>
KAMAILIO_CERT_REGISTRAR_BASE64: <b64-pem>
KAMAILIO_PRIVKEY_REGISTRAR_BASE64: <b64-pem>
```

When `cert_mode == manual`, the `KAMAILIO_CA_*` pair is omitted (CA is external).

### §6.2 short-circuit (D4 F3 hardened)

`cert_lifecycle.seed_kamailio_certs` short-circuits ONLY when ALL conditions hold:

1. `state.yaml.cert_state.config_mode == config.cert_mode` (no mode flip)
2. `state.yaml.cert_state.san_list` equals computed SAN list from current domain
3. `state.yaml.cert_state.leaf_certs[<san>].not_after - now > 30 days` for **every** SAN
4. `_audit_secret_completeness(secrets, mode)` returns OK (every required key present and base64-decodable to a valid PEM)

If any of the four fails, the stage proceeds to reissue (full for missing-CA case, leaf-only for SAN-list change while CA valid).

Staging dir presence is NOT a short-circuit input. Staging is always re-materialized from secrets at stage end, so the operator deleting `.cert-staging/` manually causes no harm.

### §6.3 Ansible deploy task (D3 N-MAJ-4 path-guard)

```yaml
- name: Assert cert deploy path is safe
  assert:
    that:
      - certs_deploy_path is string
      - certs_deploy_path.startswith('/opt/kamailio-docker/')
      - certs_deploy_path | length > len('/opt/kamailio-docker/')
    fail_msg: "certs_deploy_path must be an absolute path under /opt/kamailio-docker/ to prevent rm -rf risk from synchronize delete=true."

- name: Sync cert-staging to VM
  ansible.posix.synchronize:
    src: "{{ playbook_dir }}/../.cert-staging/"
    dest: "{{ certs_deploy_path }}/"
    delete: true
    recursive: true
    rsync_opts:
      - "--chmod=D700,F600"
  notify: Recreate kamailio containers

- name: Ensure fullchain.pem world-readable inside container
  file:
    path: "{{ certs_deploy_path }}/{{ item }}/fullchain.pem"
    mode: '0644'
  loop:
    - "sip.{{ domain }}"
    - "registrar.{{ domain }}"
```

The `assert` task before `synchronize` prevents catastrophic `rm -rf` if `certs_deploy_path` is empty, root, or otherwise pathological.

### §6.4 Post-success cleanup (D3 N-MAJ-3 honest reassessment)

Earlier drafts proposed a `shred`-style overwrite. On modern filesystems (ext4 with journaling, xfs, COW filesystems on cloud disks) and SSD-backed disks, overwriting a file's bytes via the same FD does NOT reliably overwrite the physical sectors holding the prior contents. This was security theater.

v3 drops the overwrite and is honest about the threat model:

```python
def cleanup_cert_staging(workdir: Path) -> None:
    """Remove .cert-staging/ after ansible_run succeeds.

    Wrapped by caller in try/except — cleanup failure does NOT fail apply
    (D4 nit-4).
    """
    staging = workdir / ".cert-staging"
    if not staging.exists():
        return
    shutil.rmtree(staging)
```

Threat-model documentation in `docs/operator/cert.md`:

> The cert staging directory is mode 0700 and removed after each successful apply. On ext4/xfs/COW filesystems, removed file contents may persist in unallocated blocks until overwritten. This is acceptable for v7 because (a) the workdir is operator-controlled with the same threat model as `secrets.yaml`, (b) all key material is also present sops-encrypted in `secrets.yaml`, (c) we provide `voipbin-install cert clean-staging` for explicit cleanup. For hostile-host scenarios use full-disk encryption.

If `ansible_run` fails, staging is preserved for debugging. `voipbin-install cert clean-staging` removes it manually.

### §6.5 Half-state policy (D3 N-MAJ-2 resolution)

The 6 KAMAILIO_* secret keys (self_signed mode) or 4 (manual mode) can fall out of sync with `state.yaml.cert_state`. v3 resolves any inconsistency by **treating secrets.yaml as source of truth**:

`_audit_secret_completeness(secrets, mode) -> AuditResult`:

- self_signed mode requires all 6 keys: CA cert/key + sip cert/key + registrar cert/key
- manual mode requires 4 keys: sip cert/key + registrar cert/key
- each key must base64-decode to a valid PEM
- on ANY missing/malformed key, all keys for that mode are treated as missing and reissued from scratch (CA is regenerated; this changes the CA fingerprint and trips next browser-trust)

`state.yaml.cert_state` is treated as advisory metadata. If state says "self_signed valid until 2027" but secrets.yaml is missing the CA key, secrets.yaml wins and the stage reissues.

Tests `TestHalfStatePolicy`:
- missing CA cert → full reissue
- missing one leaf → full reissue (we don't try partial — simpler, safer)
- malformed base64 in any key → full reissue
- valid keys + stale state → state corrected on next write, no reissue (idempotent path)

### §6.6 state.yaml.cert_state schema (metadata-only)

```yaml
cert_state:
  schema_version: 1
  config_mode: self_signed | manual    # what operator chose in config.yaml
  actual_mode: self_signed | manual    # what last successful issuance produced
  acme_pending: false                  # reserved: PR-AC will use this
  ca_subject: "CN=VoIPBin Install CA dev,O=VoIPBin"  # absent when manual mode
  ca_not_after: "2036-05-13T00:00:00Z"               # absent when manual mode
  ca_fingerprint_sha256: "AB:CD:..."                 # absent when manual mode
  san_list: ["sip.dev.voipbin-install.example.com",
             "registrar.dev.voipbin-install.example.com"]
  leaf_certs:
    sip.dev.voipbin-install.example.com:
      not_after: "2027-05-13T00:00:00Z"
      fingerprint_sha256: "12:34:..."
      serial: 2
    registrar.dev.voipbin-install.example.com:
      not_after: "2027-05-13T00:00:00Z"
      fingerprint_sha256: "56:78:..."
      serial: 3
```

NO key material. All keys live in secrets.yaml (sops-encrypted). State writes use atomic temp+rename (D4 nit-6).

## §7. Files to add / change

### Added

- `tests/test_pr_z_*.py` — see §11 for breakdown
- `tests/fixtures/certs/manual_mode/sip.example.com/{fullchain,privkey}.pem` — manual-mode test fixtures
- `tests/fixtures/certs/manual_mode/registrar.example.com/{fullchain,privkey}.pem`
- `docs/operator/cert.md` — operator-facing doc: CA trust install, manual-mode layout, post-apply WSS warning, renewal flow
- `docs/plans/2026-05-13-pr-z-cert-provisioning-design.md` — this document
- `scripts/dev/pr_z_mutant_harness.py` — mutant injection script
- `scripts/commands/cert.py` — CLI command implementation (status, renew, clean-staging)
- `scripts/cert_lifecycle.py` — **(NEW, D3 N-MAJ-1)** state-aware orchestrator: secret audit, short-circuit, reissue decision, staging materialization, post-success cleanup hook. Imports pure primitives from `tls_bootstrap`.
- `ansible/requirements.yml` — **(NEW, D4 F1)** declares `ansible.posix` collection so `synchronize` module is available.

### Changed

- `scripts/tls_bootstrap.py` — extend with `KAMAILIO_PAIRS` constant, `_generate_ca()`, `_issue_leaf_signed_by_ca()`. **PURE crypto only.** Existing SSL_PAIRS / api / hook code unchanged. NO state.yaml or secrets-orchestration logic added here.
- `scripts/cli.py` — register `cert` subcommand group
- `scripts/pipeline.py` — insert `cert_provision` stage between `reconcile_k8s_outputs` and `ansible_run`; add `_run_cert_provision` thin runner (delegates to `cert_lifecycle.seed_kamailio_certs`); add post-success cleanup hook; **add `STAGE_LABELS["cert_provision"]` entry (D4 F2)**; **`ansible_run` runner gains `ansible-galaxy collection install -r ansible/requirements.yml` preamble (D4 F1)**.
- `scripts/preflight.py` — no change (preflight cert check is PR-AA)
- `scripts/wizard.py` — prompt for `cert_mode` after domain prompt
- `scripts/secret_schema.py` — add `KAMAILIO_*_BASE64` keys with `additionalProperties: false` compatibility
- `config/schema.py` — add `cert_mode`, `cert_manual_dir` fields with strict validation
- `ansible/roles/kamailio/tasks/main.yml` — replace "Create certificate directories" task with: (1) `assert certs_deploy_path is safe` path-guard task (D3 N-MAJ-4), (2) synchronize-based deploy task with `delete: true`, (3) explicit `file: mode: 0644` for `fullchain.pem`. Add handler `Recreate kamailio containers` if not present.
- `.gitignore` — add `.cert-staging/`

## §8. Cost & performance

- RSA-2048 keygen: ~50-100ms per leaf on a dev VM. Total `cert_provision` stage: ~300ms (CA gen if absent + 2 leaves).
- KMS API calls: 1 encrypt per apply (when secrets.yaml mutated), 1 decrypt per ansible_run (when reading certs). At ~$0.03/10k operations, even 100 applies/month = <$0.05/mo. v1's $30/mo estimate was a 600× error.
- Storage in secrets.yaml: ~12 KB additional (CA pair + 2 leaf pairs in base64). Negligible.
- Workdir `.cert-staging/`: cleaned post-success; momentary plaintext footprint.

## §9. Risks

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| sops + KMS key not yet available when `cert_provision` runs | Low | High | KMS keyring is created before terraform_init; by `cert_provision` (post-reconcile_k8s_outputs) it is guaranteed present. Preflight check at stage start verifies `.sops.yaml` resolves. |
| Kamailio's `tls.so` rejects our self-signed cert format | Low | High | tls.cfg inspection (§3.1) confirms PEM + RSA + SHA256 + X.509v3 are all accepted. Existing `tls_bootstrap.py` produces this exact format for API/Hook certs. |
| Manual-mode operator places certs at wrong subdirectory layout | Medium | Medium | `validate_manual_cert_dir` enumerates exact required paths with clear error message + remediation `tree` output |
| Idempotency violation: re-running cert_provision reissues unnecessarily | Medium | Low | short-circuit when `leaf_not_after - now > 30d` AND SAN set unchanged (§6.2) |
| secrets.yaml corruption loses CA, leaf certs no longer chain | Low | High | sops-encrypted; corruption detectable; operator can rotate via `cert renew --force` |
| operator manually deletes cert files on VM | Medium | Low | next `ansible_run` re-deploys from staging; PR-Z's synchronize task is unconditional |
| domain change after install | Medium | High | `cert_provision` detects SAN-list change against `state.yaml.cert_state.san_list`; re-issues leaf certs (CA unchanged) |
| **Workdir plaintext leaf privkey window** (D2 H3) | Medium | Medium | staging dir is `.gitignored`, mode 0700, cleaned post-success; documented in `docs/operator/cert.md` |
| **CA key compromise via workdir backup + ADC** (D2 H4) | Low | High | per-install CA (no cross-install blast radius); rotate on operator handoff; documented |
| **Browser blocks WSS to self-signed Kamailio** (D2 M6) | High | Medium | `apply` final banner prints CA cert export path and per-OS browser-trust install commands; `docs/operator/cert.md` § \"Trusting the install CA\" |
| ACME mode used in v7 | Low | Medium | config schema rejects with clear error pointing to PR-AC tracking |
| Multi-replica rolling restart needed | Low (v7 = 1 replica) | Low | documented limitation; PR-AC adds rolling restart strategy |

## §10. Open questions — CLOSED at D2

- **Q1 (CA on project change)**: CA is tied to install workdir, NOT GCP project. Operator moving to new project copies workdir → CA travels. No regeneration. **CLOSED.**
- **Q2 (destroy purge)**: `voipbin-install destroy` purges `.cert-staging/` and `state.yaml.cert_state` by default. New flag `--keep-certs` preserves them for reinstall. **CLOSED.**
- **Q3 (ACME DNS check)**: deferred to PR-AC.
- **Q4 (verify expiry threshold)**: deferred to PR-AA.
- **Q5 (notify on cert change)**: YES, ansible task notifies `Recreate kamailio containers` handler. Single-replica limitation documented. **CLOSED.**
- **Q6 (WSS browser trust)**: NOT safely deferrable. Apply banner + docs/operator/cert.md handle it. **CLOSED.**

## §11. Test surface

| Test class | Cases | Critical mutant coverage |
|---|---|---|
| `TestSanListIsExactlyTwo` | 2 | KAMAILIO_PAIRS extension catches drift; pin to sip + registrar |
| `TestSelfSignedCaGeneration` | 5 | RSA-2048, X.509v3, subject `CN=VoIPBin Install CA`, validity 10y, idempotent |
| `TestLeafCertIssuance` | 6 | signed by CA (chain verify), SANs correct, KU/EKU for serverAuth, validity 365d, serial unique, fingerprint stable across re-runs |
| `TestSeedKamailioCertsOrchestrator` | 8 | self_signed writes 6 keys to secrets, manual mode reads from cert_manual_dir, manual mode omits CA keys, idempotent on valid certs, regenerates when SAN unchanged + CA absent, regenerates leaves when SAN list changed, rejects mode=acme, rejects mode=bogus |
| `TestCertStateYaml` | 5 | schema_version pinned, no key material in state, leaf_certs map matches san_list exactly, config_mode != actual_mode allowed, CA fields absent when manual |
| `TestCertCliCommands` | 6 | `cert status` reports expiry per SAN, `cert status --json` machine-readable, `cert renew` no-op when >30d, `cert renew --force` re-issues, `cert clean-staging` removes dir, error messages have remediation hints |
| `TestPipelineCertProvisionStage` | 7 | stage inserted at correct position, runner returns False on cert.py error, state updated on success, dry-run skips writes, idempotent on re-run, mid-write failure leaves secrets.yaml unmutated, post-success cleanup hook runs |
| `TestConfigSchemaCertMode` | 5 | accepts self_signed, accepts manual, rejects acme with helpful message, rejects bogus mode, `cert_manual_dir` required only when manual |
| `TestSecretSchemaIntegration` | 3 | new KAMAILIO_* keys appear in secret_schema with `additionalProperties: false` compat, no SSL_*_BASE64 regression, manual mode does not require CA keys |
| `TestWizardCertModePrompt` | 4 | default self_signed, manual prompts for cert_manual_dir, manual validates dir layout, init writes cert_mode to config.yaml |
| `TestAnsibleKamailioCertDeploy` | 4 | synchronize task present, delete:yes scoped, handler notify configured, post-ansible_run cleanup hook fires |
| `TestStaleSanCleanup` | 3 | san_list shrink purges from staging, san_list shrink purges from VM (via synchronize delete), san_list grow adds without orphaning |
| `TestWildcardCertManualMode` | 2 | wildcard cert symlinked into both SAN dirs, validation accepts single SAN matching wildcard |
| `TestCaValidityInvariant` | 1 | CA `not_after > leaf_not_after + renewal_threshold` enforced (D2 M3) |
| `TestPostSuccessCleanup` | 3 | staging dir removed on success, hook does NOT fire on ansible_run failure, manual `cert clean-staging` works (no shred — D3 N-MAJ-3) |
| `TestHalfStatePolicy` | 4 | missing CA → full reissue, missing one leaf → full reissue, malformed base64 → full reissue, stale state + valid secrets → no reissue (D3 N-MAJ-2) |
| `TestCertDeployPathGuard` | 3 | empty path fails assert, non-/opt/kamailio-docker path fails assert, valid path passes (D3 N-MAJ-4) |
| `TestAnsibleRequirementsCollection` | 2 | `ansible/requirements.yml` exists with `ansible.posix`, pipeline runs `ansible-galaxy collection install` preamble (D4 F1) |
| `TestStageLabelsCompleteness` | 1 | `STAGE_LABELS["cert_provision"]` present and stable string (D4 F2) |
| `TestModuleSplitContract` | 2 | `tls_bootstrap` module imports no yaml/secrets state, `cert_lifecycle` is the only orchestration entry point (D3 N-MAJ-1) |

**Total: 74 new tests.** Expected baseline shift: 828 → 902.

### Mutant harness target

≥14/16 mutants. Categories:
1. CA expiry shortened to 1 year (must catch chain-invariant test)
2. SAN list truncated to 1 entry (must catch SanListIsExactlyTwo)
3. Leaf signed with wrong CA (must catch chain verify)
4. File mode 0644 on privkey (must catch file mode test)
5. state.yaml.cert_state inline-key leak (must catch metadata-only schema test)
6. Pipeline stage order flipped (cert_provision after ansible_run)
7. Schema strictness drop (additionalProperties: true)
8. RSA→DSA swap in leaf
9. Manual-mode validation skip (accept missing files silently)
10. Idempotency bypass (regenerate even when valid)
11. Dry-run writes files
12. Expired cert accepted
13. Validity period 1 day
14. Renew threshold flipped (renew at 365d instead of 30d)
15. Post-success cleanup runs on failure too
16. Acme mode silently accepted

## §12. Acceptance criteria

- [ ] All 74 new tests pass
- [ ] Mutant harness catches ≥14/16
- [ ] Full pytest suite remains green (828 + 74 = 902 expected)
- [ ] `bash scripts/dev/check-plan-sensitive.sh` returns OK
- [ ] Conflict check vs main empty
- [ ] PR review min-3 with fresh subagents
- [ ] Synthetic injection table in PR body is **literal script stdout** (no manual table; v6 lesson)
- [ ] Schema additions paired with negative `additionalProperties: false` test
- [ ] No production code change outside listed §7 files
- [ ] D1 + D2 design review iterations resolved every blocker
- [ ] dogfood re-verification passes: Kamailio TLS loads, container healthy

## §13. Carry-forward to next PRs

- **PR-AA** (verify cert preflight): smaller follow-up, depends on PR-Z's `tls_bootstrap.py` extension. Closes D1 M4 by adding the preflight in its own scope.
- **PR-AB** (k8s `PLACEHOLDER_KAMAILIO_INTERNAL_LB_ADDRESS` wiring): defer until post-cert dogfood iteration confirms it still blocks
- **PR-AC** (full ACME integration): separate design-first PR. Will set `actual_mode = acme` + clear `acme_pending` upon successful issuance.
- **PR-AD** (non-Kamailio TLS — admin SPA, talk, meet via shared CA): separate design-first PR. Will distribute `KAMAILIO_CA_CERT_BASE64` (or rename to `INSTALL_CA_CERT_BASE64`) to k8s secrets for client trust.

PR dependency graph: `PR-Z → PR-AA, PR-AC, PR-AD`. `PR-AB` is independent (k8s placeholder fix).

## §14. Live-integration verification commands (Phase 5c targets)

When PR-Z lands and dogfood is re-run, the verification sequence:

```bash
DOMAIN="dev.voipbin-install.example.com"
ZONE="us-central1-a"
PROJECT="voipbin-install-dev"
VM="instance-kamailio-voipbin-us-central1-a-0"

# 1. Cert files exist on VM at the correct paths
gcloud compute ssh "$VM" --zone="$ZONE" --project="$PROJECT" \
  --command="sudo ls -la /opt/kamailio-docker/certs/sip.${DOMAIN}/ /opt/kamailio-docker/certs/registrar.${DOMAIN}/"
# expected: fullchain.pem (0644), privkey.pem (0600) in each dir

# 2. Cert chain verifies (leaf signed by our CA)
gcloud compute ssh "$VM" --zone="$ZONE" --project="$PROJECT" --command="
  sudo openssl verify -CAfile /opt/kamailio-docker/certs/sip.${DOMAIN}/fullchain.pem \
    /opt/kamailio-docker/certs/sip.${DOMAIN}/fullchain.pem"
# expected: 'OK' (because fullchain contains both leaf and CA)

# 3. SANs match the expected list
gcloud compute ssh "$VM" --zone="$ZONE" --project="$PROJECT" --command="
  sudo openssl x509 -in /opt/kamailio-docker/certs/sip.${DOMAIN}/fullchain.pem -noout -text \
    | grep -A1 'Subject Alternative Name'"
# expected: 'DNS:sip.<DOMAIN>' (and only that)

# 4. Kamailio TLS module loads cleanly
gcloud compute ssh "$VM" --zone="$ZONE" --project="$PROJECT" \
  --command="sudo docker logs kamailio 2>&1 | grep -iE 'tls.*(load_cert|ERROR)' | head -20"
# expected: load_cert lines succeed, no ERROR

# 5. Kamailio container is healthy
gcloud compute ssh "$VM" --zone="$ZONE" --project="$PROJECT" \
  --command="sudo docker compose -f /opt/kamailio-docker/docker-compose.yml ps"
# expected: kamailio service is Up (healthy)

# 6. state.yaml.cert_state populated correctly
yq '.cert_state' ~/gitvoipbin/install/.voipbin-state.yaml
# expected: schema_version: 1, config_mode: self_signed, actual_mode: self_signed,
# san_list: [sip.<DOMAIN>, registrar.<DOMAIN>], leaf_certs has 2 entries

# 7. secrets.yaml has the 6 new keys (decrypted preview)
cd ~/gitvoipbin/install && sops -d secrets.yaml | yq '. | keys' | grep -E 'KAMAILIO_'
# expected: 6 keys (CA cert/key + 2 SAN cert/key pairs)

# 8. .cert-staging/ cleaned up post-success
ls -la ~/gitvoipbin/install/.cert-staging/ 2>&1
# expected: 'No such file or directory'
```

If any of these fail, the PR-Z design is incomplete and v7 cannot close.

## §15. D1 + D2 review responses

See §0 table. All blockers addressed.

---
**v3 status**: design closed. Ready for implementation. D3 + D4 findings reflected. PR-level review will catch residual issues.
