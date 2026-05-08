# First-Time User Hardening Design

**Date:** 2026-05-08  
**Branch:** NOJIRA-First-time-user-hardening  
**Status:** Final (rev 10 — post-10-iteration review loop)

## Problem Statement

VoIPBin users are first-time installers who know nothing about GCP, Terraform, Ansible, or Kubernetes. The current installer fails silently or with cryptic error messages at multiple points in the pipeline. The most recent reported failure:

```
Error: Failed to open state file at gs://...: auth: "invalid_grant"
"reauth related error (invalid_rapt)"
```

This error — GCP Application Default Credentials (ADC) expiry — is invisible to the user because:
1. The preflight check validates `gcloud auth list` (user accounts) but not ADC
2. The `apply` command has zero pre-flight auth checks
3. On failure, the pipeline shows a generic "Fix the issue and re-run" box

## Goals

- Guide first-time users to the correct state automatically wherever possible
- Auto-detect common failure causes before and after they occur
- Auto-fix what can be fixed without user expertise (ADC setup/refresh, managed-package tool install)
- Show exact, copy-pasteable fix commands for what cannot be auto-executed safely
- Reduce average time-to-successful-deploy for a first-time user from "unknown/blocking" to "guided completion"

## Non-Goals

- Modifying Terraform/Ansible/Kubernetes configuration files
- Handling every possible GCP error (focus on the 90% cases)
- Supporting Windows (Linux/macOS only)

## Gap Analysis

Full audit of all modules against the first-time user persona:

| Module | Gap | Severity |
|--------|-----|----------|
| `preflight.py` | Validates `gcloud auth list` but not ADC (what Terraform uses) | Critical |
| `commands/apply.py` | Zero auth/billing/API checks before pipeline starts; generic failure box | Critical |
| `commands/destroy.py` | Same ADC gap — terraform destroy fails identically | High |
| `pipeline.py` | Generic "Stage failed. Pipeline halted." with no post-failure diagnosis | High |
| `terraform.py` | `capture=False` streams to terminal — error text unavailable for diagnosis | High |
| `commands/init.py` | Shows install URLs only — no OS-aware commands, no auto-install offer | Medium |
| `cli.py` | `--stage` missing `terraform_reconcile` (regression: added to APPLY_STAGES but not CLI) | Medium |
| `gcp.py` | `create_service_account()` silently continues on non-idempotent failures | Medium |
| `secretmgr.py` | `decrypt_with_sops()` returns `None` with no error or file path logged | Medium |
| `k8s.py` | `k8s_get_credentials()` shows raw stderr; no diagnosis of auth vs IAM vs missing cluster | Low |
| `ansible_runner.py` | No hint when SSH/IAP tunnel fails (VMs still booting is the common cause) | Low |

## Architecture

Three layers, each independently valuable:

```
Layer 1: Pre-flight hardening
  init:  ADC setup/check + OS-aware tool install
  apply: Pre-apply health check (ADC, billing, APIs) before pipeline

Layer 2: Post-failure diagnosis
  pipeline: After any stage fails → probe GCP state → print exact fix
  (single call site: pipeline.py only — not duplicated in cmd_apply)

Layer 3: Surface-level fixes
  cli.py:       add missing --stage option (terraform_reconcile regression)
  gcp.py:       surface SA creation errors while preserving idempotent role binding
  secretmgr.py: log SOPS decrypt errors with file path
  utils.py:     fix _SAFE_ID_RE regex to allow [ and ] for indexed Terraform resource addresses
```

**Import convention**: All imports of `diagnosis.py` are top-level in each caller
file (never deferred inside functions or conditionals). `diagnosis.py` imports from
`scripts.utils`, `scripts.display`, `scripts.config`, and `scripts.gcp`
(for `check_quotas()`). No import cycles result from including `scripts.gcp`.

## Detailed Design

### New Module: `scripts/diagnosis.py`

**Path**: `/home/pchero/gitvoipbin/install/scripts/diagnosis.py` — alongside the existing
`preflight.py`, `gcp.py`, etc. Imported at the top level of every caller (`apply.py`,
`destroy.py`, `pipeline.py`, `preflight.py`, `commands/init.py`).

No import cycles: `diagnosis.py` imports from `scripts.utils`, `scripts.display`,
`scripts.config`, and `scripts.gcp` (for `check_quotas()`, `check_required_apis()`,
and `check_billing_tristate()`). `diagnosis.py` does NOT import from `scripts.preflight`.
`preflight.py` imports from `diagnosis.py` (one-way: `check_application_default_credentials`,
`get_os_install_hint`). Moving `check_billing_tristate` to `gcp.py` breaks the
otherwise-circular `diagnosis → preflight → diagnosis` import chain.

Four responsibilities:

#### 1. ADC Check and Setup/Refresh

User-account credentials (`gcloud auth list`) and Application Default Credentials
(`gcloud auth application-default`) are independent credential sets. The existing
`check_gcp_auth()` validates the former; `check_application_default_credentials()`
validates the latter — the one that Terraform, SOPS, and the GCS backend actually use.

