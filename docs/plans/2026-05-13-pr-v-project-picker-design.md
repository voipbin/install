# PR-V — Interactive GCP project picker for `voipbin-install init` wizard

Status: Draft v3 (iter-1 + iter-2 fixes applied)
Author: Hermes (CPO)
Date: 2026-05-13
Worktree: `~/gitvoipbin/install/.worktrees/NOJIRA-PR-V-project-picker`
Branch: `NOJIRA-PR-V-project-picker`
Predecessor: PR #46 PR-U-3 (merged 55bdace)
Successor: none

---

## 1. Problem statement

`scripts/wizard.py:71-82` (Q1 "GCP Project ID") currently asks the operator to type the project ID manually. It auto-detects a single value via `gcloud config get-value project` and presents it as the prompt default, but the operator must already know the exact project ID — typos, copy-paste errors, and "did I mean dev or prod?" mistakes are common.

For pchero's dogfood workflow specifically (multiple GCP projects under one gcloud account), the typing UX is friction at minimum and a foot-gun at worst — a mistyped project ID will pass `_validate_project_id` (format-only check), fail `check_gcp_project` (which then aborts with "Cannot access project '<typo>'"), and the operator restarts `init`.

PR-V replaces the free-form text prompt with a list-and-pick UI: call `gcloud projects list` to enumerate ACTIVE projects the operator can see, annotate each with billing status, render through the existing `prompt_choice` helper, and append a "[N+1] Enter manually..." escape hatch so the wizard remains usable when the projects list is empty (no permissions), too long (50+), or the desired project is in a different org/folder not surfaced by the default list call.

## 2. Goals (numbered, testable)

1. Replace the free-form `prompt_text("Enter your GCP project ID")` in `scripts/wizard.py:77-81` with a numbered-choice picker built on the existing `prompt_choice` helper.
2. New helper `scripts/gcp.py:list_active_projects() -> list[ProjectListing]` returns ACTIVE projects from `gcloud projects list --filter=lifecycleState:ACTIVE --format=json`, sorted alphabetically by projectId. Empty list on any failure (no raise — graceful degradation).
3. Each `ProjectListing` carries (`project_id: str`, `name: str`, `billing_enabled: bool | None`). `billing_enabled=None` means "could not determine" (permission denied, transient failure) — display as "billing: unknown" rather than asserting False.
4. Billing status is fetched in a single batch call when feasible (`gcloud beta billing projects list --format=json`) to avoid N gcloud round-trips. Fallback to per-project on permission failure: silently downgrade to `billing_enabled=None` for affected entries.
5. The picker shows: `[N] <project_id>  —  <name> (billing: yes|no|unknown)`. The current `gcloud config get-value project` value (if present in the list) becomes the default selection. The detected project gets a `*` marker.
6. Last numbered entry is `[N+1] Enter manually...` which falls back to the existing `prompt_text` flow (preserves escape hatch for empty list / cross-org projects).
7. If `list_active_projects()` returns an empty list (zero projects visible OR gcloud failure), skip the picker entirely and fall through to the existing text prompt. Behavior is backward-compatible — no regression for operators with restricted IAM.
8. Existing validation (`_validate_project_id`) and existing post-prompt checks (`check_gcp_project`, `check_gcp_billing`) remain unchanged. Picker-selected values flow through the same validation path as text-entered values.
9. Keep PR strictly within wizard Q1 + new gcp.py helper + tests. NO changes to `apply`/`destroy`/`status`/`verify` CLI surface (those remain config.yaml-bound — sufficient for the current single-active-project pattern).
10. After PR-V merges, `voipbin-install init` on a host with multiple ACTIVE projects shows a numbered list; operator picks by number; selected project_id flows into config.yaml unchanged. Existing single-project hosts see identical behavior to today (auto-detected default works the same).

## 3. Non-goals (explicit scope cuts)

