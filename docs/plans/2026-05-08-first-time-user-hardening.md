# First-Time User Hardening Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Guide first-time VoIPBin users past the most common install failures: ADC expiry, billing off, missing APIs, wrong tool versions, and cryptic pipeline errors.

**Architecture:** Three independent layers — pre-flight hardening (ADC + tool install in init, health checks before apply/destroy), post-failure diagnosis (GCP probe after any stage failure, single call site in pipeline.py), and surface-level fixes (regex, SA errors, SOPS decrypt logging, missing CLI stage). Central new module `scripts/diagnosis.py` imports only from `scripts.gcp`, `scripts.display`, `scripts.utils`, and `scripts.config` — no circular dependency with `preflight.py`.

**Tech Stack:** Python 3.10+, Rich (console/Panel), Click, gcloud CLI subprocess calls, pytest+unittest.mock.

---

## Dependency graph

```
display.py  (no internal deps)
utils.py    (no internal deps)
gcp.py      → display, utils
preflight.py → display, utils
diagnosis.py → gcp, display, utils, config   ← NEW
preflight.py → diagnosis  (new import: check_application_default_credentials, get_os_install_hint)
init.py     → diagnosis   (new imports)
apply.py    → diagnosis   (new import: run_pre_apply_checks)
destroy.py  → diagnosis   (new imports)
pipeline.py → diagnosis, display  (new imports)
```

Implementation order must respect this graph: gcp.py before diagnosis.py, diagnosis.py before all callers.

---

## Task 1: Fix _SAFE_ID_RE in scripts/utils.py

**Files:**
- Modify: `scripts/utils.py:19`
- Test: `tests/test_utils.py`

**Step 1: Write a failing test for indexed Terraform addresses**

Add to `tests/test_utils.py`:

```python
def test_validate_cmd_arg_allows_indexed_address():
    # Currently crashes on google_compute_instance.kamailio[0]
    from scripts.utils import _validate_cmd_arg
    _validate_cmd_arg("google_compute_instance.kamailio[0]", "resource")  # must not raise
```

**Step 2: Run test to confirm it fails**

```bash
cd /home/pchero/gitvoipbin/install/.worktrees/NOJIRA-First-time-user-hardening
pytest tests/test_utils.py::test_validate_cmd_arg_allows_indexed_address -v
```

Expected: FAIL — `ValueError: Unsafe characters in resource`

**Step 3: Fix the regex in scripts/utils.py line 19**

Change:
```python
_SAFE_ID_RE = re.compile(r"^[a-zA-Z0-9._:/@-]+$")
```
To:
```python
_SAFE_ID_RE = re.compile(r"^[a-zA-Z0-9._:/@\[\]-]+$")
```

The `[` and `]` are safe here — `_validate_cmd_arg` values are passed as subprocess arguments (not shell strings), so bracket injection is not a risk.

**Step 4: Run test to confirm it passes**

```bash
pytest tests/test_utils.py -v
```

Expected: all tests PASS

**Step 5: Commit**

```bash
git add scripts/utils.py tests/test_utils.py
git commit -m "fix: allow [ and ] in _SAFE_ID_RE for indexed Terraform addresses"
```

---

## Task 2: Add new GCP probe functions to scripts/gcp.py

Add `check_billing_tristate()`, `check_required_apis()`, and surface SA creation errors.

**Files:**
- Modify: `scripts/gcp.py`
- Modify: `scripts/gcp.py` (`create_service_account`)
- Test: `tests/test_gcp.py`

**Step 1: Write failing tests for check_billing_tristate**

Add to `tests/test_gcp.py`:

```python
from scripts.gcp import check_billing_tristate, check_required_apis

class TestCheckBillingTristate:
    @patch("scripts.gcp.run_cmd")
    def test_billing_enabled(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout="True\n")
        assert check_billing_tristate("my-project") == "enabled"

    @patch("scripts.gcp.run_cmd")
    def test_billing_enabled_lowercase(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout="true\n")
        assert check_billing_tristate("my-project") == "enabled"

    @patch("scripts.gcp.run_cmd")
    def test_billing_disabled(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout="False\n")
        assert check_billing_tristate("my-project") == "disabled"

    @patch("scripts.gcp.run_cmd")
    def test_billing_unknown_on_error(self, mock_run):
        mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="PERMISSION_DENIED")
        assert check_billing_tristate("my-project") == "unknown"


class TestCheckRequiredApis:
    @patch("scripts.gcp.run_cmd")
    def test_all_enabled_returns_empty(self, mock_run):
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout="compute.googleapis.com\ncontainer.googleapis.com\nsqladmin.googleapis.com\n"
        )
        assert check_required_apis("my-project") == []

    @patch("scripts.gcp.run_cmd")
    def test_one_missing(self, mock_run):
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout="compute.googleapis.com\ncontainer.googleapis.com\n"
        )
        missing = check_required_apis("my-project")
        assert missing == ["sqladmin.googleapis.com"]

    @patch("scripts.gcp.run_cmd")
    def test_probe_failure_returns_empty(self, mock_run):
        mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="error")
        assert check_required_apis("my-project") == []
```

**Step 2: Run tests to confirm they fail**

```bash
pytest tests/test_gcp.py::TestCheckBillingTristate tests/test_gcp.py::TestCheckRequiredApis -v
```

Expected: ImportError — functions don't exist yet

**Step 3: Add check_billing_tristate and check_required_apis to scripts/gcp.py**

Add after the existing `check_quotas` function (around line 104), before `display_quota_results`:

```python
from typing import Literal  # add to top-level imports at top of file

REQUIRED_APIS = [
    "compute.googleapis.com",
    "container.googleapis.com",
    "sqladmin.googleapis.com",
]


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


def check_required_apis(project_id: str) -> list[str]:
    """Return list of required APIs not yet enabled in the project.

    Returns an empty list if all required APIs are enabled or the check fails.
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

**Step 4: Fix create_service_account to surface real errors**

In `scripts/gcp.py`, replace the `create_service_account` SA creation block (lines 163-168):

```python
# OLD (silently ignores all errors):
run_cmd(
    ["gcloud", "iam", "service-accounts", "create", sa_name,
     f"--display-name={display_name}",
     f"--project={project_id}"],
    timeout=30,
)
```

With:
```python
# NEW (surfaces real errors, keeps idempotency for already-exists):
result = run_cmd(
    ["gcloud", "iam", "service-accounts", "create", sa_name,
     f"--display-name={display_name}",
     f"--project={project_id}"],
    timeout=30,
)
if result.returncode != 0:
    stderr_lower = result.stderr.lower()
    if "already exists" not in stderr_lower:
        print_warning(f"Service account creation error: {result.stderr.strip()}")
        print_warning("Continuing with role binding using pre-computed SA email.")
        # Do NOT return early — role binding is idempotent and must always run
```

**Step 5: Add SA error tests to test_gcp.py**

```python
class TestCreateServiceAccount:
    @patch("scripts.gcp._load_yaml_data")
    @patch("scripts.gcp.run_cmd_with_retry")
    @patch("scripts.gcp.run_cmd")
    def test_already_exists_is_silent(self, mock_run, mock_retry, mock_load):
        mock_run.return_value = MagicMock(
            returncode=1, stderr="ERROR: (gcloud.iam.service-accounts.create) Resource in projects [p] already exists"
        )
        mock_load.return_value = {"roles": []}
        email = create_service_account("my-project")
        assert email == "voipbin-installer@my-project.iam.gserviceaccount.com"

    @patch("scripts.gcp._load_yaml_data")
    @patch("scripts.gcp.run_cmd_with_retry")
    @patch("scripts.gcp.run_cmd")
    @patch("scripts.gcp.print_warning")
    def test_real_error_prints_warning_and_continues(self, mock_warn, mock_run, mock_retry, mock_load):
        mock_run.return_value = MagicMock(returncode=1, stderr="PERMISSION_DENIED: foo")
        mock_load.return_value = {"roles": ["roles/editor"]}
        email = create_service_account("my-project")
        assert email is not None
        assert mock_warn.called  # warning printed
        assert mock_retry.called  # role binding still ran