```
check_application_default_credentials() -> (is_valid: bool, account: str | None)
  Runs: gcloud auth application-default print-access-token
  If exit code != 0: return (False, None) immediately — do NOT run a second
    subprocess for the account name when ADC is invalid.
  If exit code == 0: run gcloud config get-value account to get the account string.
    Returns None if that command fails, returns empty, or returns the literal "(unset)".
    Never returns the raw "(unset)" string to callers.
    When exit code == 0 but account is empty or "(unset)": returns (True, None).
    `is_valid=True` reflects token validity only — not the completeness of account metadata.

offer_adc_setup(auto_accept: bool = False) -> bool
  **Guard 1 (unconditional, runs before any prompt or auto-accept logic)**:
    If shutil.which("gcloud") is None:
      print: "gcloud CLI is not installed. Run: voipbin-install init"
      return False
    (This guard fires before auto_accept is consulted — even CI callers cannot bypass it.)

  Detects whether ADC is absent or expired by checking for the ADC file.
  Default path: ~/.config/gcloud/application_default_credentials.json (Linux/macOS)
  When the CLOUDSDK_CONFIG environment variable is set, the path is:
    $CLOUDSDK_CONFIG/application_default_credentials.json
  Use os.environ.get("CLOUDSDK_CONFIG") to check; fall back to ~/.config/gcloud if unset.
  The ADC file path is used solely to determine first-time vs refresh framing — NOT to
  validate whether credentials are still valid. Validity is always determined by
  check_application_default_credentials() (which invokes gcloud auth application-default
  print-access-token). A file that exists but holds an expired token triggers
  "refresh" framing; a missing file triggers "first-time" framing.
    If file absent → first-time framing:
      "Application Default Credentials are not yet configured.
       These credentials allow Terraform to access GCP on your behalf."
    If file exists but token invalid → refresh framing:
      "Your Application Default Credentials have expired."
  Always appends: "A browser window will open for you to sign in to GCP."
  Offers: "Set up credentials now? [Y/n]"  (first-time)
          "Refresh credentials now? [Y/n]"  (expired)
  If user says "n":
    Prints: print_fix("How to fix", ["gcloud auth application-default login"])
    Returns False
  If yes: runs gcloud auth application-default login
          timeout=300s (prevents indefinite hang if browser cannot open)
          capture=False (user must interact with the OAuth flow in the terminal)
  If login times out or returns non-zero:
    Prints: print_fix("How to fix", ["gcloud auth application-default login"])
    Prints: "Then re-run: voipbin-install <command>"
    Returns False
  Re-checks with check_application_default_credentials()
  Returns True if now valid

  auto_accept=True: bypasses the [Y/n] prompt and directly invokes
    gcloud auth application-default login (same subprocess call as the "yes" path,
    with the same timeout=300s and capture=False). The OAuth browser flow is still
    interactive — the user must complete sign-in in the browser. auto_accept only
    skips the "Set up/Refresh now? [Y/n]" question; it does NOT make the function
    safe for headless CI use. Pre-configure ADC before running apply/destroy in CI.
```

#### 2. Pre-Apply Health Check

Called from `cmd_apply()` before `run_pipeline()`. Target: ≤ 10 seconds total
on a normal connection. Ordered cheapest-first to fail fast.

The project ID is read as `config.get("gcp_project_id")` — the consistent key
used throughout the codebase (seen in `init.py`, `apply.py`, `gcp.py`).

```
run_pre_apply_checks(config: InstallerConfig, auto_approve: bool = False, only_stage: str | None = None) -> bool
  project_id = config.get("gcp_project_id")

  Check 1 (ADC, ~2s):      check_application_default_credentials()
    → if not valid: offer_adc_setup(auto_accept=auto_approve)
    → if still not valid: return False  (offer_adc_setup printed guidance)

  Check 2 (project, ~3s):  gcloud projects describe {project_id}
    → if fails: print "Check project ID and IAM permissions", return False

  Check 3 (billing, ~3s):  check_billing_tristate(project_id) from scripts.gcp
    → "enabled"  → continue
    → "disabled" → print console link, return False
    → "unknown"  → probe failed (auth, network, etc.) → skip billing hint, continue to check 4

  Check 4 (key APIs, ~2s): missing = check_required_apis(project_id)  # defined in gcp.py
    → if missing: print "Enable APIs:\n  gcloud services enable {' '.join(missing)} --project {project_id}"
                  return False

  Returns True only if all checks pass

  **gcloud prerequisite assumption**: `run_pre_apply_checks()` assumes `gcloud`
  is already on PATH — enforced by the `init` prerequisite check. If gcloud is
  somehow absent (user skipped `init`), check_application_default_credentials()
  returns is_valid=False with a "command not found" exit code. In this case,
  `offer_adc_setup()` must detect the underlying cause before showing ADC guidance:
  if `shutil.which("gcloud")` returns None, print:
    "gcloud CLI is not installed. Run: voipbin-install init"
  and return False. This prevents the misleading instruction
  "run gcloud auth application-default login" when gcloud itself is absent.
```

**Timestamp-based skip for checks 2–4**: The function signature is:
`run_pre_apply_checks(config, auto_approve=False, only_stage=None) -> bool`

Skip checks 2–4 if ALL of the following are true:
1. The installer state file exists
2. `deployment_state` is not `"failed"`
3. State `timestamp` is within 24 hours
4. `only_stage` is `None` (full pipeline resume, not a single-stage targeted run)