- Global `--project=<id>` CLI option for `apply`/`destroy`/`status`/`verify`. Sufficient via config.yaml + directory pattern; defer until multi-project operator workflow is genuinely common.
- `voipbin-install switch-project` standalone command. Use `init --reconfigure` for post-init project changes.
- Searching/filtering when the list is huge (50+ projects). Out of scope. If needed later, add a `--filter <substring>` option to the picker — separate PR.
- Listing projects from non-default organizations or folders not visible to the operator's IAM. Out of scope (escape hatch via manual entry covers this).
- Caching the project list across `init` invocations. Each `init --reconfigure` re-fetches. Stale data is a known UX cost we accept.
- Validating that the operator's gcloud account has `billing.projects.get` permission before attempting the billing batch call. We tolerate permission failures gracefully (degrades to `billing_enabled=None`).
- Removing the existing `get_project_id()` helper. Still used as the "default selection" signal inside the picker.

## 4. Affected files (table: file → why)

| File | Why | Change type |
|---|---|---|
| `scripts/gcp.py` | Add `list_active_projects()` and `ProjectListing` dataclass; both batch billing call and per-project fallback live here | append |
| `scripts/wizard.py` | Q1 picker integration; fallback to text-prompt on empty list / "Enter manually" choice | modify |
| `tests/test_pr_v_project_picker.py` | New test file (14 cases) | new |
| `tests/test_wizard.py` (if exists) | Verify no regression in wizard flow | check at impl time |
| `docs/plans/2026-05-13-pr-v-project-picker-design.md` | This file | new |

Estimated diff: ~120 LOC added, ~15 LOC modified across 4 code files + 1 design + 1 test. Single-PR scope.

## 5. Exact string replacements / API changes

### 5.1 `scripts/gcp.py` — append after `get_project_id()` (around L46)

**Iter-1 finding I1 resolved.** `gcloud beta billing projects list` requires `--billing-account=<id>` and lists projects under ONE account at a time. The previous v1 design's assumption of a single global call was wrong. v2 uses the two-step accounts-first pattern: enumerate billing accounts via `gcloud billing accounts list --format=json`, then iterate `gcloud beta billing projects list --billing-account=<id>` per account, unioning results into a single `{projectId: billingEnabled}` map. Empirically verified against pchero's dogfood gcloud (2 billing accounts, 6 ACTIVE projects spread across them).

