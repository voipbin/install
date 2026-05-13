# PR-AA Design — cert_provision sops re-encrypt tempfile name fix

**Status:** D2 (revised after D1 review — incorporates B1 + M1/M2/M3/M4 + minors)
**Author:** Hermes (CPO)
**Parent:** PR-Z (#51, merged 2026-05-13 as 9d93695)
**Branch:** `NOJIRA-PR-AA-cert-provision-sops-tempfile-name`
**Worktree:** `~/gitvoipbin/install/.worktrees/NOJIRA-PR-AA-cert-provision-sops-tempfile-name/`

## Goal

Make `cert_provision` stage's secret-persistence path actually re-encrypt
`secrets.yaml` with sops, so the `cert_provision → ansible_run` chain on
dogfood (`voipbin-install-dev`) can advance past PR-Z's first live failure.

## Background

Dogfood iter#8 (2026-05-13, immediately post-PR-Z merge) failed at
`cert_provision` with the message `cert_provision: sops re-encryption failed`
and halted the pipeline. Direct reproduction:

```
$ sops --encrypt --in-place --gcp-kms "$KMS" /tmp/secrets-iter8-plain.yaml
error loading config: no matching creation rules found
```

Root cause is in `scripts/pipeline.py:495-498` (`_persist_secrets_after_reissue`):

```python
fd, tmp_str = tempfile.mkstemp(
    prefix="secrets.", suffix=".plain", dir=str(config._dir),
)
```

The generated tempfile name `secrets.XXXXXX.plain` does NOT match the
`.sops.yaml` `creation_rules[].path_regex: secrets\.yaml$`. sops 3.12.x
loads `.sops.yaml` from the working directory and rejects the call when no
rule matches, even though `--gcp-kms` is supplied explicitly on the command
line. The path-regex match is checked BEFORE falling back to the CLI flag.

This bug was masked by `tests/test_pr_z_pipeline_cert_stage.py` because that
test mocks `encrypt_with_sops` — it never invokes the real sops binary, so
the tempfile-name × path_regex interaction was never exercised.

## Scope

### In

- `scripts/pipeline.py:_persist_secrets_after_reissue` — change tempfile naming
  so sops's `.sops.yaml` rule resolution accepts it. Two-line change.
- New regression test that calls the real `sops` binary against a stub
  `.sops.yaml` + tempfile produced by the function, asserting re-encryption
  succeeds. Skipped (not failed) if sops binary or GCP KMS access unavailable
  in CI sandbox; the assertion runs only when both prerequisites are present.
- New mock-side guard test that pins the tempfile-name shape (regex match
  against `secrets\.yaml$`) so a future refactor cannot regress to the prior
  shape without the pin failing.
- Synthetic injection proof in PR body: revert tempfile naming to the
  PR-Z-broken form, re-run new tests, confirm at least one fails.

### Out

- Mutant survivors #11 (APPLY_STAGES order) and #16 (KAMAILIO_PAIRS swap)
  from PR-Z's harness. Defer to a follow-up PR after iter#9 confirms the
  cert_provision → ansible_run flow composes correctly. Bundling them here
  would (a) widen the PR beyond a single root-cause category, (b) spend
  guarding-effort before knowing how ansible_run actually consumes the
  certs, which determines which guards matter most.
- Generalizing the sops re-encryption helper to a shared utility. The
  current call site is the only one in the codebase. If a second site
  appears, refactor then.
- Refactoring `secretmgr.encrypt_with_sops` to surface sops's stderr.
  Worthwhile but unrelated; track as a follow-up note.

### Non-goals

- Avoid `.sops.yaml` discovery via `--config /dev/null` or `SOPS_CONFIG_PATH`
  override. The natural path is to make the tempfile match the rule, which
  also keeps the file discoverable by future operators if they ever inspect
  it directly. Bypassing `.sops.yaml` would be one extra coupling between
  installer code and sops's argument parser.

## Design

### Approach: tempfile-name pattern that matches `secrets\.yaml$`

Replace:

```python
fd, tmp_str = tempfile.mkstemp(
    prefix="secrets.", suffix=".plain", dir=str(config._dir),
)
```

With:

```python
# Tempfile MUST end in `secrets.yaml` so .sops.yaml's
# `path_regex: secrets\.yaml$` rule matches. sops resolves rules from the
# working dir BEFORE honoring --gcp-kms, so a non-matching name fails with
# `no matching creation rules found`. PR-AA dogfood iter#8 lesson.
fd, tmp_str = tempfile.mkstemp(
    prefix="cert-staging-", suffix=".secrets.yaml", dir=str(config._dir),
)
```

Resulting names: `cert-staging-XXXXXX.secrets.yaml`. The `.secrets.yaml` suffix
satisfies the path_regex. The `cert-staging-` prefix marks the file as
PR-AA-owned for diagnostic clarity. Files are deleted in the `finally` block
on success or failure.

### Atomic replace target unchanged

`os.replace(str(tmp_path), str(config.secrets_path))` keeps the on-disk
artifact named `secrets.yaml`. The tempfile name only matters during the
sops invocation window.

### Cleanup safety

The existing `try/finally` already unlinks the tempfile on both success and
failure paths. Tempfiles surviving a process crash will now show as
`cert-staging-*.secrets.yaml` in the workdir, which is greppable and
distinguishable from the canonical `secrets.yaml`.

## Trade-offs

| Decision | Pro | Con |
|---|---|---|
| Match path_regex via tempfile rename | Minimal change, no sops flag coupling, future-friendly | Tempfile name now embeds knowledge of `.sops.yaml` rule |
| Vs. `--config /dev/null` bypass | Bypass loses operator's ability to add per-key overrides via .sops.yaml | Would have been one-line CLI flag |
| Vs. `SOPS_CONFIG_PATH` env override | Same as above | Same as above |
| Single-PR scope (sops fix only) | Honest 1-root-cause-per-PR | Mutant #11/#16 still open after PR-AA |
| Real-sops test skipped when KMS unavailable | CI portability | Test does not run in unit-only sandbox; rely on dogfood iter#9 to confirm end-to-end |

## Test surface

All tests live in `tests/test_pr_aa_cert_persist_tempfile.py`. Real-sops
round-trip uses an **age** keypair generated in-test (no GCP/KMS dependency)
so it runs in any CI sandbox that has the `sops` and `age` binaries on
PATH. age binary presence is checked at module import; the round-trip test
is `skip`ped only when `sops` or `age` is genuinely absent, not when GCP
credentials are missing.

| Test | What it pins | Synthetic injection result |
|---|---|---|
| `test_persist_tempfile_name_matches_sops_path_regex` | Loads the regex from `write_sops_config`'s output (the source of truth, NOT a hard-coded string), captures the path passed to `encrypt_with_sops` via mock, asserts `re.search(rule_regex, captured_tmp_path.name)` is not None. M1 fix. | FAIL when reverted to `prefix="secrets.", suffix=".plain"` |
| `test_persist_tempfile_lives_in_config_dir` | Tempfile is created under `config._dir`, not `/tmp` | FAIL when `dir=` arg dropped |
| `test_persist_tempfile_cleaned_on_sops_failure` | Tempfile unlinked even when `encrypt_with_sops` returns False | FAIL when `finally` block removed |
| `test_persist_tempfile_cleaned_on_sops_success` | Tempfile unlinked after successful `os.replace` (no orphan) | FAIL when `os.replace` swapped for `shutil.copy` |
| `test_persist_real_sops_age_round_trip` | End-to-end with REAL sops binary + age recipient: write secrets dict → run actual `sops --encrypt --in-place --age <recipient>` against the tempfile produced by the function → confirm rc==0 → decrypt with `sops --decrypt` and assert payload roundtrips. Skipped only when `sops` or `age` binary missing. M2 fix. | FAIL when reverted to broken tempfile name (the actual-execution gate) |
| `test_orphan_secrets_plaintext_swept_on_entry` | Pre-create `secrets.abc.plain` and `secrets.def.plain` in config dir. Call `_persist_secrets_after_reissue`. Assert both orphans are unlinked before sops invocation. M4 fix. | FAIL when sweep block removed |

## Acceptance criteria

1. All six new tests present in `tests/test_pr_aa_cert_persist_tempfile.py`.
2. Synthetic injection proof in PR body — table showing each test × bug
   reinjected × FAIL result.
3. `pytest tests/ -q --ignore=tests/test_pr_w_conftest_import_shim.py` →
   915 passed (909 baseline + 6 new; the age-round-trip test runs when
   `sops` and `age` are on PATH, otherwise `skip`ped — adjust to
   914 passed / 1 skipped if age binary unavailable in dev env).
4. `bash scripts/dev/check-plan-sensitive.sh` → no findings.
5. `git merge-tree` vs `origin/main` → no conflicts.
6. Code review min 3 iterations completed.

## Concurrency

The cert_provision stage is single-flighted by the pipeline driver: only
one `voipbin-install apply` invocation per workdir is the documented
operator contract (state.yaml lock + `.voipbin-state.yaml` checkpoint
serialize stage entry). No additional file lock is added in PR-AA. If a
future PR introduces parallel apply (e.g. multi-environment fan-out from
one workdir, which would be a major architectural change), revisit this
assumption and add `flock(config._dir / ".cert-provision.lock")` around
the persist block. Documented as a carry-forward.

## Abort criteria specific to this PR

- If the real-sops round-trip test FAILS even after the tempfile rename,
  the diagnosis is wrong (sops 3.12.x has additional gating beyond
  path_regex). Stop, re-investigate, do NOT ship a speculative fix.
- If review iteration surfaces a second root cause in `cert_provision`
  (e.g. cert_state schema mismatch), open a separate PR-AB for it; do not
  expand PR-AA scope.

## Risks

| Risk | Mitigation |
|---|---|
| sops 3.13.x changes path_regex semantics | Pin sops version constraint in operator docs as follow-up; test asserts behavior on 3.12.x which is what dogfood runs |
| Tempfile name collision with operator's manually-named files | `cert-staging-` prefix is reserved by this code path; unlikely operator collision |
| Other call sites use the old tempfile pattern | Grep confirms `_persist_secrets_after_reissue` is the only caller; no shared helper to update |
| Real-sops test flake on dogfood machine due to KMS quota | Test is skipped when KMS access unavailable; flake risk is bounded to local dev runs |

## Carry-forward to next PR

- **PR-AB candidate:** mutant survivors #11, #16 from PR-Z + any iter#9 nits.
- **PR-AC candidate:** ACME mode (already documented as future in PR-Z).
- **Follow-up note:** `secretmgr.encrypt_with_sops` should surface sops's
  stderr to make future failures self-diagnosing without needing direct
  reproduction.

## Open questions

None. The fix shape is mechanical; the design's only choice was tempfile
rename vs sops-config bypass and the trade-off table covers both.

## Resume marker

After merge: pull main locally, remove worktree, run dogfood iter#9 to
verify cert_provision short-circuits OR reissues + ansible_run advances.