When `only_stage` is set (e.g. `--stage k8s_apply`), the timestamp skip is disabled.
This is intentional: a user targeting a single stage is explicitly re-running
something that failed, and billing/API checks may reveal the cause. The timestamp
skip is designed for full-pipeline resume from a checkpoint, not for targeted
single-stage investigation.

The `"failed"` guard is critical: a user whose previous `apply` failed must not
skip billing and API checks, as those may be the root cause.

When `deployment_state == "applying"` (process crashed mid-run), checks 2–4 are
skipped if the timestamp is fresh and `only_stage` is None. This is acceptable.

If `timestamp` is absent from the state file (e.g., state files created before
timestamp tracking was added), treat it as stale — do NOT skip checks 2–4.
Implementation: `ts = state.get("timestamp"); if not ts: treat as stale`.

**State YAML schema** (`run_pre_apply_checks` is read-only — it never writes to the state file):

| Key | Type | Written by | Meaning |
|-----|------|-----------|---------|
| `timestamp` | ISO-8601 str | `pipeline.py` | Set each time a stage status is updated (stage start, pass, or fail). `run_pre_apply_checks` compares this against `datetime.now(UTC)` to determine if checks 2–4 may be skipped. |
| `deployment_state` | str | `pipeline.py` | Overall state: `"applying"`, `"deployed"`, `"failed"` (and `"planned"`, `"destroying"`, `"destroy_failed"`, `"destroyed"` for other flows). `run_pre_apply_checks` only checks `!= "failed"` — any non-`"failed"` state allows the 24h skip. |
| `stages` | dict | `pipeline.py` | Per-stage status. Not read by `run_pre_apply_checks`. |

`pipeline.py` already writes `timestamp` on every state save (existing behavior). No new
key is introduced by this feature. `run_pre_apply_checks` only reads these keys; all
writes to the state file remain exclusively in `pipeline.py`.

Same check set (without the timestamp skip) runs in `cmd_destroy()`. Destroy always
passes `only_stage=None` — destroy is never a partial-stage operation.

`cmd_apply()` must thread `only_stage` through from the `--stage` CLI option to
`run_pre_apply_checks()`. The `auto_approve` parameter is threaded through from
`cmd_apply()`/`cmd_destroy()` to ensure `offer_adc_setup()` can skip the prompt.

#### 3. Post-Failure Diagnosis

**Single call site: `pipeline.py` only.** `cmd_apply()` does not call diagnosis
separately, preventing duplicate output.

`pipeline.py` imports `diagnosis` at the top level (with all other existing imports
at the top of the file, not inside any function or condition).

All hints returned by `diagnose_stage_failure()` are rendered in a **single**
`print_fix("Likely causes", hints)` call (using the `list[str]` signature). One
panel shows all detected issues together, avoiding multiple identically-titled panels.

```
diagnose_stage_failure(config: InstallerConfig, stage: str) -> list[str]
  project_id   = config.get("gcp_project_id")
  region       = config.get("region")
  zone         = config.get("zone")
  cluster_name = "voipbin-gke-cluster"
  # "voipbin" is the fixed env prefix hardcoded in gke.tf (var.env defaults to "voipbin").
  # The "env" key is NOT in the installer config schema and is not collected by the wizard.
  # Do NOT use config.get("env", ...) — hardcode the cluster name to match the Terraform resource.
  Probes GCP state after failure. Does not parse terraform/ansible output.
  Returns list of "Likely cause: X → Fix: Y" strings.

  **ADC-first guard rule**: Always run check_application_default_credentials() first.
  If ADC is invalid: add the ADC hint and RETURN IMMEDIATELY — do NOT probe billing,
  quotas, or any other GCP resource. Those probes would themselves fail with
  PERMISSION_DENIED and generate false hints (e.g., "billing may be disabled" when
  the real cause is just ADC expiry). The user's only actionable step when ADC is
  invalid is to fix ADC first.

  Billing check: check_billing_tristate(project_id) from scripts.gcp
    "enabled"  → no hint
    "disabled" → add billing hint
    "unknown"  → probe failed: skip billing hint
  (Same function as run_pre_apply_checks — single implementation, no duplication)

  ADC valid → continue with billing and stage-specific checks:

  Stage-specific checks:
    terraform_init:
      - Billing (always, as above)
      - GCS bucket accessible: gcloud storage ls gs://{project_id}-voipbin-tf-state --project={project_id}
        (uses gcloud storage, not gsutil — gsutil may not be present on newer gcloud SDK installs)
        If bucket missing or access denied: "State bucket does not exist — create it first:
          gcloud storage buckets create gs://{project_id}-voipbin-tf-state --project={project_id}"
      - Key APIs: missing = check_required_apis(project_id)
        If non-empty: "Enable APIs: gcloud services enable {' '.join(missing)} --project {project_id}"

    terraform_reconcile / terraform_apply:
      - Billing (always, as above)
      - GCS bucket accessible (same as above)
      - Key APIs: missing = check_required_apis(project_id); hint if non-empty (same as above)
      - Quota check: calls check_quotas(project_id, region) from scripts.gcp
        For each QuotaResult q where q.ok is False, append hint:
          "Likely cause: insufficient {q.metric} quota ({q.available:.0f} available,
           {q.required:.0f} required) → Fix:
           https://console.cloud.google.com/iam-admin/quotas?project={project_id}"
        (Do NOT call display_quota_results() — it prints directly instead of returning strings)

    ansible_run:
      - VM instance status: gcloud compute instances list --project={project_id}
          --filter="labels.env=voipbin" --format="value(name,status)"
        VM labels are set by Terraform as labels.env = var.env on all instances
        (kamailio.tf line 31, rtpengine.tf equivalent). The tag "voipbin" does NOT
        exist on any VM — the correct filter is the label, not the tag.
        The env prefix is hardcoded to "voipbin" (same reason as cluster_name above).
        Parse output line by line; each line is "name\tstatus" (tab-separated).
        Conditions and hints:
          - Zero lines returned (no VMs with labels.env=voipbin):
            "VMs not yet created — terraform_apply stage may not have completed.
             Try re-running from terraform_apply: voipbin-install apply --stage terraform_apply"
          - At least one VM exists but none have status "RUNNING":
            "VMs may still be booting — wait 2 minutes and re-run: voipbin-install apply"
          - At least one VM has status "TERMINATED":
            "One or more VMs stopped unexpectedly — check GCP Console:
             https://console.cloud.google.com/compute/instances?project={project_id}"
        Note: the "no VMs" and "TERMINATED" hints may both fire if some VMs are
        terminated and others are missing; add both as separate hint strings.

    k8s_apply:
      - GKE cluster status:
          gcloud container clusters describe {cluster_name} --project={project_id} --zone={zone}
          (zone is required — the cluster is zonal, provisioned in var.zone per gke.tf)
          The installer only supports zonal GKE clusters. Regional clusters (which would
          require --location instead of --zone) are not in scope for this installer and
          are not provisioned by the Terraform configuration. Using --zone is correct
          and intentional; --location is not used.
          If PROVISIONING: "GKE cluster still provisioning — wait 5 minutes
            and re-run: voipbin-install apply"
          If not found (exit non-zero): "Cluster not found — terraform_apply stage
            may not have completed"

  Returns list of strings, empty if no specific diagnosis could be made.
```