```python
@dataclass
class ProjectListing:
    """A GCP project surfaced for picker selection.

    billing_enabled is tri-state:
      True   — confirmed billing-enabled (safe to deploy)
      False  — confirmed billing-disabled (terraform_apply will fail)
      None   — could not determine (permission denied, no billing accounts
               visible, gcloud beta component missing, etc.)
    """
    project_id: str
    name: str
    billing_enabled: Optional[bool]


def _fetch_billing_map() -> dict[str, bool]:
    """Build {projectId: billingEnabled} by iterating billing accounts.

    Returns an empty dict on any failure (no billing accounts visible,
    permission denied, gcloud beta component missing, JSON parse fail).
    Callers treat absence in the map as `billing_enabled=None`.
    """
    accounts_result = run_cmd(
        ["gcloud", "billing", "accounts", "list",
         "--format=json", "--filter=open=true"],
        timeout=15,
    )
    if accounts_result.returncode != 0:
        return {}
    try:
        accounts = json.loads(accounts_result.stdout or "[]")
    except ValueError:
        return {}
    if not isinstance(accounts, list):
        return {}

    billing_map: dict[str, bool] = {}
    for account in accounts:
        # account["name"] shape: "billingAccounts/<id>". The --billing-account
        # flag wants just the <id> portion.
        full_name = str(account.get("name", ""))
        if not full_name.startswith("billingAccounts/"):
            continue
        account_id = full_name[len("billingAccounts/"):]
        if not account_id:
            continue

        projects_result = run_cmd(
            ["gcloud", "beta", "billing", "projects", "list",
             f"--billing-account={account_id}", "--format=json"],
            timeout=20,
        )
        if projects_result.returncode != 0:
            continue  # Skip this account; other accounts may still succeed
        try:
            entries = json.loads(projects_result.stdout or "[]")
        except ValueError:
            continue
        if not isinstance(entries, list):
            continue
        for entry in entries:
            # entry shape: {"name": "projects/<id>/billingInfo",
            #               "projectId": "<id>",
            #               "billingAccountName": "billingAccounts/<id>",
            #               "billingEnabled": true}
            pid = str(entry.get("projectId", ""))
            if pid:
                billing_map[pid] = bool(entry.get("billingEnabled", False))
    return billing_map


def list_active_projects() -> list[ProjectListing]:
    """Return the ACTIVE GCP projects the operator can see, with billing.

    Implementation:
      1. `gcloud projects list --filter=lifecycleState:ACTIVE --format=json`
         enumerates ACTIVE projects. Empty list on any failure.
      2. `_fetch_billing_map()` builds a {projectId: billingEnabled} map
         by iterating visible OPEN billing accounts. Each unmapped project
         gets `billing_enabled=None` (rendered as "unknown" in the picker).
      3. Sort case-insensitively by project_id.

    Never raises. Returns [] on any gcloud failure so the caller can fall
    back to the text-prompt flow gracefully.
    """
    list_result = run_cmd(
        ["gcloud", "projects", "list",
         "--filter=lifecycleState:ACTIVE",
         "--format=json"],
        timeout=20,
    )
    if list_result.returncode != 0:
        return []
    try:
        raw = json.loads(list_result.stdout or "[]")
    except ValueError:
        return []
    if not isinstance(raw, list):
        return []

    listings = [
        ProjectListing(
            project_id=str(p.get("projectId", "")),
            # gcloud projects list v1 returns the display name in `name`.
            # v3 (Cloud Resource Manager) flips the semantics: `name` becomes
            # the resource path (e.g., "projects/123456789") and `displayName`
            # holds the human label. Skip `name` if it has the v3 resource-
            # path shape; otherwise use it. Tolerant fallback to `displayName`.
            # (iter-1 finding I7 + iter-2 nit #2.)
            name=(
                lambda nm, dn: (
                    dn or "" if (nm or "").startswith("projects/")
                    else (nm or dn or "")
                )
            )(p.get("name"), p.get("displayName")),
            billing_enabled=None,
        )
        for p in raw
        if p.get("projectId")  # Filter empty projectIds defensively
    ]

    billing_map = _fetch_billing_map()
    for listing in listings:
        if listing.project_id in billing_map:
            listing.billing_enabled = billing_map[listing.project_id]

    listings.sort(key=lambda lp: lp.project_id.lower())
    return listings
```

### 5.2 `scripts/wizard.py` — modify Q1 block (L71-82)

Replace the existing 12-line Q1 block with:

```python
        # --- Q1: GCP Project ID ---
        print_header("1. GCP Project ID")
        detected = get_project_id()
        default_project = defaults.get("gcp_project_id") or detected or ""

        # PR-V: Try to list visible ACTIVE projects and offer a numbered
        # picker. On empty list (no permission / gcloud failure), fall
        # through to the original text-prompt path so the wizard remains
        # usable on restricted-IAM hosts.
        # iter-2 nit #1: print a progress hint because the per-account
        # billing fetch can take ~20s × N_accounts in worst case (e.g.,
        # 8+ billing accounts → 160s+). Operators need a "this is alive"
        # signal during the gcloud round-trips.
        console.print("[dim]      Fetching GCP project list...[/dim]")
        listings = list_active_projects()
        project_id = ""
        if listings:
            options = []
            default_idx = 1
            for i, lp in enumerate(listings, 1):
                if lp.billing_enabled is True:
                    billing_str = "billing: yes"
                elif lp.billing_enabled is False:
                    billing_str = "billing: no"
                else:
                    billing_str = "billing: unknown"
                # `*` marker reflects the EFFECTIVE numeric default (see
                # iter-1 finding I5: marker must align with default_idx).
                marker = " *" if lp.project_id == default_project else ""
                options.append({
                    "id": lp.project_id,
                    "name": f"{lp.project_id}{marker}",
                    "note": f"{lp.name} ({billing_str})" if lp.name else billing_str,
                })
                if lp.project_id == default_project:
                    default_idx = i
            options.append({
                "id": "__manual__",
                "name": "Enter manually...",
                "note": "type a project ID not in the list above",
            })
            choice_idx = prompt_choice(
                "Select your GCP project",
                options,
                default=default_idx,
            )
            # Read back the selected option's id so a renamed sentinel
            # (e.g. `__manual` vs `__manual__`) is observable in tests
            # (iter-1 finding I3 → mutant #6 catchable).
            selected_id = options[choice_idx - 1]["id"]
            if selected_id != "__manual__":
                project_id = selected_id

        # Manual entry fallback: empty list OR operator chose "Enter manually..."
        if not project_id:
            if detected:
                console.print(f"      [dim]Detected: {detected}[/dim]")
            project_id = prompt_text(
                "Enter your GCP project ID",
                default=default_project,
                validate_fn=_validate_project_id,
            )
        config["gcp_project_id"] = project_id
```

Notes:
- New `list_active_projects` import is added at top of file.
- `prompt_choice` is already imported (L16).
- Picker-selected `project_id` skips `_validate_project_id` because it came from gcloud (already valid). Manual-entry path retains validation.
- Default selection priority (iter-1 finding I5): `defaults["gcp_project_id"]` > `detected` > first in list. The `*` visual marker AND the numeric default both anchor on this resolved `default_project` value — no visual/numeric mismatch.
- The sentinel `__manual__` is read back from the selected option (`options[choice_idx - 1]["id"]`) so renaming the sentinel surfaces as a behavioral change in tests, not as silent dead code (iter-1 finding I3).

### 5.3 Wire-field checklist

Empirically verified against gcloud 472+ on dogfood host 2026-05-13.

| Field | Source | Required | Notes |
|---|---|---|---|
| `ProjectListing.project_id` | `gcloud projects list --filter=lifecycleState:ACTIVE --format=json` → JSON top-level `projectId` field | yes | Stable in gcloud 200+; v1 API. Sample observed shape: `{"createTime", "lifecycleState", "name", "projectId", "projectNumber"}` |
| `ProjectListing.name` | Same call → `name` field (display name). Tolerant fallback to `displayName` for future v3 schema | no | Empty string is acceptable; display falls back to bare project_id |
| `ProjectListing.billing_enabled` | `gcloud beta billing projects list --billing-account=<id> --format=json` → `billingEnabled` field. Iterated per OPEN billing account from `gcloud billing accounts list` | no | Tri-state True/False/None |
| Billing account ID | `gcloud billing accounts list --format=json --filter=open=true` → `name` field, stripped of `billingAccounts/` prefix | yes (for billing column) | Sample: `"name": "billingAccounts/017566-989E30-C5AFD4"` → account_id `017566-989E30-C5AFD4` |

### 5.4 Producer→consumer trace

| Producer change | Consumer file | Consumer read path | Verification |
|---|---|---|---|
| `list_active_projects()` in gcp.py | `scripts/wizard.py` Q1 block | direct call at wizard runtime | unit test: stub `run_cmd` to return canned JSON, assert ProjectListing shape |
| `ProjectListing` dataclass | wizard.py picker options builder | iteration | unit test asserts dataclass fields |
| Picker selection (choice_idx ≤ len(listings)) | `config["gcp_project_id"]` | direct assignment | unit test asserts selected projectId equals config value |
| Manual entry fallback | existing `prompt_text` path | unchanged behavior | regression test asserts existing flow still works |

No dead code. No new placeholders. Strictly additive to wizard flow.

## 6. Copy/decision rationale

