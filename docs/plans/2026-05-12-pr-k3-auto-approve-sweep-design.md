2026-05-12 — PR-K3: Pre-emptive auto-approve sweep + actual-execution smoke

Background

Smoke dogfood of the apply pipeline revealed three P0 failures in succession:
  - GAP-32: terraform_reconcile stage split state-migration bug
  - GAP-33: `gcloud storage buckets create` rejected `--versioning` flag (no such flag; correct flag is `--uniform-bucket-level-access` and `gcloud storage buckets update --versioning` is a separate call)
  - GAP-34: `scripts/terraform_reconcile.py:472` calls `confirm(...)` unconditionally, with no `auto_approve` plumbing — `voipbin-install apply --auto-approve` aborted with `tcsetattr: Inappropriate ioctl for device`

The roadmap §5 abort trigger fired. Root cause analysis pointed to a process gap, not a single-bug gap: mock-only tests never exercised actual CLI flag parsing or interactive-prompt paths. PR-K3 closes that gap pre-emptively by sweeping every interactive call site and adding two actual-execution smoke tests that would have caught both GAP-33 and GAP-34 before merge.

Sweep results

Grepped scripts/ for `confirm(`, `input(`, `click.confirm`, `click.prompt`. Nine call sites found (the `def confirm(...)` definition in `display.py:120` is excluded).

| # | File:line | Reachable from apply --auto-approve? | Currently guarded? | Classification | Action |
|---|-----------|--------------------------------------|--------------------|----------------|--------|
| 1 | scripts/diagnosis.py:74 (offer_adc_setup) | yes (preflight) | yes — `if not auto_accept:` | (a) | leave alone |
| 2 | scripts/diagnosis.py:376 (offer_tool_install) | no — only called from `scripts/commands/init.py:100,108` | no | (c) | out-of-scope (init-only) |
| 3 | scripts/terraform_reconcile.py:472 (imports) | YES (reconcile_imports stage) | NO | (b) | FIX: add `auto_approve` param + guard |
| 4 | scripts/commands/apply.py:94 (Re-apply?) | yes | yes — `if not auto_approve` | (a) | leave alone |
| 5 | scripts/commands/apply.py:99 (Proceed with deployment?) | yes | yes — `if not auto_approve` | (a) | leave alone |
| 6 | scripts/commands/init.py:89 (Reconfigure?) | no (init command) | no | (c) | out-of-scope (init-only) |
| 7 | scripts/commands/destroy.py:50 (Attempt destroy anyway?) | no (destroy command) | yes — `if not auto_approve` | (c) | out-of-scope (destroy-only); already guarded anyway |
| 8 | scripts/commands/destroy.py:71 (Are you sure...) | no (destroy command) | no | (c) | out-of-scope (destroy-only) |
| 9 | scripts/terraform_reconcile.py — outputs() | yes (reconcile_outputs stage) | n/a — no interactive prompts in this function | (a) | leave alone (verified by re-reading lines 546-580) |

Counts: (a) = 4 guarded-and-reachable + 1 reachable-no-prompt = 5; (b) = 1 (the bug — terraform_reconcile.imports); (c) = 4 init/destroy-only.

Fix for (b)

`scripts/terraform_reconcile.py::imports(config)` gains an `auto_approve: bool = False` parameter. When True, the `confirm(...)` at line 472 is skipped and the function proceeds straight to the import loop (preserving the same emitted "Importing..." log line, just without the gate). When False, current behavior is preserved exactly.

`scripts/pipeline.py::_run_reconcile_imports` currently calls `_terraform_imports(config)` on line 153 with no `auto_approve` argument, even though its own signature already accepts `auto_approve`. The fix is a one-line change to forward the flag.

Test strategy

Two new tests in `tests/test_cli_smoke.py`:

1. `test_state_bucket_gcloud_flags_valid_syntax` (GAP-33 cross-check)
   - For each branch of `ensure_state_bucket` (create / describe-already-exists / update-existing), patch `scripts.state_bucket.run_cmd` to capture the argv list and force the desired branch.
   - For each captured argv that starts with `gcloud`, run `gcloud <subcommand path> --help` and assert every `--flag` in the captured argv appears verbatim in the help text. SKIP gracefully via `pytest.skip` if `gcloud` is not on PATH (CI hygiene).
   - Synthetic proof: re-introducing the actual GAP-33 incident shape (changing `--public-access-prevention` to `--public-access-prevention=enforced` in the `gcloud storage buckets create` argv on `scripts/state_bucket.py:97`) causes this test to fail with a "documented as a boolean … but argv passes it the `=value` form" assertion. The test parses `gcloud storage buckets create --help` for the `--[no-]flagname` syntax to identify boolean flags and rejects any `--flag=value` token whose flag is documented as a pure boolean (no `--flag=METAVAR` form). The legacy positive check (unknown flag such as adding `--versioning` to `create`) also still fails the test.

2. `test_reconcile_imports_auto_approve_no_stdin_read` (GAP-34 closed-stdin smoke)
   - Pragmatic scope reduction (per task brief): exercise the `_run_reconcile_imports` runner directly with `auto_approve=True` and a closed-stdin fixture. Full `apply_pipeline` end-to-end was considered but rejected — too many stubs needed for terraform / k8s / dns to give a meaningful signal vs. unit-level test that pinpoints the bug.
   - Replace `sys.stdin` with a closed `io.StringIO()` and patch `scripts.terraform_reconcile.terraform_state_list` to return empty, `scripts.terraform_reconcile.check_exists_in_gcp` to return `(True, True)` (forces the confirm path), `scripts.terraform_reconcile.import_resource` to return `(True, "")`, and `build_registry` to return one fake entry. Call `_run_reconcile_imports(config, {}, dry_run=False, auto_approve=True)` and assert it completes without raising `EOFError`/`OSError` (which `confirm()` -> `input()` would raise on a closed stream).
   - Synthetic proof: dropping the `auto_approve=auto_approve` forwarding from `_run_reconcile_imports` (i.e. reintroducing the GAP-34 bug) causes this test to fail with `EOFError`.

Out-of-scope

- `scripts/commands/init.py:89` (Reconfigure?) — only reached via `voipbin-install init`. Not in the apply pipeline. Future hardening could add `--non-interactive` to init, but not in this PR.
- `scripts/commands/destroy.py:71` (Are you sure...) — only reached via `voipbin-install destroy`. There is a deliberate design choice to *not* allow destroy to run without an interactive confirmation by default; an `--auto-approve` flag exists but the second confirmation is intentional. Leave alone.
- `scripts/diagnosis.py:376` (offer_tool_install) — reached only from `init` flow. Not in apply path.

Review iterations

Per working agreement, two design-review rounds and three code-review rounds were performed with independent reviewer personas (no `delegate_task` tool was available in this environment, so reviewer iterations were performed as separate self-reviews with distinct prompts to enforce fresh-eyes critique). All findings are absorbed into this design and the implementation.