```

**Step 6: Run all gcp tests**

```bash
pytest tests/test_gcp.py -v
```

Expected: all PASS

**Step 7: Commit**

```bash
git add scripts/gcp.py tests/test_gcp.py
git commit -m "feat: add check_billing_tristate, check_required_apis; surface SA errors"
```

---

## Task 3: Add print_fix to scripts/display.py

**Files:**
- Modify: `scripts/display.py`
- Test: `tests/test_display.py`

**Step 1: Write failing tests**

Add to `tests/test_display.py`:

```python
from unittest.mock import patch, MagicMock
from scripts.display import print_fix

class TestPrintFix:
    @patch("scripts.display.console")
    def test_string_input(self, mock_console):
        print_fix("How to fix", "gcloud auth application-default login")
        mock_console.print.assert_called_once()
        call_args = str(mock_console.print.call_args)
        assert "How to fix" in call_args

    @patch("scripts.display.console")
    def test_list_input(self, mock_console):
        print_fix("Likely causes", ["Billing disabled", "ADC expired"])
        mock_console.print.assert_called_once()

    @patch("scripts.display.console")
    def test_single_item_list(self, mock_console):
        print_fix("Fix", ["run: gcloud auth login"])
        mock_console.print.assert_called_once()
```

**Step 2: Run tests to confirm they fail**

```bash
pytest tests/test_display.py::TestPrintFix -v
```

Expected: ImportError or AttributeError

**Step 3: Add print_fix to scripts/display.py**

Add after `print_result_box` (after line 55):

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

**Step 4: Run tests**

```bash
pytest tests/test_display.py -v
```

Expected: all PASS

**Step 5: Commit**

```bash
git add scripts/display.py tests/test_display.py
git commit -m "feat: add print_fix() guidance box helper to display.py"
```

---

## Task 4a: Create scripts/diagnosis.py — ADC functions

**Files:**
- Create: `scripts/diagnosis.py`
- Create: `tests/test_diagnosis.py`

**Step 1: Create tests/test_diagnosis.py with ADC tests**

```python
"""Tests for scripts/diagnosis.py."""

import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


class TestCheckApplicationDefaultCredentials:
    @patch("scripts.diagnosis.run_cmd")
    def test_valid_token_returns_true_with_account(self, mock_run):
        # Call 1: token check succeeds. Call 2: account lookup returns email.
        mock_run.side_effect = [
            MagicMock(returncode=0, stdout="ya29.token\n"),
            MagicMock(returncode=0, stdout="user@example.com\n"),
        ]
        from scripts.diagnosis import check_application_default_credentials
        valid, account = check_application_default_credentials()
        assert valid is True
        assert account == "user@example.com"

    @patch("scripts.diagnosis.run_cmd")
    def test_invalid_token_returns_false_none(self, mock_run):
        mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="invalid_grant")
        from scripts.diagnosis import check_application_default_credentials
        valid, account = check_application_default_credentials()
        assert valid is False
        assert account is None
        assert mock_run.call_count == 1  # no second subprocess

    @patch("scripts.diagnosis.run_cmd")
    def test_valid_token_unset_account_returns_true_none(self, mock_run):
        mock_run.side_effect = [
            MagicMock(returncode=0, stdout="ya29.token\n"),
            MagicMock(returncode=0, stdout="(unset)\n"),
        ]
        from scripts.diagnosis import check_application_default_credentials
        valid, account = check_application_default_credentials()
        assert valid is True
        assert account is None  # never expose "(unset)" to callers


class TestOfferAdcSetup:
    @patch("scripts.diagnosis.shutil.which", return_value=None)
    @patch("scripts.diagnosis.print_error")
    def test_gcloud_missing_returns_false(self, mock_err, mock_which):
        from scripts.diagnosis import offer_adc_setup
        result = offer_adc_setup()
        assert result is False
        mock_err.assert_called()

    @patch("scripts.diagnosis.shutil.which", return_value=None)
    @patch("scripts.diagnosis.print_error")
    def test_auto_accept_with_gcloud_missing_still_returns_false(self, mock_err, mock_which):
        """gcloud guard fires BEFORE auto_accept — even auto_accept=True cannot bypass it."""
        from scripts.diagnosis import offer_adc_setup
        result = offer_adc_setup(auto_accept=True)
        assert result is False

    @patch("scripts.diagnosis.check_application_default_credentials")
    @patch("scripts.diagnosis.run_cmd")
    @patch("scripts.diagnosis.shutil.which", return_value="/usr/bin/gcloud")
    @patch("scripts.diagnosis.confirm", return_value=False)
    @patch("scripts.diagnosis.print_fix")
    def test_user_declines_prints_fix_returns_false(
        self, mock_fix, mock_confirm, mock_which, mock_run, mock_check
    ):
        from scripts.diagnosis import offer_adc_setup
        # ADC is invalid (so the setup offer fires)
        mock_check.return_value = (False, None)
        # Simulate missing ADC file
        with patch("pathlib.Path.exists", return_value=False):
            result = offer_adc_setup()
        assert result is False
        mock_fix.assert_called()

    @patch("scripts.diagnosis.check_application_default_credentials")
    @patch("scripts.diagnosis.run_cmd")
    @patch("scripts.diagnosis.shutil.which", return_value="/usr/bin/gcloud")
    def test_auto_accept_invokes_login_directly(self, mock_which, mock_run, mock_check):
        mock_run.return_value = MagicMock(returncode=0)
        mock_check.side_effect = [(False, None), (True, "user@example.com")]
        with patch("pathlib.Path.exists", return_value=True):
            from scripts.diagnosis import offer_adc_setup
            result = offer_adc_setup(auto_accept=True)
        mock_run.assert_called()
        assert result is True

    def test_cloudsdk_config_env_var_used(self, tmp_path, monkeypatch):
        """When CLOUDSDK_CONFIG is set, ADC file path uses that directory."""
        monkeypatch.setenv("CLOUDSDK_CONFIG", str(tmp_path))
        with patch("scripts.diagnosis.shutil.which", return_value="/usr/bin/gcloud"), \
             patch("scripts.diagnosis.check_application_default_credentials", return_value=(False, None)), \
             patch("scripts.diagnosis.confirm", return_value=False), \
             patch("scripts.diagnosis.print_fix"):
            from scripts.diagnosis import offer_adc_setup
            offer_adc_setup()
            # File would have been checked at tmp_path/application_default_credentials.json
            # No assertion needed beyond "no crash"
```

**Step 2: Run tests to confirm they fail**

```bash
pytest tests/test_diagnosis.py::TestCheckApplicationDefaultCredentials tests/test_diagnosis.py::TestOfferAdcSetup -v
```

Expected: ModuleNotFoundError — `scripts.diagnosis` doesn't exist yet

**Step 3: Create scripts/diagnosis.py with ADC functions**

```python
"""Diagnosis and guided recovery functions for VoIPBin installer."""

from __future__ import annotations

import os
import platform
import shutil
from pathlib import Path
from typing import TYPE_CHECKING

from scripts.display import confirm, print_error, print_fix, print_success, print_warning
from scripts.utils import run_cmd

if TYPE_CHECKING:
    from scripts.config import InstallerConfig


# ---------------------------------------------------------------------------
# 1. ADC Check and Setup
# ---------------------------------------------------------------------------

def check_application_default_credentials() -> tuple[bool, str | None]:
    """Check Application Default Credentials validity.

    Returns (is_valid, account) where account is None if not retrievable.
    is_valid=True means the ADC token works; account is a best-effort lookup.
    """
    result = run_cmd(["gcloud", "auth", "application-default", "print-access-token"])
    if result.returncode != 0:
        return False, None

    account_result = run_cmd(["gcloud", "config", "get-value", "account"])
    if account_result.returncode != 0:
        return True, None
    account = account_result.stdout.strip()
    if not account or account == "(unset)":
        return True, None
    return True, account


