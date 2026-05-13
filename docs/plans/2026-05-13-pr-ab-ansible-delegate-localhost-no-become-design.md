# PR-AB Design — ansible delegate_to: localhost become: false

**Status:** D1 (single-axis; mechanical YAML fix, scope is small)
**Author:** Hermes (CPO)
**Parent:** PR-Z (#51) + PR-AA (#52)
**Branch:** `NOJIRA-PR-AB-ansible-delegate-localhost-no-become`
**Worktree:** `~/gitvoipbin/install/.worktrees/NOJIRA-PR-AB-ansible-delegate-localhost-no-become/`

## Goal

Unblock dogfood iter#10 by making PR-Z's `delegate_to: localhost` cert-
staging validation tasks run WITHOUT sudo on the operator's local machine.

## Background

Dogfood iter#9 (2026-05-13, post-PR-AA merge) advanced through
`cert_provision` (PR-AA fix worked live) and entered `ansible_run`. The
kamailio role's `common` tasks completed (17 ok), then the role's first
cert-staging validation task failed:

```
TASK [kamailio : Assert cert staging directory exists]
fatal: [instance-kamailio-…-0 -> localhost]: FAILED!
  module_stderr: |- sudo: a password is required
  module_stdout: ''
  msg: MODULE FAILURE
  rc: 1
```

Root cause: `ansible/ansible.cfg` declares globally:

```
become = True
become_method = sudo
become_user = root
```

`ansible/roles/kamailio/tasks/main.yml:36-45` has two tasks with
`delegate_to: localhost` (a `stat:` to check cert_staging_dir, and a
`fail:` that consumes its result). The global `become` applies to the
delegated tasks too, so ansible tries `sudo` on the operator's local
machine. Operator's local sudo is interactive (no NOPASSWD), so the
module aborts with `sudo: a password is required`.

The operator's local machine ALREADY has read access to the
cert_staging_dir (it was written by `cert_provision` running in the same
process). Sudo is not needed; it is a global-config artifact.

## Scope

### In

- `ansible/roles/kamailio/tasks/main.yml`: add `become: false` to the
  two `delegate_to: localhost` tasks (the `stat:` and the dependent
  `fail:`).
- New regression test `tests/test_pr_ab_ansible_delegate_no_become.py`
  with three guards:
  1. YAML-parse the kamailio role's main.yml, find every task with
     `delegate_to: localhost`, assert each has `become: false`.
  2. Repo-wide regex sweep across `ansible/roles/**/*.yml` and
     `ansible/playbooks/**/*.yml`: any task with
     `delegate_to: localhost` MUST also declare `become: false`.
  3. Static check that `ansible.cfg`'s global `become = True` is still
     present (the fix is a per-task override, not a global flip; flipping
     global become would break VM-side tasks).

### Out

- Splitting cert_staging validation out of the kamailio role entirely
  (deferred discussion — operator-side check is arguably redundant since
  cert_provision stage already records `staging_materialized` in
  state.yaml). Tracking as a `voipbin-install-dogfood-loop` skill nit;
  consider in PR-AC scope.
- Refactoring `ansible.cfg` to use task-level `become` declarations
  instead of global. Larger change, separate PR.
- Investigating rtpengine role for the same pattern — current grep shows
  zero `delegate_to: localhost` in rtpengine role, BUT the rtpengine role
  doesn't have the cert-staging dependency. If a future PR adds
  cert-staging-like operator-side validation to rtpengine, the regression
  test will catch the missing `become: false`.

### Non-goals

- Removing or relaxing the cert-staging validation logic. The check is
  correct; only its execution context (sudo on operator localhost) was
  wrong.
- Adding `become: false` to non-delegate tasks. The VM-side tasks
  legitimately need sudo for Docker/systemd/file-ownership ops.

## Design

### Approach: per-task `become: false` override

In `ansible/roles/kamailio/tasks/main.yml`, change:

```yaml
- name: Assert cert staging directory exists
  stat:
    path: "{{ cert_staging_dir }}"
  register: _cert_staging_stat
  delegate_to: localhost

- name: Fail if cert staging missing
  fail:
    msg: "cert_staging_dir {{ cert_staging_dir }} does not exist. The cert_provision pipeline stage must run before ansible_run."
  when: not _cert_staging_stat.stat.exists or not _cert_staging_stat.stat.isdir
```

To:

```yaml
- name: Assert cert staging directory exists
  stat:
    path: "{{ cert_staging_dir }}"
  register: _cert_staging_stat
  delegate_to: localhost
  become: false

- name: Fail if cert staging missing
  fail:
    msg: "cert_staging_dir {{ cert_staging_dir }} does not exist. The cert_provision pipeline stage must run before ansible_run."
  when: not _cert_staging_stat.stat.exists or not _cert_staging_stat.stat.isdir
  delegate_to: localhost
  become: false
```

Why `become: false` on the `fail:` too: the `fail:` module itself runs on
the delegated host (which we now want to be localhost without sudo). Even
though it does not touch the filesystem, executing under sudo would still
hit `sudo: a password is required`. Adding `delegate_to: localhost` to
the `fail:` makes the no-become intent explicit; absent the delegate, the
fail would run on the VM (where sudo is fine) but with `when:` evaluating
a register variable that originated on localhost — confusing audit
surface. Pin both to localhost+no-become for symmetry.

## Trade-offs

| Decision | Pro | Con |
|---|---|---|
| Per-task `become: false` override | Minimal blast radius, mirrors ansible upstream guidance for delegate_to localhost | Two more lines per task; future contributors must remember the pattern (regression test enforces) |
| Vs. flipping global `become = False` in ansible.cfg | Would also fix this | Breaks every VM-side task that needs root (Docker install, systemd, /opt mounts, etc.) — would cascade-fail across the playbook |
| Vs. removing the cert-staging check entirely | Less code | Operator running `--stage ansible_run` standalone (skipping cert_provision) would silently corrupt the deploy; the assert is the safety net for that path |
| Vs. moving check into cert_provision stage | Cleaner separation | Larger refactor; defer to a follow-up PR if the pattern recurs |

## Test surface

| Test | What it pins | Synthetic injection result |
|---|---|---|
| `test_kamailio_delegate_tasks_have_no_become` | Parse `ansible/roles/kamailio/tasks/main.yml`; for each task whose `delegate_to == 'localhost'`, assert `become is False` (explicit, not absent — absence inherits global True) | FAIL when `become: false` removed |
| `test_repo_wide_delegate_localhost_pairs_with_no_become` | Walk `ansible/roles/**/*.yml` + `ansible/playbooks/**/*.yml`; same assertion as above for every task | FAIL when a new role-level `delegate_to: localhost` task is added without `become: false` |
| `test_ansible_cfg_global_become_still_true` | Parse `ansible/ansible.cfg`; assert `[privilege_escalation].become == True`. Pins that PR-AB did NOT relax the global setting (which would break VM-side tasks) | FAIL if someone "fixes" the iter#9 issue by flipping the global flag |

## Acceptance criteria

1. All three tests in `tests/test_pr_ab_ansible_delegate_no_become.py`.
2. Synthetic injection proof in PR body (programmatic, not hand-classified).
3. `pytest tests/ -q --ignore=tests/test_pr_w_conftest_import_shim.py` →
   919 + 3 = **922 passed** (with age binaries) or 920 passed + 2 skipped
   (without age).
4. `bash scripts/dev/check-plan-sensitive.sh` → OK.
5. `git merge-tree` vs `origin/main` → no conflicts.
6. Code review min 3 iterations.

## Concurrency

N/A. YAML-only change; no runtime concurrency surface.

## Abort criteria specific to this PR

- If iter#10 (post-merge) surfaces a DIFFERENT failure mode in the same
  task block (e.g. cert_staging_dir path resolves to wrong location on
  localhost vs VM), open a separate PR-AB-1 — do not expand PR-AB scope
  with speculative fixes.

## Risks

| Risk | Mitigation |
|---|---|
| `become: false` on `delegate_to: localhost` does not actually suppress sudo on the operator's machine in some ansible versions | Test would catch nothing because tests are YAML-shape only; rely on iter#10 live verification |
| Future task author adds another `delegate_to: localhost` without `become: false` | Regression test #2 (repo-wide sweep) catches this at pytest time, before merge |
| Operator's local environment has NOPASSWD sudo and the bug "works" silently | Test #1 catches the regression regardless of operator environment |

## Carry-forward to next PR

- PR-AC scope (deferred from this PR): PR-Z mutant survivors #11 + #16,
  plus skill patches for (a) programmatic mutant table mandatory before
  PR open, (b) "delegate_to: localhost pairs with become: false" pitfall.

## Open questions

None. The fix is mechanical YAML. Trade-off analysis settled.

## Resume marker

After merge: pull main, remove worktree, run dogfood iter#10 to verify
ansible_run advances past the cert-staging validation tasks.