#### 4. OS-Aware Install Hints

```
_detect_os() -> str   # internal helper
  platform.system() → "Darwin" → "macos"
  /etc/os-release: read file, split each line on the first "=", take the value part,
    strip surrounding double-quotes (values are commonly quoted: ID="ubuntu").
    ID field → "ubuntu"/"debian" → "debian"
             → "rhel"/"centos"/"rocky"/"almalinux" → "rhel"
             → "fedora" → "fedora"
             → "arch" → "arch"
  If /etc/os-release is absent, cannot be read, or has no `ID=` field → fallback → "linux"

get_os_install_hint(tool: str) -> tuple[list[str], bool]
  Returns (install_steps, can_auto_run)
  install_steps: list of command strings (one entry per command step)
  can_auto_run=True only for package-manager commands (apt, brew, yum, pip3,
               gcloud components) where the package manager handles integrity
  can_auto_run=False for curl-pipe-bash, multi-step system-repo setups that
               require gpg key import, and any path without package-manager
               integrity guarantees

offer_tool_install(tool: str) -> bool
  steps, can_auto_run = get_os_install_hint(tool)
  Always prints all steps in a print_fix() block (copy-pasteable)
  If can_auto_run:
    Offers "Install now? [Y/n]" and runs each step in sequence if confirmed
    After running: calls check_tool_exists(tool) (PATH check)
    If tool still not on PATH: prints:
      "Installation may require restarting your shell.
       Open a new terminal, then re-run: voipbin-install init"
    Returns True only if check_tool_exists() now passes
  If not can_auto_run:
    Prints: "Run the commands above in your terminal, then re-run:
              voipbin-install init"
    Returns False
  In both cases the caller (cmd_init) calls sys.exit(1) on False — no additional
  message is printed by the caller since offer_tool_install() has already guided
  the user.
```

**Auto-run policy (which install methods are safe to run on user consent):**

| Tool | Ubuntu/Debian | macOS | RHEL | Auto-run? |
|------|--------------|-------|------|-----------|
| gcloud | `curl https://sdk.cloud.google.com \| bash` | `brew install --cask google-cloud-sdk` | Multi-step RPM repo setup (see below) | Debian: No. macOS brew: Yes. RHEL: No |
| terraform | Multi-step HashiCorp apt repo (gpg key + apt source + install) | `brew tap hashicorp/tap && brew install hashicorp/tap/terraform` | Multi-step HashiCorp yum repo | macOS brew: Yes. Others: No (requires gpg key import) |
| ansible | `pip3 install ansible` | `pip3 install ansible` | `pip3 install ansible` | Yes (all platforms) |
| kubectl | `gcloud components install kubectl` | `brew install kubectl` | `gcloud components install kubectl` | Yes (all platforms) |
| sops | Display-only (see below) | `brew install sops` | Display-only (see below) | macOS brew: Yes. Linux: No |
| python3 | URL fallback: https://www.python.org/downloads/ | URL fallback | URL fallback | No (managed by system Python setup; too risky to auto-install) |

**RHEL gcloud install steps** (display-only, `can_auto_run=False`):
```
sudo tee /etc/yum.repos.d/google-cloud-sdk.repo << 'EOM'
[google-cloud-cli]
name=Google Cloud CLI
baseurl=https://packages.cloud.google.com/yum/repos/cloud-sdk-el9-x86_64
enabled=1
gpgcheck=1
repo_gpgcheck=0
gpgkey=https://packages.cloud.google.com/yum/doc/rpm-package-key.gpg
EOM
sudo dnf install -y google-cloud-cli
```