def _get_adc_file_path() -> Path:
    """Return the ADC credentials file path, respecting CLOUDSDK_CONFIG."""
    cloudsdk_config = os.environ.get("CLOUDSDK_CONFIG")
    if cloudsdk_config:
        return Path(cloudsdk_config) / "application_default_credentials.json"
    return Path.home() / ".config" / "gcloud" / "application_default_credentials.json"


def offer_adc_setup(auto_accept: bool = False) -> bool:
    """Offer to set up or refresh Application Default Credentials.

    Guard 1 (unconditional): if gcloud is not on PATH, print prereq message and
    return False immediately — even auto_accept=True cannot bypass this.
    """
    if shutil.which("gcloud") is None:
        print_error("gcloud CLI is not installed. Run: voipbin-install init")
        return False

    adc_path = _get_adc_file_path()
    if not adc_path.exists():
        print_warning("Application Default Credentials are not yet configured.")
        print_warning("These credentials allow Terraform to access GCP on your behalf.")
        prompt = "Set up credentials now? [Y/n]"
    else:
        print_warning("Your Application Default Credentials have expired.")
        prompt = "Refresh credentials now? [Y/n]"

    print_warning("A browser window will open for you to sign in to GCP.")

    if not auto_accept:
        if not confirm(prompt, default=True):
            print_fix("How to fix", ["gcloud auth application-default login"])
            return False

    result = run_cmd(
        ["gcloud", "auth", "application-default", "login"],
        capture=False,
        timeout=300,
    )
    if result.returncode != 0:
        print_fix("How to fix", ["gcloud auth application-default login"])
        print_error("Then re-run: voipbin-install <command>")
        return False

    valid, _ = check_application_default_credentials()
    return valid
```

**Step 4: Run ADC tests**

```bash
pytest tests/test_diagnosis.py::TestCheckApplicationDefaultCredentials tests/test_diagnosis.py::TestOfferAdcSetup -v
```

Expected: all PASS

**Step 5: Commit**

```bash
git add scripts/diagnosis.py tests/test_diagnosis.py
git commit -m "feat: add check_application_default_credentials and offer_adc_setup"
```

---

## Task 4b: Add run_pre_apply_checks to scripts/diagnosis.py

**Step 1: Write failing tests for run_pre_apply_checks**

Add to `tests/test_diagnosis.py`:

```python
from datetime import datetime, timezone, timedelta


def _make_config(project_id="my-project", region="us-central1", zone="us-central1-a"):
    cfg = MagicMock()
    cfg.get.side_effect = lambda k, *a: {
        "gcp_project_id": project_id, "region": region, "zone": zone
    }.get(k, a[0] if a else None)
    return cfg


class TestRunPreApplyChecks:
    @patch("scripts.diagnosis.check_required_apis", return_value=[])
    @patch("scripts.diagnosis.check_billing_tristate", return_value="enabled")
    @patch("scripts.diagnosis.run_cmd")
    @patch("scripts.diagnosis.check_application_default_credentials", return_value=(True, "u@e.com"))
    def test_all_pass_returns_true(self, mock_adc, mock_run, mock_bill, mock_apis):
        mock_run.return_value = MagicMock(returncode=0, stdout="my-project")
        from scripts.diagnosis import run_pre_apply_checks
        assert run_pre_apply_checks(_make_config()) is True

    @patch("scripts.diagnosis.offer_adc_setup", return_value=False)
    @patch("scripts.diagnosis.check_application_default_credentials", return_value=(False, None))
    def test_adc_fail_returns_false(self, mock_adc, mock_setup):
        from scripts.diagnosis import run_pre_apply_checks
        assert run_pre_apply_checks(_make_config()) is False

    @patch("scripts.diagnosis.check_required_apis", return_value=[])
    @patch("scripts.diagnosis.check_billing_tristate", return_value="disabled")
    @patch("scripts.diagnosis.run_cmd")
    @patch("scripts.diagnosis.check_application_default_credentials", return_value=(True, "u@e.com"))
    def test_billing_disabled_returns_false(self, mock_adc, mock_run, mock_bill, mock_apis):
        mock_run.return_value = MagicMock(returncode=0, stdout="my-project")
        from scripts.diagnosis import run_pre_apply_checks
        assert run_pre_apply_checks(_make_config()) is False

    @patch("scripts.diagnosis.check_required_apis", return_value=[])
    @patch("scripts.diagnosis.check_billing_tristate", return_value="unknown")
    @patch("scripts.diagnosis.run_cmd")
    @patch("scripts.diagnosis.check_application_default_credentials", return_value=(True, "u@e.com"))
    def test_billing_unknown_continues_to_api_check(self, mock_adc, mock_run, mock_bill, mock_apis):
        mock_run.return_value = MagicMock(returncode=0, stdout="my-project")
        from scripts.diagnosis import run_pre_apply_checks
        assert run_pre_apply_checks(_make_config()) is True

    @patch("scripts.diagnosis.check_required_apis", return_value=["sqladmin.googleapis.com"])
    @patch("scripts.diagnosis.check_billing_tristate", return_value="enabled")
    @patch("scripts.diagnosis.run_cmd")
    @patch("scripts.diagnosis.check_application_default_credentials", return_value=(True, "u@e.com"))
    def test_missing_api_returns_false(self, mock_adc, mock_run, mock_bill, mock_apis):
        mock_run.return_value = MagicMock(returncode=0, stdout="my-project")
        from scripts.diagnosis import run_pre_apply_checks
        assert run_pre_apply_checks(_make_config()) is False

    @patch("scripts.diagnosis.check_required_apis", return_value=[])
    @patch("scripts.diagnosis.check_billing_tristate", return_value="enabled")
    @patch("scripts.diagnosis.run_cmd")
    @patch("scripts.diagnosis.check_application_default_credentials", return_value=(True, "u@e.com"))
    def test_timestamp_skip_when_fresh_and_not_failed(self, mock_adc, mock_run, mock_bill, mock_apis):
        """Checks 2-4 skip when state is fresh, non-failed, and only_stage=None."""
        fresh_ts = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        with patch("scripts.diagnosis.load_state", return_value={
            "timestamp": fresh_ts, "deployment_state": "deployed"
        }):
            from scripts.diagnosis import run_pre_apply_checks
            result = run_pre_apply_checks(_make_config(), only_stage=None)
        # Checks 2-4 skipped — billing and API mocks should NOT have been called
        mock_bill.assert_not_called()
        mock_apis.assert_not_called()
        assert result is True

    @patch("scripts.diagnosis.check_required_apis", return_value=[])
    @patch("scripts.diagnosis.check_billing_tristate", return_value="enabled")
    @patch("scripts.diagnosis.run_cmd")
    @patch("scripts.diagnosis.check_application_default_credentials", return_value=(True, "u@e.com"))
    def test_no_skip_when_state_failed(self, mock_adc, mock_run, mock_bill, mock_apis):
        fresh_ts = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        mock_run.return_value = MagicMock(returncode=0, stdout="my-project")
        with patch("scripts.diagnosis.load_state", return_value={
            "timestamp": fresh_ts, "deployment_state": "failed"
        }):
            from scripts.diagnosis import run_pre_apply_checks
            run_pre_apply_checks(_make_config(), only_stage=None)
        mock_bill.assert_called()  # checks 2-4 ran

    @patch("scripts.diagnosis.check_required_apis", return_value=[])
    @patch("scripts.diagnosis.check_billing_tristate", return_value="enabled")
    @patch("scripts.diagnosis.run_cmd")
    @patch("scripts.diagnosis.check_application_default_credentials", return_value=(True, "u@e.com"))
    def test_no_skip_when_only_stage_set(self, mock_adc, mock_run, mock_bill, mock_apis):
        fresh_ts = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        mock_run.return_value = MagicMock(returncode=0, stdout="my-project")
        with patch("scripts.diagnosis.load_state", return_value={
            "timestamp": fresh_ts, "deployment_state": "deployed"
        }):
            from scripts.diagnosis import run_pre_apply_checks
            run_pre_apply_checks(_make_config(), only_stage="k8s_apply")
        mock_bill.assert_called()  # skip disabled by only_stage

    @patch("scripts.diagnosis.check_required_apis", return_value=[])
    @patch("scripts.diagnosis.check_billing_tristate", return_value="enabled")
    @patch("scripts.diagnosis.run_cmd")
    @patch("scripts.diagnosis.check_application_default_credentials", return_value=(True, "u@e.com"))
    def test_no_skip_when_timestamp_absent(self, mock_adc, mock_run, mock_bill, mock_apis):
        mock_run.return_value = MagicMock(returncode=0, stdout="my-project")
        with patch("scripts.diagnosis.load_state", return_value={"deployment_state": "deployed"}):
            from scripts.diagnosis import run_pre_apply_checks
            run_pre_apply_checks(_make_config(), only_stage=None)
        mock_bill.assert_called()  # no timestamp → treat as stale