- **Numbered choice vs arrow-key UI:** Locked decision (Q3 CPO consultation 2026-05-13). Number entry has zero new dependencies and matches the rest of the wizard (`prompt_choice` already used for region, GKE type, etc).
- **Billing in the list (Q2 = b):** One extra batch gcloud call. Surfaces "billing disabled" preemptively so operator does not waste 30s into `terraform_apply` before discovering the issue.
- **Manual-entry escape hatch (Q4 = a):** Always present as last option. Covers (i) cross-org projects, (ii) just-created projects not yet visible to gcloud caching, (iii) operator's gcloud account changed mid-session.
- **Wizard-only scope (Q1 = a):** No `--project` global option. Apply/destroy/status/verify remain config.yaml-bound. The directory-per-project pattern (and `init --reconfigure` for changing within a directory) covers existing workflows.
- **Empty list = silent fallback (no error):** Restricted-IAM operators (e.g., service accounts with `iam.serviceAccounts.actAs` only) cannot run `projects list`. Failing the wizard here would block them entirely. Falling back to text-prompt preserves the v0 UX.
- **Tri-state billing (`Optional[bool]`):** A False positive ("billing: no" when actually yes) is much worse than "unknown" — the operator would skip the project unnecessarily. Tri-state keeps the picker honest.
- **No fzf / questionary dep:** Pinning the dependency surface; the install repo is intentionally minimal-dep.

## 7. Verification plan

### 7.1 Static checks (pre-commit)

1. `python -m pytest tests/ -q` — full suite green (expect 771 + 22 new = ~793).
2. `bash scripts/dev/check-plan-sensitive.sh docs/plans/2026-05-13-pr-v-project-picker-design.md` — sensitive scan PASS.
3. `grep -n list_active_projects scripts/` — exactly 2 hits (def in gcp.py + use in wizard.py).
4. `grep -n ProjectListing scripts/ tests/` — def in gcp.py + tests.

### 7.2 Test enumeration (new file `tests/test_pr_v_project_picker.py`)