**sops Linux install steps** (display-only, `can_auto_run=False`):
Pinning to a specific version and showing the SHA256 command from the release
is the safest approach, but the exact hash changes with each release and cannot
be hardcoded safely in the source code without a maintenance process. For safety,
sops on Linux is always display-only, pointing to the releases page:
```
https://github.com/getsops/sops/releases/latest
```
Users are shown two steps: download the binary for their arch and verify its
checksum using the `.checksums.txt` file published alongside each release.
The installer prints these as display-only instructions; it does not
auto-construct or auto-execute a curl command with a pinned version.

For **python3**: since Python is the runtime for the installer itself and is already
running at the point of the prereq check, a missing `python3` on PATH means the
installer would not have launched in the first place. `get_os_install_hint("python3")`
returns the `python.org` download URL as a URL-only fallback (display-only).
In practice, this code path is only reachable if the `python3` binary installed
a different name or PATH issue, and the message guides the user accordingly.

### Updated: `scripts/preflight.py`

**`check_billing_tristate` is defined in `scripts/gcp.py`** (not in `preflight.py`),
alongside `check_required_apis()` and `check_quotas()`. All three are GCP probe functions
with identical structural characteristics (run a gcloud command, return a structured result).
Placing them in `gcp.py` avoids a circular import that would arise if `check_billing_tristate`
were in `preflight.py` (since `preflight.py` imports from `diagnosis.py` and `diagnosis.py`
needs `check_billing_tristate`).

`preflight.py` imports `check_billing_tristate` from `scripts.gcp`:
```python
from scripts.gcp import check_billing_tristate
```

The existing `check_gcp_billing()` in `preflight.py` is kept unchanged for backward
compatibility with existing callers (e.g., `cmd_init`); the new code only uses
`check_billing_tristate` (from `gcp.py`).

Both `run_pre_apply_checks` (in `diagnosis.py`) and `diagnose_stage_failure` (in `diagnosis.py`)
call `check_billing_tristate` imported from `scripts.gcp` — they do NOT re-implement
the billing probe inline.

Two additions to the existing flow for init/preflight integration:

1. `check_application_default_credentials()` — top-level import from `diagnosis`;
   callable standalone so `cmd_init()` can call it after `check_gcp_auth()`
2. `get_os_install_hint(tool)` — imported from `diagnosis`; used in
   `run_preflight_display()` to show the OS-aware hint in the failure line.
   `run_preflight_display()` displays hints only (no auto-install offer) — the
   auto-install offer lives in `cmd_init()` via `offer_tool_install()`.

**What `run_preflight_display()` shows for a missing tool:**
The existing code shows `r.hint` (a URL string). The updated code calls
`get_os_install_hint(r.tool)` and shows **only the first step** from `install_steps`
inline in the `✗` line as a short hint (e.g., `brew install --cask google-cloud-sdk`
or `pip3 install ansible`). For multi-step installs (RHEL gcloud, HashiCorp apt repo),
the first step alone is not executable; in those cases, show the URL from the last
element of `install_steps` (which should be a URL for display-only installs) or
fall back to the existing `r.hint` URL. This keeps the `✗` line single-line and
legible. The full multi-step instructions are shown by `offer_tool_install()` in the
`cmd_init()` loop, not by `run_preflight_display()`.

### Updated: `scripts/commands/init.py`

**Top-level imports** (added to existing import block at top of file — not deferred):
```python
from scripts.diagnosis import (
    check_application_default_credentials,
    offer_adc_setup,
    offer_tool_install,
    get_os_install_hint,   # used by _can_auto_run helper below
)
```

`_can_auto_run(tool: str) -> bool` is a local module-level helper defined in init.py:
```python
def _can_auto_run(tool: str) -> bool:
    _, can_auto = get_os_install_hint(tool)
    return can_auto
```

**Step 1 — Prerequisites block** replaces the existing failure handling:

The existing code:
```python
all_ok = run_preflight_display(results)
if not all_ok:
    print_error("Some prerequisites are missing. Install them and re-run.")
    sys.exit(1)
```

Is replaced with a **two-pass loop** to avoid hiding multiple missing tools:

```python
all_ok = run_preflight_display(results)
if not all_ok:
    # Pass 1: auto-installable tools — attempt each in sequence.
    # Exit immediately if an auto-install attempt fails (tool still not on PATH after
    # running), since the user needs to resolve it before we can proceed.
    for r in results:
        if not r.ok and _can_auto_run(r.tool):
            installed = offer_tool_install(r.tool)
            if not installed:
                # offer_tool_install() already printed guidance
                sys.exit(1)

    # Pass 2: display-only tools — collect ALL hints and show them together.
    # A first-time user may be missing gcloud, terraform, and sops simultaneously;
    # exiting after the first display-only tool would require multiple re-runs to
    # discover all missing prerequisites.
    display_only_missing = [r for r in results if not r.ok and not _can_auto_run(r.tool)]
    if display_only_missing:
        for r in display_only_missing:
            offer_tool_install(r.tool)  # always returns False; prints print_fix() block
        # sys.exit(1) is unconditional after the loop — do NOT call it inside the loop
        # (that would exit before showing hints for subsequent missing tools)
        sys.exit(1)
    # All tools now installed — continue
```