```

**Step 2: Run tests to confirm they fail**

```bash
pytest tests/test_diagnosis.py::TestRunPreApplyChecks -v
```

Expected: ImportError (run_pre_apply_checks not yet defined)

**Step 3: Add run_pre_apply_checks to scripts/diagnosis.py**

Add these imports at the top of `scripts/diagnosis.py`:

```python
from datetime import datetime, timezone, timedelta

from scripts.gcp import check_billing_tristate, check_quotas, check_required_apis
from scripts.pipeline import load_state
```

Add the function after `offer_adc_setup`:

```python
def run_pre_apply_checks(
    config: InstallerConfig,
    auto_approve: bool = False,
    only_stage: str | None = None,
) -> bool:
    """Run pre-apply health checks. Returns True if deployment may proceed.

    Checks (in order): ADC, project access, billing, required APIs.
    Checks 2-4 are skipped if state is fresh (<24h), not failed, and
    only_stage is None — to avoid redundant GCP calls on resume.
    """
    from scripts.config import InstallerConfig  # avoid circular at module level

    project_id = config.get("gcp_project_id")

    # Check 1: ADC (always runs)
    valid, _ = check_application_default_credentials()
    if not valid:
        refreshed = offer_adc_setup(auto_accept=auto_approve)
        if not refreshed:
            return False

    # Timestamp-based skip for checks 2-4
    if only_stage is None:
        state = load_state()
        ts_str = state.get("timestamp")
        deploy_state = state.get("deployment_state", "")
        if ts_str and deploy_state != "failed":
            try:
                ts = datetime.fromisoformat(ts_str)
                if datetime.now(timezone.utc) - ts < timedelta(hours=24):
                    return True  # checks 2-4 skipped
            except (ValueError, TypeError):
                pass

    # Check 2: project accessible
    result = run_cmd(["gcloud", "projects", "describe", project_id, "--format=value(projectId)"])
    if result.returncode != 0:
        print_error(f"Cannot access project '{project_id}'. Check project ID and IAM permissions.")
        return False

    # Check 3: billing
    billing = check_billing_tristate(project_id)
    if billing == "disabled":
        print_error(f"Billing is disabled on project '{project_id}'.")
        print_fix("Enable billing", [f"https://console.cloud.google.com/billing/linkedaccount?project={project_id}"])
        return False
    # "unknown" → probe failed, skip hint, continue to check 4

    # Check 4: required APIs
    missing = check_required_apis(project_id)
    if missing:
        apis_str = " ".join(missing)
        print_error("Required GCP APIs are not enabled.")
        print_fix("Enable APIs", [f"gcloud services enable {apis_str} --project {project_id}"])
        return False

    return True
```

**Step 4: Run tests**

```bash
pytest tests/test_diagnosis.py::TestRunPreApplyChecks -v
```

Expected: all PASS

**Step 5: Commit**

```bash
git add scripts/diagnosis.py tests/test_diagnosis.py
git commit -m "feat: add run_pre_apply_checks with 4-check health gate and timestamp skip"
```

---

## Task 4c: Add diagnose_stage_failure to scripts/diagnosis.py

**Step 1: Write failing tests**

Add to `tests/test_diagnosis.py`:

```python
class TestDiagnoseStageFailure:
    def _make_cfg(self, project="proj", region="us-central1", zone="us-central1-a"):
        cfg = MagicMock()
        cfg.get.side_effect = lambda k, *a: {
            "gcp_project_id": project, "region": region, "zone": zone
        }.get(k, a[0] if a else None)
        return cfg

    @patch("scripts.diagnosis.check_application_default_credentials", return_value=(False, None))
    def test_adc_invalid_returns_adc_hint_only(self, mock_adc):
        from scripts.diagnosis import diagnose_stage_failure
        hints = diagnose_stage_failure(self._make_cfg(), "terraform_init")
        assert any("gcloud auth application-default login" in h for h in hints)
        assert len(hints) == 1  # ADC guard returns immediately

    @patch("scripts.diagnosis.check_required_apis", return_value=[])
    @patch("scripts.diagnosis.check_billing_tristate", return_value="disabled")
    @patch("scripts.diagnosis.run_cmd")
    @patch("scripts.diagnosis.check_application_default_credentials", return_value=(True, "u@e.com"))
    def test_billing_disabled_adds_hint(self, mock_adc, mock_run, mock_bill, mock_apis):
        mock_run.return_value = MagicMock(returncode=0, stdout="")
        from scripts.diagnosis import diagnose_stage_failure
        hints = diagnose_stage_failure(self._make_cfg(), "terraform_init")
        assert any("billing" in h.lower() for h in hints)

    @patch("scripts.diagnosis.check_required_apis", return_value=[])
    @patch("scripts.diagnosis.check_billing_tristate", return_value="unknown")
    @patch("scripts.diagnosis.run_cmd")
    @patch("scripts.diagnosis.check_application_default_credentials", return_value=(True, "u@e.com"))
    def test_billing_unknown_no_billing_hint(self, mock_adc, mock_run, mock_bill, mock_apis):
        mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="no bucket")
        from scripts.diagnosis import diagnose_stage_failure
        hints = diagnose_stage_failure(self._make_cfg(), "terraform_init")
        assert not any("billing" in h.lower() for h in hints)

    @patch("scripts.diagnosis.check_required_apis", return_value=[])
    @patch("scripts.diagnosis.check_billing_tristate", return_value="enabled")
    @patch("scripts.diagnosis.run_cmd")
    @patch("scripts.diagnosis.check_application_default_credentials", return_value=(True, "u@e.com"))
    def test_ansible_run_no_vms(self, mock_adc, mock_run, mock_bill, mock_apis):
        mock_run.return_value = MagicMock(returncode=0, stdout="")  # empty = no VMs
        from scripts.diagnosis import diagnose_stage_failure
        hints = diagnose_stage_failure(self._make_cfg(), "ansible_run")
        assert any("terraform_apply" in h for h in hints)

    @patch("scripts.diagnosis.check_required_apis", return_value=[])
    @patch("scripts.diagnosis.check_billing_tristate", return_value="enabled")
    @patch("scripts.diagnosis.run_cmd")
    @patch("scripts.diagnosis.check_application_default_credentials", return_value=(True, "u@e.com"))
    def test_ansible_run_vm_filter_uses_labels(self, mock_adc, mock_run, mock_bill, mock_apis):
        mock_run.return_value = MagicMock(returncode=0, stdout="")
        from scripts.diagnosis import diagnose_stage_failure
        diagnose_stage_failure(self._make_cfg(), "ansible_run")
        # Verify the command used labels.env=voipbin, NOT tags.items=voipbin
        call_args = str(mock_run.call_args)
        assert "labels.env=voipbin" in call_args
        assert "tags.items" not in call_args

    @patch("scripts.diagnosis.check_required_apis", return_value=[])
    @patch("scripts.diagnosis.check_billing_tristate", return_value="enabled")
    @patch("scripts.diagnosis.run_cmd")
    @patch("scripts.diagnosis.check_application_default_credentials", return_value=(True, "u@e.com"))
    def test_k8s_apply_describe_uses_zone(self, mock_adc, mock_run, mock_bill, mock_apis):
        mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="not found")
        from scripts.diagnosis import diagnose_stage_failure
        hints = diagnose_stage_failure(self._make_cfg(zone="us-central1-a"), "k8s_apply")
        call_args = str(mock_run.call_args)
        assert "--zone" in call_args
        assert "us-central1-a" in call_args

    @patch("scripts.diagnosis.check_required_apis", return_value=[])
    @patch("scripts.diagnosis.check_billing_tristate", return_value="enabled")
    @patch("scripts.diagnosis.check_quotas")
    @patch("scripts.diagnosis.run_cmd")
    @patch("scripts.diagnosis.check_application_default_credentials", return_value=(True, "u@e.com"))
    def test_quota_hints_formatted_correctly(self, mock_adc, mock_run, mock_quotas, mock_bill, mock_apis):
        from scripts.gcp import QuotaResult
        mock_quotas.return_value = [
            QuotaResult(metric="CPUS", available=2, required=8, ok=False, description="vCPUs"),
        ]
        mock_run.return_value = MagicMock(returncode=0, stdout="")
        from scripts.diagnosis import diagnose_stage_failure
        hints = diagnose_stage_failure(self._make_cfg(), "terraform_apply")
        assert any("CPUS" in h and "2" in h and "8" in h for h in hints)
