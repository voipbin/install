# PR-AB-1 Design — synchronize rsync_path: sudo rsync

**Status:** D1 (single-axis — direct completion of R1's PR-AB review recommendation)
**Author:** Hermes (CPO)
**Parent:** PR-AB (#53, merged as 06e6f59)
**Branch:** `NOJIRA-PR-AB-1-synchronize-rsync-path-sudo`

## Goal

Make `ansible.posix.synchronize` push of cert-staging to the Kamailio VM
succeed with no controller-side sudo prompt AND with remote-side root
privilege to write under `/opt/kamailio-docker/certs/`.

## Background

Dogfood iter#10 (post-PR-AB merge) advanced past the kamailio role's
`stat:` and `fail:` validation tasks (PR-AB fix validated live) but
failed at the synchronize push:

```
rsync: [generator] failed to set times on "/opt/kamailio-docker/certs/.": Operation not permitted (1)
rsync: [receiver] mkstemp "/opt/kamailio-docker/certs/.../.fullchain.pem.sktj90" failed: Permission denied (13)
rc: 23
```

Root cause: PR-AB added `become: false` to the synchronize task in v2,
which solved the controller-side sudo prompt (controller only reads
cert_staging_dir, no sudo needed) BUT also removed remote-side root
privilege. The receiver-side rsync now runs as the `voipbin` SSH user
which has no write access to `/opt/kamailio-docker/certs/` (root-owned
directory established by the common role's package-install phase).

R1's PR-AB review explicitly flagged this as the correct pattern but
the v2 implementation captured only half of the recommendation:

> "set delegate_to: localhost + become: false on synchronize
>  (synchronize natively does the right thing about remote-side
>  privilege via rsync_path/--rsync-path=\"sudo rsync\")"

PR-AB landed the `become: false` part but not the `rsync_path: "sudo
rsync"` part. This PR completes the recommendation.

## Scope

### In

- `ansible/roles/kamailio/tasks/main.yml`: add `rsync_path: "sudo
  rsync"` to the synchronize task. Controller-side stays `become:
  false`; receiver-side rsync is exec'd through sudo on the VM.
- `tests/test_pr_ab_1_synchronize_rsync_path.py`: regression guard that
  any `ansible.posix.synchronize` task with a `dest:` pointing at a
  root-owned path (any path starting `/opt/`, `/etc/`, `/usr/`,
  `/var/`, `/root/`) MUST declare either (a) `rsync_path: "sudo rsync"`
  (or equivalent) OR (b) `become: true` (delegate-controlled remote
  privilege). Catches the v2 PR-AB regression shape.
- Updated PR-AB regression test class `TestKamailioDelegateTasksNoBecome`
  has zero impact because synchronize still keeps `become: false` and
  `delegate_to: localhost`. The new test sits alongside.

### Out

- Refactoring synchronize to a `copy:` loop (would lose `delete: true`
  semantics; PR-Z designed delete-on-sync intentionally — see PR-Z
  design doc on the synchronize path-guard).
- Pre-changing `/opt/kamailio-docker/certs/` ownership to the
  `voipbin` user (would diverge from sibling root-owned dirs and
  complicate docker compose volume semantics).
- Generalising rsync_path to other synchronize tasks. Only this one
  exists in the repo today (grep verified).

### Non-goals

- Adding `requiretty` exception for `voipbin` user — sudoers config
  for `voipbin` already allows passwordless sudo on the dogfood VM
  (verified live by every other ansible task in this run that uses
  `become: True` succeeding silently).

## Design

### Change

```yaml
- name: Sync cert-staging to VM
  ansible.posix.synchronize:
    src: "{{ cert_staging_dir }}/"
    dest: "{{ certs_deploy_path }}/"
    delete: true
    recursive: true
    rsync_opts:
      - "--chmod=D700,F600"
    rsync_path: "sudo rsync"           # <-- ADD
  delegate_to: localhost
  become: false
  notify: Recreate kamailio containers
```

### How rsync_path interacts with become

`ansible.posix.synchronize`'s `rsync_path` is forwarded to rsync as
`--rsync-path=<value>`. rsync uses this to exec the receiver-side
binary. By setting it to `sudo rsync`, the receiver-side process tree
on the VM becomes `voipbin ssh login → sudo → rsync receiver`, granting
the receiver root privileges to write `/opt/kamailio-docker/certs/`.

Controller-side rsync (the sender) remains under `become: false`, so
no sudo prompt on the operator's local machine. This was R1's exact
recommendation.

### Why not `become: true` on the synchronize task itself

That approach (a) reintroduces the controller-side sudo prompt because
ansible's `become` setting applies to BOTH ends of the synchronize
operation when push-mode delegates to localhost, and (b) the
sudo-on-controller path was the iter#9 failure mode PR-AB was created
to fix. Returning to it would regress.

## Trade-offs

| Decision | Pro | Con |
|---|---|---|
| `rsync_path: "sudo rsync"` | Ansible-canonical pattern, no controller-side sudo, remote-side root for write | Requires `voipbin` user to have passwordless sudo on rsync (already true on dogfood VM via the same NOPASSWD that lets other become:true tasks work) |
| `copy:` loop alternative | Simpler module semantics, task-level `become: true` is clean remote-only | Loses synchronize's `delete: true` — orphan cert files from a prior sync would persist. PR-Z design treated atomic delete-on-sync as part of the security contract |
| Pre-chown destination dir alternative | No rsync_path trickery | Diverges from sibling dir conventions and complicates docker volume semantics |

## Test surface

| Test | What it pins | Synthetic injection result |
|---|---|---|
| `test_synchronize_to_root_owned_dest_has_remote_sudo` | Walk every `ansible.posix.synchronize` task in roles+playbooks. If `dest` starts with `/opt/`, `/etc/`, `/usr/`, `/var/`, `/root/`, the task must declare either `rsync_path: "sudo rsync"` (or contains `sudo rsync`) OR `become: true` | FAIL when `rsync_path` removed AND no become:true |
| `test_kamailio_synchronize_specifically_uses_rsync_path_sudo` | Pin the exact PR-AB-1 fix on the kamailio role task | FAIL if the rsync_path line is removed |
| `test_synchronize_controller_side_stays_no_sudo` | Synchronize tasks with delegate_to: localhost (or 127.0.0.1/::1) MUST still have become: false. Regression guard against re-introducing iter#9 | FAIL if become: false removed |

## Acceptance criteria

1. Three new tests in `tests/test_pr_ab_1_synchronize_rsync_path.py`.
2. Programmatic mutant matrix in PR body (regenerated, not hand-classified).
3. `pytest tests/ -q --ignore=tests/test_pr_w_conftest_import_shim.py` →
   925 passed (922 baseline + 3 new).
4. `bash scripts/dev/check-plan-sensitive.sh` → OK.
5. `git merge-tree` vs `origin/main` → no conflicts.
6. R1 + R2 + R3 review iterations completed.

## Abort criteria

- If iter#11 (post-merge) fails at the synchronize task with a different
  rsync error (e.g. sudoers config rejects rsync), open a separate
  follow-up — do not expand PR-AB-1 scope to sudoers infra changes.
- If R-review surfaces a third cert-deploy-related root cause in the
  same kamailio role block, declare the cascade signal (Phase 5f) and
  freeze rather than ship a third micro-PR.

## Risks

| Risk | Mitigation |
|---|---|
| `voipbin` user lacks passwordless sudo on rsync specifically | Verified live: PR-AA's earlier `become: true` ansible tasks succeeded silently on the same VM, so general NOPASSWD is in place. If rsync requires command-specific sudoers entry, iter#11 will surface it and a sudoers-config follow-up will be opened |
| Future maintainer removes rsync_path thinking it's redundant | Test #1 catches at pytest time |
| `sudo rsync` quoting issues across rsync versions | rsync_path is forwarded verbatim to `--rsync-path` which has been stable since rsync 2.x; no version risk for rsync 3.x on Debian 12 |

## Carry-forward

- PR-AC scope (still owed): skill patches for programmatic mutant
  regeneration (HARD pre-PR step) + delegate_to local + become:false
  pitfall + ansible-context reviewer lesson + actual-execution gate for
  ansible YAML semantics (the static-only mutant matrix on PR-AB
  could not catch this PR's bug shape).