`_can_auto_run(tool)` is a local helper that calls `get_os_install_hint(tool)` and
returns the `can_auto_run` boolean. This avoids duplicating the policy table in init.py.

`run_preflight_display()` shows the OS-aware install hint in the `✗` line (via
the updated preflight.py), but does NOT offer to run it — that is `offer_tool_install()`'s
job. There is no duplication: `run_preflight_display()` prints the hint as
informational context; `offer_tool_install()` then asks "Install now?".

**Step 2b — ADC setup** (inserted after existing `check_gcp_auth()` block):
```python
adc_ok, _ = check_application_default_credentials()
if not adc_ok:
    refreshed = offer_adc_setup()
    if not refreshed:
        # offer_adc_setup() already printed the fix and re-run instruction
        sys.exit(1)
print_success("Application Default Credentials valid")
```

### Updated: `scripts/commands/apply.py`

**Top-level imports** (added to existing import block at top of file):
```python
from scripts.diagnosis import run_pre_apply_checks
```

**After `_show_plan()` and the user confirmation prompt**, immediately before `run_pipeline()`:

```python
if not dry_run:
    if not run_pre_apply_checks(config, auto_approve=auto_approve, only_stage=only_stage):
        print_error("Pre-apply checks failed. Fix the issues above and re-run.")
        sys.exit(1)
```

`only_stage` comes from the `--stage` CLI option already in `cmd_apply()`'s scope.
Threading it disables the timestamp skip for single-stage targeted re-runs.

Rationale for placement: the plan display and confirmation prompt are instantaneous.
The health checks take up to 10 seconds and block progress. Showing the plan first
lets the user see what will run before waiting; it also means a blocking check failure
appears only after the user has confirmed intent, not before they even see the plan.
Placing the checks after confirmation but before `run_pipeline()` is the correct gate.

The post-failure diagnosis is printed by `pipeline.py` (the single call site).
`cmd_apply()`'s existing generic failure result box is kept unchanged.

### Updated: `scripts/commands/destroy.py`

**Top-level imports** (added to existing import block at top of file):
```python
from scripts.diagnosis import check_application_default_credentials, offer_adc_setup
```

**After `config.load()`**, before showing the plan:
```python
adc_ok, _ = check_application_default_credentials()
if not adc_ok:
    refreshed = offer_adc_setup(auto_accept=auto_approve)
    if not refreshed:
        print_error("Terraform destroy requires Application Default Credentials.")
        sys.exit(1)
```

### Updated: `scripts/pipeline.py`

**Top-level imports** (added to existing import blocks at top of file):
```python
from scripts.diagnosis import diagnose_stage_failure
from scripts.display import print_fix   # add to existing scripts.display import line
```
`print_fix` is a new function added to `display.py` in step 4. It must be in pipeline.py's
import block (not just in `diagnosis.py`) because `pipeline.py` calls it directly to
render the hints panel after a stage failure.

In the failure branch of the stage loop:
```python
if not ok:
    stages[stage_name] = "failed"
    state["deployment_state"] = "failed"
    state["stages"] = stages
    save_state(state)
    print_error(f"Stage '{label}' failed. Pipeline halted.")

    hints = diagnose_stage_failure(config, stage_name)
    if hints:
        print_fix("Likely causes", hints)  # single panel, list[str] signature

    print_step("Resume with: [bold]voipbin-install apply[/bold]")
    return False
```

`pipeline.py` is the sole call site for post-failure diagnosis.

### Updated: `scripts/cli.py`

Add `terraform_reconcile` to the `--stage` `click.Choice` (regression fix —
the stage was added to `APPLY_STAGES` in `pipeline.py` but omitted from the CLI;
the in-function guard in `cmd_apply()` checks against `APPLY_STAGES` which already
contains `terraform_reconcile`, so only the CLI option needs updating).

The complete replacement `click.Choice` list (all 5 stages — replaces the existing
4-element list in `cli.py`):

```python
@click.option(
    "--stage",
    type=click.Choice([
        "terraform_init",
        "terraform_reconcile",
        "terraform_apply",
        "ansible_run",
        "k8s_apply",
    ]),
    default=None,
    help="Run only a specific pipeline stage",
)
```

No other change is needed in `cli.py`. The guard in `cmd_apply()` that validates the
`--stage` value against `APPLY_STAGES` already includes `terraform_reconcile`.

### Updated: `scripts/gcp.py`

Two new functions — `check_billing_tristate()` and `check_required_apis()` — defined
alongside the existing `check_quotas()` in `gcp.py`. Both are GCP probe functions with
the same structural pattern (run a gcloud command, return a structured result). Placing
them here avoids a circular import between `diagnosis.py` and `preflight.py`.

```python
from typing import Literal

def check_billing_tristate(project_id: str) -> Literal["enabled", "disabled", "unknown"]:
    """Three-way billing check distinguishing disabled from probe failure.

    Returns "unknown" when the gcloud command itself fails (auth error, network, etc.)
    so callers can skip the billing hint rather than showing a false positive.
    """
    result = run_cmd([
        "gcloud", "billing", "projects", "describe", project_id,
        "--format=value(billingEnabled)",
    ])
    if result.returncode != 0:
        return "unknown"
    if "true" in result.stdout.lower():
        return "enabled"
    return "disabled"
```