```

**Step 2: Run tests to confirm they fail**

```bash
pytest tests/test_diagnosis.py::TestDiagnoseStageFailure -v
```

Expected: ImportError

**Step 3: Add diagnose_stage_failure to scripts/diagnosis.py**

```python
def diagnose_stage_failure(config: InstallerConfig, stage: str) -> list[str]:
    """Probe GCP state after a stage failure. Returns list of hint strings.

    ADC-first guard: if ADC is invalid, return immediately with one hint.
    Do NOT probe billing or other GCP resources when ADC is invalid — they
    would all fail with PERMISSION_DENIED and generate false positives.
    """
    project_id = config.get("gcp_project_id")
    region = config.get("region")
    zone = config.get("zone")
    cluster_name = "voipbin-gke-cluster"  # hardcoded — matches gke.tf var.env default

    hints: list[str] = []

    # ADC-first guard
    valid, _ = check_application_default_credentials()
    if not valid:
        hints.append(
            "Likely cause: Application Default Credentials expired → Fix: "
            "gcloud auth application-default login"
        )
        return hints

    # Billing (all stages)
    billing = check_billing_tristate(project_id)
    if billing == "disabled":
        hints.append(
            f"Likely cause: billing disabled on project '{project_id}' → Fix: "
            f"https://console.cloud.google.com/billing/linkedaccount?project={project_id}"
        )

    # Stage-specific checks
    if stage in ("terraform_init", "terraform_reconcile", "terraform_apply"):
        # GCS bucket
        bucket = f"gs://{project_id}-voipbin-tf-state"
        r = run_cmd(["gcloud", "storage", "ls", bucket, f"--project={project_id}"])
        if r.returncode != 0:
            hints.append(
                f"Likely cause: state bucket does not exist → Fix: "
                f"gcloud storage buckets create {bucket} --project={project_id}"
            )

        # APIs
        missing = check_required_apis(project_id)
        if missing:
            hints.append(
                f"Likely cause: required APIs not enabled ({', '.join(missing)}) → Fix: "
                f"gcloud services enable {' '.join(missing)} --project {project_id}"
            )

        # Quotas (for apply/reconcile)
        if stage in ("terraform_reconcile", "terraform_apply"):
            for q in check_quotas(project_id, region):
                if not q.ok:
                    hints.append(
                        f"Likely cause: insufficient {q.metric} quota "
                        f"({q.available:.0f} available, {q.required:.0f} required) → Fix: "
                        f"https://console.cloud.google.com/iam-admin/quotas?project={project_id}"
                    )

    elif stage == "ansible_run":
        r = run_cmd([
            "gcloud", "compute", "instances", "list",
            f"--project={project_id}",
            '--filter=labels.env=voipbin',
            "--format=value(name,status)",
        ])
        if r.returncode == 0:
            lines = [ln.strip() for ln in r.stdout.splitlines() if ln.strip()]
            if not lines:
                hints.append(
                    "Likely cause: VMs not yet created — terraform_apply stage may not have completed → Fix: "
                    "voipbin-install apply --stage terraform_apply"
                )
            else:
                statuses = [ln.split("\t")[-1] for ln in lines]
                if not any(s == "RUNNING" for s in statuses):
                    hints.append(
                        "Likely cause: VMs may still be booting → Fix: "
                        "wait 2 minutes and re-run: voipbin-install apply"
                    )
                if any(s == "TERMINATED" for s in statuses):
                    hints.append(
                        f"Likely cause: one or more VMs stopped unexpectedly → Fix: "
                        f"https://console.cloud.google.com/compute/instances?project={project_id}"
                    )

    elif stage == "k8s_apply":
        r = run_cmd([
            "gcloud", "container", "clusters", "describe", cluster_name,
            f"--project={project_id}",
            f"--zone={zone}",
            "--format=value(status)",
        ])
        if r.returncode != 0:
            hints.append(
                "Likely cause: GKE cluster not found — terraform_apply stage may not have completed → Fix: "
                "voipbin-install apply --stage terraform_apply"
            )
        elif "PROVISIONING" in r.stdout.upper():
            hints.append(
                "Likely cause: GKE cluster still provisioning → Fix: "
                "wait 5 minutes and re-run: voipbin-install apply"
            )

    return hints
```

**Step 4: Run tests**

```bash
pytest tests/test_diagnosis.py::TestDiagnoseStageFailure -v
```

Expected: all PASS

**Step 5: Commit**

```bash
git add scripts/diagnosis.py tests/test_diagnosis.py
git commit -m "feat: add diagnose_stage_failure with stage-specific GCP probes"
```

---

## Task 4d: Add OS-aware install hints to scripts/diagnosis.py

**Step 1: Write failing tests**

Add to `tests/test_diagnosis.py`:

```python
class TestDetectOs:
    def test_darwin_returns_macos(self):
        with patch("platform.system", return_value="Darwin"):
            from scripts.diagnosis import _detect_os
            assert _detect_os() == "macos"

    def test_ubuntu_quoted_id(self, tmp_path):
        os_release = tmp_path / "os-release"
        os_release.write_text('ID="ubuntu"\nVERSION_ID="22.04"\n')
        with patch("platform.system", return_value="Linux"), \
             patch("builtins.open", return_value=open(os_release)):
            from scripts.diagnosis import _detect_os
            assert _detect_os() == "debian"

    def test_rhel_id(self, tmp_path):
        os_release = tmp_path / "os-release"
        os_release.write_text('ID=rhel\n')
        with patch("platform.system", return_value="Linux"), \
             patch("builtins.open", return_value=open(os_release)):
            from scripts.diagnosis import _detect_os
            assert _detect_os() == "rhel"

    def test_missing_os_release_returns_linux(self):
        with patch("platform.system", return_value="Linux"), \
             patch("builtins.open", side_effect=OSError):
            from scripts.diagnosis import _detect_os
            assert _detect_os() == "linux"


class TestGetOsInstallHint:
    def test_ansible_all_platforms_auto_run(self):
        from scripts.diagnosis import get_os_install_hint
        for os_name in ("macos", "debian", "rhel", "linux"):
            with patch("scripts.diagnosis._detect_os", return_value=os_name):
                steps, can_auto = get_os_install_hint("ansible")
            assert can_auto is True
            assert any("pip3" in s for s in steps)

    def test_gcloud_linux_display_only(self):
        from scripts.diagnosis import get_os_install_hint
        with patch("scripts.diagnosis._detect_os", return_value="debian"):
            steps, can_auto = get_os_install_hint("gcloud")
        assert can_auto is False

    def test_gcloud_macos_auto_run(self):
        from scripts.diagnosis import get_os_install_hint
        with patch("scripts.diagnosis._detect_os", return_value="macos"):
            steps, can_auto = get_os_install_hint("gcloud")
        assert can_auto is True

    def test_sops_linux_display_only(self):
        from scripts.diagnosis import get_os_install_hint
        with patch("scripts.diagnosis._detect_os", return_value="debian"):
            _, can_auto = get_os_install_hint("sops")
        assert can_auto is False

    def test_sops_macos_auto_run(self):
        from scripts.diagnosis import get_os_install_hint
        with patch("scripts.diagnosis._detect_os", return_value="macos"):
            _, can_auto = get_os_install_hint("sops")
        assert can_auto is True

    def test_kubectl_all_auto_run(self):
        from scripts.diagnosis import get_os_install_hint
        with patch("scripts.diagnosis._detect_os", return_value="debian"):
            _, can_auto = get_os_install_hint("kubectl")
        assert can_auto is True