| Class | Tests | Purpose |
|---|---|---|
| `TestListActiveProjectsHappyPath` | 3 | gcloud projects list + per-account billing list both return valid JSON → ProjectListing list sorted alphabetically; billing_enabled=True/False/None populated per source; name field included when present |
| `TestListActiveProjectsErrorHandling` | 5 | projects list returncode!=0 → empty list; malformed projects JSON → empty list; billing accounts list returncode!=0 → all billing_enabled=None; per-account projects list returncode!=0 → that account skipped, others succeed; gcloud not authenticated (rc=0 + empty `[]`) → empty list (iter-1 finding I2) |
| `TestListActiveProjectsEdgeCases` | 4 | empty projects list → []; entry with empty projectId filtered out; sorting is case-insensitive; v3-schema `displayName` fallback when `name` absent (iter-1 finding I7) |
| `TestBillingMapMultiAccount` | 2 | Two billing accounts with overlapping/disjoint projects merged correctly (**last-write-wins contract pinned**: when same project appears under two accounts, iteration order determines result; test inputs use distinct projects per account to avoid ambiguity; iter-2 nit #3); account whose name doesn't start with `billingAccounts/` is skipped |
| `TestWizardPickerIntegration` | 4 | prompt_choice called with N+1 options (N projects + manual); selection ≤ N returns projectId; selection N+1 falls through to prompt_text; **`prompt_choice` is called with `default=<expected_idx>` matching the default_project resolution priority (iter-1 finding I4, mutant #8 catcher)** |
| `TestWizardPickerSentinelReadback` | 1 | Renaming `__manual__` sentinel breaks behavior (iter-1 finding I3, mutant #6 catcher): asserts selected_id readback path correctly identifies the manual option |
| `TestWizardPickerFallback` | 1 | empty listings → prompt_text called directly (no prompt_choice) |
| `TestWizardPickerBillingDisplay` | 2 | billing_enabled=True renders "billing: yes" in option note; billing_enabled=None renders "billing: unknown" (NOT "billing: no") (iter-1 finding I9, mutant #9 catcher) |

Total: 22 new tests (was 14 in v1; +5 for iter-1 hardening + 3 for sentinel/default/billing-display).

### 7.3 Mutant-injection harness

12 mutants, file-backup revert. Gate ≥10/12.

| # | Mutation | Expected catcher |
|---|---|---|
| 1 | swap `lifecycleState:ACTIVE` filter to `lifecycleState:DELETED` | TestListActiveProjectsHappyPath (argv exact-match) |
| 2 | change sort key from `project_id.lower()` to `project_id` (case-sensitive) | TestListActiveProjectsEdgeCases |
| 3 | flip `billing_enabled` default to False on missing batch lookup | TestListActiveProjectsErrorHandling |
| 4 | drop `if p.get("projectId")` filter (include empty projectId entries) | TestListActiveProjectsEdgeCases |
| 5 | change `bool(entry.get("billingEnabled", False))` to `entry.get("billingEnabled", True)` | TestListActiveProjectsHappyPath |
| 6 | rename `__manual__` sentinel to `__manual` | TestWizardPickerSentinelReadback |
| 7 | drop the empty-listings fallback (always run picker → IndexError) | TestWizardPickerFallback |
| 8 | change picker `default=default_idx` to `default=len(listings)+1` | TestWizardPickerIntegration (explicit default= kwarg assertion) |
| 9 | render billing_enabled=None as "billing: no" instead of "billing: unknown" | TestWizardPickerBillingDisplay |
| 10 | strip the `billingAccounts/` prefix incorrectly (e.g. wrong slice index) | TestBillingMapMultiAccount |
| 11 | drop `--filter=open=true` from accounts list (closed accounts included) | TestListActiveProjectsHappyPath (argv exact-match) |
| 12 | drop tolerant `displayName` fallback | TestListActiveProjectsEdgeCases |

Acceptance gate: ≥10 caught.

### 7.4 Dogfood-readiness check (post-merge)

1. `voipbin-install init` on a host with 3+ ACTIVE GCP projects → numbered list appears with billing status.
2. Selecting by number → `config.yaml` shows the chosen `gcp_project_id`.
3. Selecting the "Enter manually..." last option → free-form prompt appears, accepts typed ID.
4. On a host with restricted IAM (no `projects.list` permission) → no picker, falls through to text prompt (regression check).
5. On a host with `gcloud beta` component not installed → projects list shows but billing column is all "unknown" (graceful degradation).

## 8. Rollout / risk

| # | Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|---|
| R1 | `gcloud beta` component not installed OR operator has zero visible OPEN billing accounts | Med | All billing columns display "unknown" | Documented; non-blocking; per-account loop silently skips. Operator can run `gcloud components install beta` or proceed with manual billing verification. Iter-1 finding I1 resolved the schema: billing list is `--billing-account=<id>` scoped, NOT a global call. Iter-1 I6 explicitly covers the zero-accounts case |
| R2 | Project list is huge (operator has 100+ projects across orgs) | Low (dogfood scale) | UX degradation — long scroll | Manual-entry fallback still works; future PR can add `--filter` |
| R3 | gcloud projects list returns ACTIVE projects the operator cannot actually use (sees but not deploy-to) | Med | Wizard picks but `check_gcp_project` later fails | Existing `check_gcp_project` catches this; just a redirect, no data loss |
| R4 | Billing batch call requires Cloud Billing API enabled at the *operator's* level (not the project) | Low | "unknown" billing across the board | Same as R1 — graceful degradation, no abort |
| R5 | Race: operator created a new project moments before running `init` → not in cache | Low | New project missing from picker | Manual-entry fallback |
| R6 | JSON parse drift if gcloud changes output schema | Low | Empty list (no raise) | Tolerant parsing (`json.loads` in try/except); fallback to text prompt |
| R7 | `_validate_project_id` is skipped for picker-selected values | Expected | None — gcloud-returned IDs are already valid format | Documented in §5.2 notes |
| R8 | Operator with many (8+) billing accounts incurs sequential 20s gcloud calls during init | Low | Wizard appears to hang for 160s+ | iter-2 nit #1: print "Fetching GCP project list..." hint before the call (§5.2). Future PR could parallelize per-account billing fetch. Acceptable for dogfood scale |

## 9. Open questions (for iter-3 reviewer, if any)

All iter-1 (9) and iter-2 (4 nits) issues resolved in v3. Empirically verified against gcloud 472+ on dogfood host. No remaining open questions.

## 10. Approval status

- [x] Draft v1 written 2026-05-13
- [x] Iter-1 design review completed (9 REAL findings including 1 CRITICAL)
- [x] v2 fixes applied 2026-05-13
- [x] Iter-2 design review completed (APPROVED with 4 nits)
- [x] v3 fixes applied 2026-05-13
- [ ] APPROVED (min-2 satisfied; ready for implementation)

---

## Decisions locked 2026-05-13 (CPO consultation)

- Scope: `init` wizard Q1 only. No global `--project` CLI option, no separate `switch-project` command.
- Project source: `gcloud projects list` + `gcloud beta billing projects list` (per-account loop).
- Selection UI: numbered list via existing `prompt_choice` helper. No new dependencies.
- Fallback: always include a final "Enter manually..." option AND silently fall through to text prompt on empty list.

## 11. Iter-1 review response summary (v1 → v2)

| Iter-1 issue | Resolved in v2 | Section |
|---|---|---|
| I1 CRITICAL. `gcloud beta billing projects list` requires `--billing-account` | Replaced single-call with two-step accounts-first loop (§5.1 `_fetch_billing_map`); empirically verified | §5.1, §5.3 |
| I2. rc=0 + empty `[]` for unauthenticated gcloud not explicit | Added explicit test row | §7.2 TestListActiveProjectsErrorHandling |
| I3. `__manual__` sentinel rename not behaviorally catchable | Added `selected_id = options[choice_idx - 1]["id"]` readback path | §5.2, §7.2 TestWizardPickerSentinelReadback |
| I4. Mutant #8 (default=) not catchable without arg inspection | Explicit `default=` kwarg assertion added to TestWizardPickerIntegration | §7.2, §7.3 mutant #8 |
| I5. `*` marker anchored on `detected` (not effective default_project) | Marker now uses `default_project`; precedence documented | §5.2 |
| I6. R1 omitted zero-billing-accounts case | R1 expanded | §8 R1 |
| I7. `name` field v3 schema drift | Tolerant `displayName` fallback | §5.1, mutant #12 |
| I8. Redundant `json.JSONDecodeError` in except tuples | Reduced to `ValueError` only | §5.1 |
| I9. `billing_enabled=None` rendered same as False | TestWizardPickerBillingDisplay distinguishes "unknown" from "no" | §7.2, mutant #9 |

All 9 iter-1 findings addressed.

## 12. Iter-2 review response summary (v2 → v3)

Iter-2 APPROVED the design. 4 non-blocking nits were folded into v3 anyway (no silent rejection per skill discipline):

| Iter-2 nit | Resolved in v3 | Section |
|---|---|---|
| #1. No progress hint during multi-account billing fetch | Added `console.print("[dim]Fetching GCP project list...[/dim]")` before `list_active_projects()` | §5.2 wizard code block, §8 R8 |
| #2. `name` fallback order brittle if gcloud migrates to Resource Manager v3 default | v3-shape detection: if `name.startswith("projects/")`, skip to `displayName` | §5.1 listings comprehension |
| #3. TestBillingMapMultiAccount duplicate-project contract undocumented | Test description pins "last-write-wins; test inputs use distinct projects per account to avoid ambiguity" | §7.2 TestBillingMapMultiAccount |
| #4. Latency risk for 8+ billing accounts not in risk register | Added R8 to risk register | §8 R8 |

All 4 iter-2 nits resolved.