New function `check_required_apis()` — called by both `run_pre_apply_checks()` (check 4)
and `diagnose_stage_failure()` (terraform_* stages). Defined in `gcp.py` alongside the
existing `check_quotas()`:

```python
REQUIRED_APIS = [
    "compute.googleapis.com",
    "container.googleapis.com",
    "sqladmin.googleapis.com",
]

def check_required_apis(project_id: str) -> list[str]:
    """Return list of required APIs not yet enabled in the project.

    Returns an empty list if all required APIs are enabled or the check fails
    (non-zero exit from gcloud). Callers treat a non-empty list as actionable;
    an empty list from a failed probe is indistinguishable from all-enabled and
    is acceptable — the deployment will fail on its own if an API is truly missing.
    """
    result = run_cmd([
        "gcloud", "services", "list",
        "--enabled",
        "--project", project_id,
        "--format=value(config.name)",
    ])
    if result.returncode != 0:
        return []  # probe failed — do not generate false "enable API" hints
    enabled = set(result.stdout.splitlines())
    return [api for api in REQUIRED_APIS if api not in enabled]
```

`create_service_account()` change: surface real errors without breaking idempotent
role-binding (which must run regardless of whether the create step succeeded):

```python
result = run_cmd([...create SA...])
if result.returncode != 0:
    stderr_lower = result.stderr.lower()
    if "already exists" in stderr_lower:
        pass  # expected idempotent case
    else:
        print_warning(f"Service account creation error: {result.stderr.strip()}")
        print_warning("Continuing with role binding using pre-computed SA email.")
        # Do NOT return early — role binding is idempotent and must always run

# Role binding always runs (idempotent regardless of create outcome)
for role in roles_data["roles"]:
    run_cmd_with_retry([...bind role...])
```

### Updated: `scripts/secretmgr.py`

Top-level import addition:
```python
from scripts.display import print_error
```

`decrypt_with_sops()` — add error logging with file path:
```python
if result.returncode != 0:
    print_error(f"SOPS decryption failed for {encrypted_path}: {result.stderr.strip()}")
    print_error("Ensure Application Default Credentials are valid and your account "
                "has roles/cloudkms.cryptoKeyDecrypter on the KMS key.")
    return None
```

### Updated: `scripts/display.py`

New helper accepting `list[str] | str` for the content area:

```python
def print_fix(title: str, lines: list[str] | str) -> None:
    """Print a 'How to fix' guidance box."""
    if isinstance(lines, str):
        lines = [lines]
    body = "\n".join(f"  {line}" for line in lines)
    console.print(Panel(
        f"[bold yellow]{title}[/bold yellow]\n\n{body}",
        border_style="yellow",
        expand=False,
    ))
```

Note: no extra leading spaces on the Panel title or body — the Panel widget
provides its own padding. This matches the indentation style of `print_result_box()`
and other Panel-based helpers in `display.py`. This is intentional; the consistent
no-prefix convention makes all panels visually uniform regardless of nesting depth.

### Updated: `scripts/utils.py`

Fix `_SAFE_ID_RE` to allow `[` and `]` — required for indexed Terraform resource
addresses used by `terraform_reconcile.py` (e.g., `google_compute_instance.kamailio[0]`).
Current regex crashes on all indexed addresses with a `ValueError` before the import
command runs.

```python
_SAFE_ID_RE = re.compile(r"^[a-zA-Z0-9._:/@\[\]-]+$")
```

The `[` and `]` characters are safe in this context: `_validate_cmd_arg` is used on
Terraform resource addresses (not shell commands), and the caller passes the value
as a subprocess argument (not through a shell), so bracket injection is not a risk.

## Error Patterns and Fix Commands

| Pattern | Stage | Cause | Fix |
|---------|-------|-------|-----|
| `invalid_grant` / `reauth related error` | terraform_* | ADC expired | `gcloud auth application-default login` |
| ADC file absent | any | First-time user, ADC never configured | `gcloud auth application-default login` |
| GCS bucket missing / `bucket doesn't exist` | terraform_init | First run — state bucket not created yet | `gcloud storage buckets create gs://PROJECT-voipbin-tf-state --project=PROJECT` then retry |
| `PERMISSION_DENIED` / `403` | terraform_* | Missing IAM roles | `gcloud projects add-iam-policy-binding PROJECT --member=user:EMAIL --role=roles/editor` |
| `QUOTA_EXCEEDED` | terraform_apply | Quota hit | `https://console.cloud.google.com/iam-admin/quotas?project=PROJECT` |
| `SERVICE_DISABLED` / `API not enabled` | terraform_init | API missing | `gcloud services enable APINAME --project PROJECT` |
| `billingDisabled` | any | Billing off | `https://console.cloud.google.com/billing` |
| `RESOURCE_EXHAUSTED` | any | Rate limit | Wait 60s, retry |
| SSH timeout / connection refused | ansible_run | VMs still booting | Wait 2 min, `voipbin-install apply` |
| VMs TERMINATED | ansible_run | VM stopped unexpectedly | Check GCP Console → Compute Engine |
| `Unable to connect to server` | k8s_apply | GKE still provisioning | Wait 5 min, `voipbin-install apply` |
| Cluster not found | k8s_apply | terraform_apply incomplete | Re-run from terraform_apply stage |
| `Already exists` / `409` | terraform_apply | Resource drift | Handled by terraform_reconcile (auto-import) |
| SOPS decrypt fails | k8s_apply | ADC or KMS IAM | `gcloud auth application-default login` |