class TestOfferToolInstall:
    @patch("scripts.diagnosis.check_tool_exists", return_value=True)
    @patch("scripts.diagnosis.run_cmd")
    @patch("scripts.diagnosis.confirm", return_value=True)
    @patch("scripts.diagnosis.print_fix")
    def test_auto_run_success_returns_true(self, mock_fix, mock_confirm, mock_run, mock_check):
        mock_run.return_value = MagicMock(returncode=0)
        with patch("scripts.diagnosis._detect_os", return_value="macos"), \
             patch("scripts.diagnosis.get_os_install_hint", return_value=(["brew install sops"], True)):
            from scripts.diagnosis import offer_tool_install
            assert offer_tool_install("sops") is True

    @patch("scripts.diagnosis.check_tool_exists", return_value=False)
    @patch("scripts.diagnosis.run_cmd")
    @patch("scripts.diagnosis.confirm", return_value=True)
    @patch("scripts.diagnosis.print_fix")
    @patch("scripts.diagnosis.print_error")
    def test_auto_run_path_not_updated_returns_false(
        self, mock_err, mock_fix, mock_confirm, mock_run, mock_check
    ):
        mock_run.return_value = MagicMock(returncode=0)
        with patch("scripts.diagnosis.get_os_install_hint", return_value=(["brew install x"], True)):
            from scripts.diagnosis import offer_tool_install
            assert offer_tool_install("x") is False
            mock_err.assert_called()  # shell-restart message

    @patch("scripts.diagnosis.print_fix")
    @patch("scripts.diagnosis.print_error")
    def test_display_only_returns_false(self, mock_err, mock_fix):
        with patch("scripts.diagnosis.get_os_install_hint",
                   return_value=(["https://example.com"], False)):
            from scripts.diagnosis import offer_tool_install
            assert offer_tool_install("gcloud") is False
```

**Step 2: Run tests to confirm they fail**

```bash
pytest tests/test_diagnosis.py::TestDetectOs tests/test_diagnosis.py::TestGetOsInstallHint tests/test_diagnosis.py::TestOfferToolInstall -v
```

**Step 3: Add OS-aware functions to scripts/diagnosis.py**

Add these imports at the top of `scripts/diagnosis.py`:
```python
import platform
from scripts.utils import check_tool_exists
```

Add after `diagnose_stage_failure`:

```python
# ---------------------------------------------------------------------------
# 4. OS-Aware Install Hints
# ---------------------------------------------------------------------------

def _detect_os() -> str:
    """Detect the current OS. Returns: macos, debian, rhel, fedora, arch, linux."""
    if platform.system() == "Darwin":
        return "macos"
    try:
        with open("/etc/os-release") as f:
            for line in f:
                if line.startswith("ID="):
                    val = line.split("=", 1)[1].strip().strip('"').lower()
                    if val in ("ubuntu", "debian"):
                        return "debian"
                    if val in ("rhel", "centos", "rocky", "almalinux"):
                        return "rhel"
                    if val == "fedora":
                        return "fedora"
                    if val == "arch":
                        return "arch"
    except OSError:
        pass
    return "linux"


_INSTALL_HINTS: dict[str, dict[str, tuple[list[str], bool]]] = {
    "gcloud": {
        "macos":  (["brew install --cask google-cloud-sdk"], True),
        "debian": (["curl https://sdk.cloud.google.com | bash"], False),
        "rhel":   ([
            "sudo tee /etc/yum.repos.d/google-cloud-sdk.repo << 'EOM'\n"
            "[google-cloud-cli]\nname=Google Cloud CLI\n"
            "baseurl=https://packages.cloud.google.com/yum/repos/cloud-sdk-el9-x86_64\n"
            "enabled=1\ngpgcheck=1\nrepo_gpgcheck=0\n"
            "gpgkey=https://packages.cloud.google.com/yum/doc/rpm-package-key.gpg\nEOM",
            "sudo dnf install -y google-cloud-cli",
        ], False),
        "linux":  (["curl https://sdk.cloud.google.com | bash"], False),
    },
    "terraform": {
        "macos":  (["brew tap hashicorp/tap", "brew install hashicorp/tap/terraform"], True),
        "debian": ([
            "wget -O- https://apt.releases.hashicorp.com/gpg | sudo gpg --dearmor -o /usr/share/keyrings/hashicorp-archive-keyring.gpg",
            'echo "deb [signed-by=/usr/share/keyrings/hashicorp-archive-keyring.gpg] https://apt.releases.hashicorp.com $(lsb_release -cs) main" | sudo tee /etc/apt/sources.list.d/hashicorp.list',
            "sudo apt update && sudo apt install -y terraform",
        ], False),
        "rhel":   ([
            "sudo yum install -y yum-utils",
            "sudo yum-config-manager --add-repo https://rpm.releases.hashicorp.com/RHEL/hashicorp.repo",
            "sudo yum install -y terraform",
        ], False),
        "linux":  (["https://developer.hashicorp.com/terraform/downloads"], False),
    },
    "ansible": {
        "macos":  (["pip3 install ansible"], True),
        "debian": (["pip3 install ansible"], True),
        "rhel":   (["pip3 install ansible"], True),
        "linux":  (["pip3 install ansible"], True),
    },
    "kubectl": {
        "macos":  (["brew install kubectl"], True),
        "debian": (["gcloud components install kubectl"], True),
        "rhel":   (["gcloud components install kubectl"], True),
        "linux":  (["gcloud components install kubectl"], True),
    },
    "sops": {
        "macos":  (["brew install sops"], True),
        "debian": (["https://github.com/getsops/sops/releases/latest"], False),
        "rhel":   (["https://github.com/getsops/sops/releases/latest"], False),
        "linux":  (["https://github.com/getsops/sops/releases/latest"], False),
    },
    "python3": {
        "macos":  (["https://www.python.org/downloads/"], False),
        "debian": (["https://www.python.org/downloads/"], False),
        "rhel":   (["https://www.python.org/downloads/"], False),
        "linux":  (["https://www.python.org/downloads/"], False),
    },
}


def get_os_install_hint(tool: str) -> tuple[list[str], bool]:
    """Return (install_steps, can_auto_run) for the given tool on the current OS."""
    os_name = _detect_os()
    tool_hints = _INSTALL_HINTS.get(tool, {})
    steps, can_auto = tool_hints.get(os_name, tool_hints.get("linux", ([], False)))
    return steps, can_auto


def offer_tool_install(tool: str) -> bool:
    """Print install hints and optionally run them. Returns True only if tool is now on PATH."""
    steps, can_auto = get_os_install_hint(tool)
    print_fix(f"Install {tool}", steps)

    if not can_auto:
        print_error(f"Run the commands above in your terminal, then re-run: voipbin-install init")
        return False

    if not confirm(f"Install {tool} now?", default=True):
        return False

    for step in steps:
        result = run_cmd(step, capture=False)
        if result.returncode != 0:
            print_error(f"Install step failed: {step}")
            return False

    if check_tool_exists(tool):
        return True

    print_error(
        "Installation may require restarting your shell. "
        "Open a new terminal, then re-run: voipbin-install init"
    )
    return False
```

**Step 4: Run all diagnosis tests**

```bash
pytest tests/test_diagnosis.py -v
```

Expected: all PASS

**Step 5: Commit**

```bash
git add scripts/diagnosis.py tests/test_diagnosis.py
git commit -m "feat: add OS-aware install hints and offer_tool_install"
```

---

## Task 5: Wire preflight.py — OS hints in run_preflight_display + ADC imports

**Files:**
- Modify: `scripts/preflight.py`
- Test: `tests/test_preflight.py`

**Step 1: Write failing tests**

Add to `tests/test_preflight.py`:

```python
class TestRunPreflightDisplayOsHints:
    @patch("scripts.preflight.get_os_install_hint", return_value=(["pip3 install ansible"], True))
    @patch("scripts.preflight.print_check")
    @patch("scripts.preflight.print_error")
    def test_shows_os_hint_for_missing_tool(self, mock_err, mock_check, mock_hint):
        from scripts.preflight import PreflightResult, run_preflight_display
        results = [PreflightResult(
            tool="ansible", version="", ok=False, required="2.15.0",
            hint="pip install ansible"
        )]
        run_preflight_display(results)
        # Should show pip3 hint (from get_os_install_hint), not the old r.hint URL
        call_args = " ".join(str(a) for a in mock_err.call_args_list)
        assert "pip3 install ansible" in call_args
```

**Step 2: Run to confirm fail**

```bash
pytest tests/test_preflight.py::TestRunPreflightDisplayOsHints -v
```

**Step 3: Update scripts/preflight.py**

At the top of `scripts/preflight.py`, add to the existing imports block:

```python
from scripts.diagnosis import (
    check_application_default_credentials,
    get_os_install_hint,
    offer_adc_setup,
)
```

Update `run_preflight_display` to use OS-aware hints:

```python
def run_preflight_display(results: list[PreflightResult]) -> bool:
    """Display preflight results. Returns True if all passed."""
    print_header("Checking prerequisites...")
    all_ok = True
    for r in results:
        print_check(r.tool, r.version, r.ok, r.required)
        if not r.ok:
            all_ok = False
            # Show OS-aware first step as a short inline hint
            steps, can_auto = get_os_install_hint(r.tool)
            if steps:
                hint_line = steps[0] if len(steps) == 1 or can_auto else steps[-1]
            else:
                hint_line = r.hint
            print_error(f"  Install: {hint_line}")
    return all_ok
```

**Step 4: Run preflight tests**

```bash
pytest tests/test_preflight.py -v
```

Expected: all PASS

**Step 5: Commit**

```bash
git add scripts/preflight.py tests/test_preflight.py
git commit -m "feat: wire OS-aware install hints into run_preflight_display"
```

---

## Task 6: Wire init.py — two-pass prereq loop + ADC check

**Files:**
- Modify: `scripts/commands/init.py`
- Test: `tests/test_init.py` (create if not exists; check manually since init is integration-heavy)

**Step 1: Add imports to init.py**

At the top of `scripts/commands/init.py`, add to existing imports:

```python
from scripts.diagnosis import (
    check_application_default_credentials,
    get_os_install_hint,
    offer_adc_setup,
    offer_tool_install,
)
```

Add a module-level helper (after imports, before `_count_gcp_apis`):

```python
def _can_auto_run(tool: str) -> bool:
    _, can_auto = get_os_install_hint(tool)
    return can_auto
```

**Step 2: Replace prerequisite failure handling in cmd_init (lines 72-75)**

Replace:
```python
all_ok = run_preflight_display(results)
if not all_ok:
    print_error("Some prerequisites are missing. Install them and re-run.")
    sys.exit(1)
```

With:
```python
all_ok = run_preflight_display(results)
if not all_ok:
    # Pass 1: auto-installable tools — attempt each in sequence
    for r in results:
        if not r.ok and _can_auto_run(r.tool):
            installed = offer_tool_install(r.tool)
            if not installed:
                sys.exit(1)

    # Pass 2: display-only tools — show ALL hints before exiting
    display_only_missing = [r for r in results if not r.ok and not _can_auto_run(r.tool)]
    if display_only_missing:
        for r in display_only_missing:
            offer_tool_install(r.tool)  # always returns False; prints print_fix block
        sys.exit(1)
    # All tools now installed — continue
```

**Step 3: Add ADC check after GCP auth check (after line 83 `print_success(f"Authenticated as {account}")`)**

```python
    # Check ADC (separate from gcloud user auth)
    adc_ok, _ = check_application_default_credentials()
    if not adc_ok:
        refreshed = offer_adc_setup()
        if not refreshed:
            sys.exit(1)
    print_success("Application Default Credentials valid")
```

**Step 4: Verify the file looks correct**

```bash
cd /home/pchero/gitvoipbin/install/.worktrees/NOJIRA-First-time-user-hardening
python3 -c "from scripts.commands.init import cmd_init; print('import OK')"
```

Expected: `import OK`

**Step 5: Run existing tests (make sure nothing broke)**

```bash
pytest tests/ -v -x --ignore=tests/test_diagnosis.py 2>&1 | tail -20
```

Expected: all existing tests PASS

**Step 6: Commit**

```bash
git add scripts/commands/init.py
git commit -m "feat: add two-pass prereq loop and ADC check to cmd_init"
```

---

## Task 7: Wire apply.py — pre-apply checks

**Files:**
- Modify: `scripts/commands/apply.py`
- Test: existing `tests/test_pipeline.py` indirectly covers this

**Step 1: Add import to apply.py**

At the top of `scripts/commands/apply.py`, add:

```python
from scripts.diagnosis import run_pre_apply_checks
```

**Step 2: Add pre-apply checks after confirmation prompt (after the `if not auto_approve and not dry_run:` block, before `run_pipeline`)**

In `cmd_apply`, the current code at lines 94-105:
```python
    # Confirm
    if not auto_approve and not dry_run:
        if not confirm("Proceed with deployment?", default=True):
            console.print("  Cancelled.")
            return

    # Run pipeline
    ok = run_pipeline(...)
```

Change to:
```python
    # Confirm
    if not auto_approve and not dry_run:
        if not confirm("Proceed with deployment?", default=True):
            console.print("  Cancelled.")
            return

    # Pre-apply health checks (after confirmation — checks take ~10s)
    if not dry_run:
        if not run_pre_apply_checks(config, auto_approve=auto_approve, only_stage=stage):
            print_error("Pre-apply checks failed. Fix the issues above and re-run.")
            sys.exit(1)

    # Run pipeline
    ok = run_pipeline(...)
```

**Step 3: Verify import works**

```bash
python3 -c "from scripts.commands.apply import cmd_apply; print('import OK')"
```

Expected: `import OK`

**Step 4: Run tests**

```bash
pytest tests/ -v -x 2>&1 | tail -20
```

Expected: all PASS

**Step 5: Commit**

```bash
git add scripts/commands/apply.py
git commit -m "feat: add pre-apply health checks to cmd_apply"
```

---

## Task 8: Wire destroy.py — ADC check

**Files:**
- Modify: `scripts/commands/destroy.py`

**Step 1: Add imports**

At the top of `scripts/commands/destroy.py`, add:

```python
from scripts.diagnosis import check_application_default_credentials, offer_adc_setup
```

**Step 2: Add ADC check after config.load() (after line 33, before project_id lookup)**

```python
    config.load()

    # ADC check — terraform destroy requires valid ADC credentials
    adc_ok, _ = check_application_default_credentials()
    if not adc_ok:
        refreshed = offer_adc_setup(auto_accept=auto_approve)
        if not refreshed:
            print_error("Terraform destroy requires Application Default Credentials.")
            sys.exit(1)

    project_id = config.get("gcp_project_id", "unknown")