## Testing Strategy

New `tests/test_diagnosis.py`:
- `check_application_default_credentials()`: valid token, expired/error, gcloud missing
- `offer_adc_setup()`: ADC file absent (first-time framing), ADC expired (refresh framing), user says "n" (prints fix, returns False), user says "y" (login success), user says "y" (login timeout → prints fix, returns False), auto_accept=True bypasses prompt; gcloud absent (`shutil.which("gcloud")` returns None) → prints prereq message, returns False (not ADC guidance); **auto_accept=True AND gcloud absent** → assert prereq message printed, returns False (verifies gcloud guard fires before auto-accept logic); CLOUDSDK_CONFIG env var set → uses custom path for file-absent detection
- `run_pre_apply_checks()`: all-pass, ADC fail with auto_approve propagation, billing fail (exit 0 + stdout "False" → .lower() check correctly identifies disabled), billing probe failure (exit non-zero → no billing hint), API fail; timestamp skip when state not "failed" and fresh and only_stage=None; no skip when state "failed"; no skip when only_stage is set (even with fresh timestamp); no skip when timestamp stale; no skip when timestamp field absent in state file (treated as stale)
- `diagnose_stage_failure()`: each stage × each failure mode; ADC-invalid guard (mock returns (False, None) → list contains ADC hint and NO billing hint); billing stdout "False" case → billing hint added; billing exit non-zero → no billing hint; ansible_run VM filter uses labels.env not tags; k8s_apply describe passes --zone; quota hints formatted correctly; empty return when no issue
- `_detect_os()`: Darwin → macos; quoted `ID="ubuntu"` (with double quotes) correctly stripped; rhel, centos, fedora, arch variants; /etc/os-release absent → linux; ID field absent → linux
- `get_os_install_hint()`: each tool × each OS; verify `can_auto_run` correctness; verify sops Linux returns display-only; verify python3 returns URL fallback; verify RHEL gcloud is display-only
- `offer_tool_install()`: auto-run path (success); auto-run path with PATH-not-updated (prints shell-restart message, returns False); display-only path (prints re-run message, returns False)

Updated tests:
- `tests/test_preflight.py` — ADC check integration; OS hint appears in failure display (not auto-install offer); mocks both `platform.system()` and `/etc/os-release`
- `tests/test_pipeline.py` — `diagnose_stage_failure()` called after stage failure with single print_fix(); no diagnosis call on success
- `tests/test_gcp.py` — SA create: already-exists path (role binding runs), real error path (warning printed, role binding still runs), success path; `check_required_apis()`: all enabled → returns []; one missing → returns that API name; gcloud command fails (exit non-zero) → returns [] (no false hints); all three APIs checked; `check_billing_tristate()`: gcloud exit 0 + stdout "True" → "enabled"; exit 0 + stdout "False" → "disabled"; exit non-zero → "unknown"; stdout "true" (lowercase) → "enabled" (case-insensitive via `.lower()`)
- `tests/test_secretmgr.py` — decrypt failure logs file path and stderr; verify `from scripts.display import print_error` is present in module

## Implementation Order

1. `scripts/utils.py` — fix `_SAFE_ID_RE` to allow `[` and `]` (FIRST: `terraform_reconcile.py`
   already calls `_validate_cmd_arg` with indexed addresses; the crash exists in production code)
2. `scripts/gcp.py` — add `check_billing_tristate()`, `check_required_apis()` + `REQUIRED_APIS`;
   surface SA errors, always proceed to role binding
   (SECOND: `diagnosis.py` imports both billing and API functions from here; must exist before step 5)
3. `scripts/preflight.py` — add `from scripts.gcp import check_billing_tristate` import +
   add tests in `tests/test_preflight.py` and `tests/test_gcp.py`
   (covers: enabled, disabled, unknown/probe-failure cases; tests live in test_gcp.py since the
   function is now in gcp.py)
4. `scripts/display.py` — `print_fix(title, lines: list[str] | str)` helper (no deps)
5. `scripts/diagnosis.py` — full new module + `tests/test_diagnosis.py`
   (imports `check_required_apis` from `gcp.py` — requires step 2)
   (imports `check_billing_tristate` from `preflight.py` — requires step 3)
   (imports `print_fix` from `display.py` — requires step 4)
6. `scripts/preflight.py` — wire `check_application_default_credentials()` + OS hints (display only)
7. `scripts/commands/init.py` — top-level imports, two-pass prereq loop, ADC setup step
8. `scripts/commands/apply.py` — top-level import, pre-apply checks with only_stage threading
9. `scripts/commands/destroy.py` — top-level imports, ADC check with auto_approve threading
10. `scripts/pipeline.py` — top-level import, single `print_fix()` call after stage failure
11. `scripts/cli.py` — add `terraform_reconcile` to `--stage` choices
12. `scripts/secretmgr.py` — add display import, log decrypt error with file path