```

**Step 3: Verify import**

```bash
python3 -c "from scripts.commands.destroy import cmd_destroy; print('import OK')"
```

**Step 4: Run tests**

```bash
pytest tests/ -v -x 2>&1 | tail -20
```

**Step 5: Commit**

```bash
git add scripts/commands/destroy.py
git commit -m "feat: add ADC check to cmd_destroy before terraform destroy"
```

---

## Task 9: Wire pipeline.py — post-failure diagnosis

**Files:**
- Modify: `scripts/pipeline.py`
- Test: `tests/test_pipeline.py`

**Step 1: Write failing tests**

Add to `tests/test_pipeline.py`:

```python
class TestPipelineDiagnosis:
    @patch("scripts.pipeline.diagnose_stage_failure", return_value=["hint1"])
    @patch("scripts.pipeline.print_fix")
    @patch("scripts.pipeline.STAGE_RUNNERS", {"terraform_init": MagicMock(return_value=False)})
    def test_diagnosis_called_on_failure(self, mock_fix, mock_diag):
        """After a stage fails, diagnose_stage_failure is called and print_fix renders hints."""
        from scripts.pipeline import run_pipeline
        from unittest.mock import MagicMock
        config = MagicMock()
        config.get.return_value = "my-project"
        with patch("scripts.pipeline.load_state", return_value={}), \
             patch("scripts.pipeline.save_state"):
            run_pipeline(config, only_stage="terraform_init")
        mock_diag.assert_called_once_with(config, "terraform_init")
        mock_fix.assert_called_once_with("Likely causes", ["hint1"])

    @patch("scripts.pipeline.diagnose_stage_failure")
    @patch("scripts.pipeline.STAGE_RUNNERS", {"terraform_init": MagicMock(return_value=True)})
    def test_no_diagnosis_on_success(self, mock_diag):
        from scripts.pipeline import run_pipeline
        from unittest.mock import MagicMock
        config = MagicMock()
        with patch("scripts.pipeline.load_state", return_value={}), \
             patch("scripts.pipeline.save_state"):
            run_pipeline(config, only_stage="terraform_init")
        mock_diag.assert_not_called()
```

**Step 2: Run to confirm fail**

```bash
pytest tests/test_pipeline.py::TestPipelineDiagnosis -v
```

**Step 3: Add imports to scripts/pipeline.py**

Add to the existing import block at the top of `scripts/pipeline.py`:

```python
from scripts.diagnosis import diagnose_stage_failure
from scripts.display import print_fix   # add print_fix to existing display import line
```

(Add `print_fix` to the existing `from scripts.display import (...)` block on lines 11-18.)

**Step 4: Update failure branch in run_pipeline (lines 217-224)**

Replace:
```python
        if not ok:
            stages[stage_name] = "failed"
            state["deployment_state"] = "failed"
            state["stages"] = stages
            save_state(state)
            print_error(f"Stage '{label}' failed. Pipeline halted.")
            print_step("Resume with: [bold]voipbin-install apply[/bold]")
            return False
```

With:
```python
        if not ok:
            stages[stage_name] = "failed"
            state["deployment_state"] = "failed"
            state["stages"] = stages
            save_state(state)
            print_error(f"Stage '{label}' failed. Pipeline halted.")

            hints = diagnose_stage_failure(config, stage_name)
            if hints:
                print_fix("Likely causes", hints)

            print_step("Resume with: [bold]voipbin-install apply[/bold]")
            return False
```

**Step 5: Run tests**

```bash
pytest tests/test_pipeline.py -v
```

Expected: all PASS

**Step 6: Commit**

```bash
git add scripts/pipeline.py tests/test_pipeline.py
git commit -m "feat: add post-failure GCP diagnosis to pipeline.py"
```

---

## Task 10: Fix cli.py — add terraform_reconcile to --stage choices

**Files:**
- Modify: `scripts/cli.py:47`

**Step 1: Update the click.Choice for --stage**

In `scripts/cli.py`, line 47, replace:

```python
@click.option("--stage", type=click.Choice(["terraform_init", "terraform_apply", "ansible_run", "k8s_apply"]), default=None, help="Run only a specific pipeline stage")
```

With:

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

**Step 2: Verify the CLI shows the new option**

```bash
python3 scripts/cli.py apply --help
```

Expected output includes: `terraform_reconcile` in the stage choices list

**Step 3: Run tests**

```bash
pytest tests/ -v -x 2>&1 | tail -20
```

**Step 4: Commit**

```bash
git add scripts/cli.py
git commit -m "fix: add terraform_reconcile to --stage CLI choices (regression)"
```

---

## Task 11: Fix secretmgr.py — log decrypt errors with file path

**Files:**
- Modify: `scripts/secretmgr.py`
- Test: `tests/test_secretmgr.py`

**Step 1: Write failing tests**

Add to `tests/test_secretmgr.py`:

```python
class TestDecryptWithSopsErrors:
    @patch("scripts.secretmgr.print_error")
    @patch("scripts.secretmgr.run_cmd")
    def test_decrypt_failure_logs_file_path_and_stderr(self, mock_run, mock_err):
        mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="invalid_grant")
        from pathlib import Path
        from scripts.secretmgr import decrypt_with_sops
        result = decrypt_with_sops(Path("/config/secrets.yaml"))
        assert result is None
        # Must log the file path and the stderr
        all_calls = " ".join(str(c) for c in mock_err.call_args_list)
        assert "secrets.yaml" in all_calls
        assert "invalid_grant" in all_calls
```

**Step 2: Run to confirm fail**

```bash
pytest tests/test_secretmgr.py::TestDecryptWithSopsErrors -v
```

Expected: ImportError or assertion failure (currently no error logged)

**Step 3: Add import and update decrypt_with_sops in scripts/secretmgr.py**

Add import at top:
```python
from scripts.display import print_error
```

Replace the failure branch in `decrypt_with_sops` (lines 46-47):

```python
# OLD:
    if result.returncode != 0:
        return None
```

```python
# NEW:
    if result.returncode != 0:
        print_error(f"SOPS decryption failed for {encrypted_path}: {result.stderr.strip()}")
        print_error(
            "Ensure Application Default Credentials are valid and your account "
            "has roles/cloudkms.cryptoKeyDecrypter on the KMS key."
        )
        return None
```

**Step 4: Run tests**

```bash
pytest tests/test_secretmgr.py -v
```

Expected: all PASS

**Step 5: Run the full test suite**

```bash
pytest tests/ -v 2>&1 | tail -30
```

Expected: all tests PASS, no regressions

**Step 6: Final commit**

```bash
git add scripts/secretmgr.py tests/test_secretmgr.py
git commit -m "fix: log SOPS decrypt errors with file path and KMS hint"
```

---

## Final verification

```bash
# All tests pass
pytest tests/ -v

# Import check for the new module
python3 -c "import scripts.diagnosis; print('diagnosis OK')"

# CLI help shows terraform_reconcile
python3 scripts/cli.py apply --help | grep terraform_reconcile

# Static syntax check
python3 -m py_compile scripts/diagnosis.py scripts/gcp.py scripts/display.py \
  scripts/preflight.py scripts/commands/init.py scripts/commands/apply.py \
  scripts/commands/destroy.py scripts/pipeline.py scripts/cli.py scripts/secretmgr.py
```

---

## Summary of changes by file

| File | Change |
|------|--------|
| `scripts/utils.py` | Fix `_SAFE_ID_RE` regex — allow `[` and `]` |
| `scripts/gcp.py` | Add `check_billing_tristate`, `check_required_apis`, `REQUIRED_APIS`; surface SA errors |
| `scripts/display.py` | Add `print_fix(title, lines)` Panel helper |
| `scripts/diagnosis.py` | **New module** — ADC check/setup, pre-apply checks, failure diagnosis, OS install hints |
| `scripts/preflight.py` | Import from diagnosis; show OS hint in `run_preflight_display` |
| `scripts/commands/init.py` | Two-pass prereq loop; ADC check after GCP auth |
| `scripts/commands/apply.py` | Import `run_pre_apply_checks`; call before `run_pipeline` |
| `scripts/commands/destroy.py` | ADC check before destroy |
| `scripts/pipeline.py` | Import `diagnose_stage_failure`, `print_fix`; call after stage failure |
| `scripts/cli.py` | Add `terraform_reconcile` to `--stage` choices |
| `scripts/secretmgr.py` | Log decrypt errors with file path and KMS hint |
