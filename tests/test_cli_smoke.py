"""Actual-execution smoke tests added by PR-K3.

The historical mock-only test pattern caught neither GAP-33 (a real
`gcloud storage buckets create` flag rejected at runtime) nor GAP-34
(``confirm(...)`` called on a closed stdin under ``--auto-approve``).
These two tests exercise the real CLI / real stdin so future drift of
the same shape is caught before merge.

1. ``test_state_bucket_gcloud_flags_valid_syntax``
   For every ``gcloud …`` argv that ``ensure_state_bucket`` would invoke,
   shell out to ``gcloud <subcommand> --help`` and assert each ``--flag``
   the installer uses actually appears in gcloud's own help text.
   Skipped if ``gcloud`` is not on PATH.

2. ``test_reconcile_imports_auto_approve_no_stdin_read``
   Run ``_run_reconcile_imports`` with ``auto_approve=True`` and a closed
   stdin. Any forgotten ``auto_approve`` plumbing (the GAP-34 bug) lets
   ``confirm()`` reach ``input()`` and raises ``EOFError`` — the test
   would catch it.
"""

from __future__ import annotations

import io
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest


# ---------------------------------------------------------------------------
# Test 1 — gcloud help cross-check (GAP-33 shape)
# ---------------------------------------------------------------------------


class _FakeCompleted:
    def __init__(self, rc: int = 0, stdout: str = "", stderr: str = "") -> None:
        self.returncode = rc
        self.stdout = stdout
        self.stderr = stderr


class _BucketConfig:
    def __init__(self, data: dict[str, Any]) -> None:
        self._data = data

    def get(self, key: str, default: Any = None) -> Any:
        return self._data.get(key, default)


def _gcloud_help_text(subcommand_parts: list[str]) -> str:
    """Run ``gcloud <subcommand_parts> --help`` and return combined output.

    Caches at module level on first call per subcommand to keep the test
    well under a second per branch even when called many times.
    """
    if not hasattr(_gcloud_help_text, "_cache"):
        _gcloud_help_text._cache = {}  # type: ignore[attr-defined]
    cache: dict[tuple, str] = _gcloud_help_text._cache  # type: ignore[attr-defined]
    key = tuple(subcommand_parts)
    if key in cache:
        return cache[key]
    proc = subprocess.run(
        ["gcloud", *subcommand_parts, "--help"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    cache[key] = (proc.stdout or "") + "\n" + (proc.stderr or "")
    return cache[key]


def _capture_argvs(side_effect_fn) -> list[list[str]]:
    """Invoke ``ensure_state_bucket`` once with a patched ``run_cmd`` and
    return the list of argvs the function would have shelled out.

    ``side_effect_fn(call_index, argv)`` returns the synthetic
    ``CompletedProcess`` for that call. This lets one driver loop force
    each branch (create vs. already-exists vs. race).
    """
    captured: list[list[str]] = []

    def fake_run_cmd(cmd, *args, **kwargs):
        captured.append(list(cmd))
        return side_effect_fn(len(captured) - 1, cmd)

    from scripts import state_bucket as sb

    cfg = _BucketConfig({
        "gcp_project_id": "fake-project-123",
        "region": "us-central1",
        "state_bucket": "fake-project-123-tfstate",
    })
    with patch.object(sb, "run_cmd", side_effect=fake_run_cmd):
        sb.ensure_state_bucket(cfg)
    return captured


def test_state_bucket_gcloud_flags_valid_syntax() -> None:
    """Cross-check every gcloud flag in ``ensure_state_bucket`` against
    ``gcloud … --help``. Catches drift of the GAP-33 shape."""
    if shutil.which("gcloud") is None:
        pytest.skip("gcloud not on PATH — skipping CLI help cross-check")

    # Branch A: bucket does not exist → describe-fail, create-ok, update-ok
    argvs_a = _capture_argvs(
        lambda i, cmd: _FakeCompleted(rc=1, stderr="404 not found") if i == 0 else _FakeCompleted(rc=0)
    )
    # Branch B: bucket exists → describe-ok, update-ok
    argvs_b = _capture_argvs(lambda i, cmd: _FakeCompleted(rc=0))

    import re

    seen_subcommands: set[tuple] = set()
    failures: list[str] = []

    for argv in argvs_a + argvs_b:
        if not argv or argv[0] != "gcloud":
            continue
        # Subcommand path = leading non-flag, non-positional tokens.
        # gcloud storage buckets describe gs://… → subcommand = ["storage","buckets","describe"]
        sub: list[str] = []
        for tok in argv[1:]:
            if tok.startswith("-") or tok.startswith("gs://"):
                break
            sub.append(tok)
        # Keep raw tokens so we can distinguish `--flag` from `--flag=value`.
        raw_flag_tokens = [tok for tok in argv[1:] if tok.startswith("--")]
        key = tuple(sub)
        if key in seen_subcommands and not raw_flag_tokens:
            continue
        seen_subcommands.add(key)
        help_text = _gcloud_help_text(sub)

        for tok in raw_flag_tokens:
            if "=" in tok:
                flag, _value = tok.split("=", 1)
            else:
                flag, _value = tok, None

            # (1) Flag name must appear in help at all.
            if flag not in help_text:
                failures.append(
                    f"{flag!r} not found in `gcloud {' '.join(sub)} --help`; "
                    f"full argv: {argv}"
                )
                continue

            # (2) GAP-33 shape: `--flag=value` passed to a documented BOOLEAN
            # flag. gcloud documents booleans as `--[no-]flagname`. Using the
            # `=value` form on such a flag is rejected at runtime — this is
            # exactly the incident that motivated this test.
            flag_name = flag[2:]  # strip leading `--`
            bool_pattern = re.compile(
                r"--\[no-\]" + re.escape(flag_name) + r"\b"
            )
            value_pattern = re.compile(
                r"--" + re.escape(flag_name) + r"=[A-Z_]"
            )
            is_boolean = bool(bool_pattern.search(help_text))
            takes_value = bool(value_pattern.search(help_text))

            if _value is not None and is_boolean and not takes_value:
                failures.append(
                    f"{flag!r} is documented as a boolean (`--[no-]{flag_name}`) "
                    f"in `gcloud {' '.join(sub)} --help` but argv passes it the "
                    f"`=value` form ({tok!r}). This is the GAP-33 incident shape "
                    f"(`--public-access-prevention=enforced`) — gcloud rejects "
                    f"it at runtime. Full argv: {argv}"
                )

    assert not failures, (
        "gcloud flag(s) misuse vs `gcloud --help` — GAP-33 shape:\n  - "
        + "\n  - ".join(failures)
    )


# ---------------------------------------------------------------------------
# Test 2 — closed-stdin smoke for reconcile_imports (GAP-34 shape)
# ---------------------------------------------------------------------------


class _ReconcileConfig:
    """Minimal config stub for the reconcile_imports runner."""

    def __init__(self) -> None:
        self._data = {
            "gcp_project_id": "fake-project-123",
            "region": "us-central1",
        }

    def get(self, key: str, default: Any = None) -> Any:
        return self._data.get(key, default)


def test_reconcile_imports_auto_approve_no_stdin_read(monkeypatch) -> None:
    """When ``auto_approve=True``, the reconcile-imports runner must not
    read from stdin. Closed stdin + an unguarded ``confirm()`` raise
    ``EOFError`` — exactly the GAP-34 symptom on a non-tty pipe.
    """
    # Force any input() / Console.input() to fail loudly.
    monkeypatch.setattr(sys, "stdin", io.StringIO(""))

    fake_registry = [{
        "tf_address": "google_storage_bucket.fake",
        "import_id": "fake-bucket",
        "description": "Fake bucket for smoke test",
        "gcloud_check": ["gcloud", "storage", "buckets", "describe", "gs://fake-bucket"],
    }]

    from scripts import pipeline, terraform_reconcile

    monkeypatch.setattr(terraform_reconcile, "build_registry", lambda config: fake_registry)
    monkeypatch.setattr(terraform_reconcile, "terraform_state_list", lambda config: set())
    monkeypatch.setattr(terraform_reconcile, "check_exists_in_gcp", lambda cmd: (True, True))
    monkeypatch.setattr(terraform_reconcile, "import_resource", lambda addr, iid, pid: (True, ""))

    cfg = _ReconcileConfig()

    # Drive the actual runner the way pipeline.apply_pipeline drives it.
    ok = pipeline._run_reconcile_imports(cfg, {}, dry_run=False, auto_approve=True)

    assert ok is True, (
        "_run_reconcile_imports returned False under auto_approve=True — "
        "likely the auto_approve flag is not forwarded (GAP-34 shape)."
    )


def test_reconcile_imports_without_auto_approve_does_read_stdin(monkeypatch) -> None:
    """Negative companion: WITHOUT ``auto_approve``, the same path must
    attempt stdin (proving Test 2 isn't a tautology — the guard *is* the
    only thing preventing the EOFError)."""
    monkeypatch.setattr(sys, "stdin", io.StringIO(""))

    fake_registry = [{
        "tf_address": "google_storage_bucket.fake",
        "import_id": "fake-bucket",
        "description": "Fake bucket for smoke test",
        "gcloud_check": ["gcloud", "storage", "buckets", "describe", "gs://fake-bucket"],
    }]

    from scripts import pipeline, terraform_reconcile

    monkeypatch.setattr(terraform_reconcile, "build_registry", lambda config: fake_registry)
    monkeypatch.setattr(terraform_reconcile, "terraform_state_list", lambda config: set())
    monkeypatch.setattr(terraform_reconcile, "check_exists_in_gcp", lambda cmd: (True, True))
    monkeypatch.setattr(terraform_reconcile, "import_resource", lambda addr, iid, pid: (True, ""))

    cfg = _ReconcileConfig()

    # auto_approve=False → confirm() is reached → closed stdin → EOFError
    # (rich's Confirm.ask raises EOFError on closed stdin); accept any of
    # EOFError / OSError to remain robust across rich versions.
    with pytest.raises((EOFError, OSError)):
        pipeline._run_reconcile_imports(cfg, {}, dry_run=False, auto_approve=False)


# ---------------------------------------------------------------------------
# PR-L — registry validator + parent_check + required-keys tests
# ---------------------------------------------------------------------------


def _good_entry(**overrides: Any) -> dict[str, Any]:
    """Return a minimally-valid registry entry; tests override one field."""
    entry: dict[str, Any] = {
        "tf_address":   "google_storage_bucket.recordings",
        "description":  "GCS Recordings Bucket",
        "gcloud_check": ["gcloud", "storage", "buckets", "describe",
                         "gs://dev-voipbin-recordings", "--project=p"],
        "import_id":    "dev-voipbin-recordings",
    }
    entry.update(overrides)
    return entry


def test_registry_validator_rejects_none_substring() -> None:
    """PR-L D4.1 — `None` literal in tf_address must hard-fail."""
    from scripts.terraform_reconcile import ReconcileRegistryError, _validate_entry

    bad = _good_entry(tf_address="google_storage_bucket.None-voipbin-recordings")
    with pytest.raises(ReconcileRegistryError) as excinfo:
        _validate_entry(bad)
    msg = str(excinfo.value)
    assert "tf_address" in msg
    assert "None" in msg
    assert "init --reconfigure" in msg


def test_registry_validator_rejects_unsubstituted_template() -> None:
    """PR-L D4.2 — `${var.env}` placeholder in argv must hard-fail."""
    from scripts.terraform_reconcile import ReconcileRegistryError, _validate_entry

    bad = _good_entry(gcloud_check=[
        "gcloud", "storage", "buckets", "describe",
        "gs://${var.env}-voipbin-recordings", "--project=p",
    ])
    with pytest.raises(ReconcileRegistryError) as excinfo:
        _validate_entry(bad)
    msg = str(excinfo.value)
    assert "gcloud_check" in msg
    assert "${" in msg or "placeholder" in msg


def test_required_keys_missing_raises_with_hint() -> None:
    """PR-L D4.3 / PR-L2 — InstallerConfig missing `gcp_project_id` → build_registry hard-fails.

    PR-L2 narrowed RECONCILE_REQUIRED_KEYS to drop `env` (env has a safe
    terraform-side default `voipbin`). gcp_project_id remains required.
    """
    from scripts.config import InstallerConfig
    from scripts.terraform_reconcile import ReconcileRegistryError, build_registry

    cfg = InstallerConfig()
    cfg.set_many({
        # NOTE: no `gcp_project_id` — that's the precondition.
        "region": "y",
        "zone": "y-a",
        "env": "test",
    })
    with pytest.raises(ReconcileRegistryError) as excinfo:
        build_registry(cfg)
    msg = str(excinfo.value)
    assert "gcp_project_id" in msg
    assert "init --reconfigure" in msg


def test_build_registry_without_env_uses_default_no_none_literal() -> None:
    """PR-L2 — config without `env` key must build a clean registry.

    `env` has a safe default ("voipbin") matching terraform/variables.tf.
    Building a registry from a config that omits `env` must NOT leak "None"
    literals into recordings/tmp bucket names (the original GAP-35 shape),
    and must pass the PR-L _validate_entry checks.
    """
    from scripts.config import InstallerConfig
    from scripts.terraform_reconcile import build_registry

    cfg = InstallerConfig()
    cfg.set_many({
        "gcp_project_id": "voipbin-install-dev",
        "region": "us-central1",
        "zone": "us-central1-a",
        # NOTE: no `env` — must default to "voipbin".
    })
    entries = build_registry(cfg)
    # No registry entry may contain the literal "None" anywhere.
    for entry in entries:
        for field in ("tf_address", "import_id"):
            assert "None" not in entry.get(field, ""), (
                f"{field} of {entry['tf_address']} leaked 'None': "
                f"{entry.get(field)!r}"
            )
        for tok in entry.get("gcloud_check", []):
            assert not tok.startswith("None"), (
                f"gcloud_check token leaked 'None': {tok!r} "
                f"in entry {entry['tf_address']}"
            )
    # Recordings/tmp entries must use the default "voipbin" prefix.
    recordings = next(e for e in entries
                      if e["tf_address"] == "google_storage_bucket.recordings")
    assert recordings["import_id"] == "voipbin-voipbin-recordings"
    tmp = next(e for e in entries
               if e["tf_address"] == "google_storage_bucket.tmp")
    assert tmp["import_id"] == "voipbin-voipbin-tmp"


class _ParentCheckConfig:
    """Minimal config stub that already passes required-keys validation."""

    def __init__(self) -> None:
        self._data = {
            "gcp_project_id": "p",
            "region": "us-central1",
            "zone": "us-central1-a",
            "env": "test",
            "kamailio_count": 0,
            "rtpengine_count": 0,
        }

    def get(self, key: str, default: Any = None) -> Any:
        return self._data.get(key, default)


def _parent_check_fake_registry() -> list[dict[str, Any]]:
    """One child entry with a parent_check, gcloud_check, valid shape."""
    return [{
        "tf_address":   "google_sql_database.voipbin",
        "description":  "Cloud SQL Database",
        "gcloud_check": ["gcloud", "sql", "databases", "describe", "voipbin",
                         "--instance=voipbin-mysql", "--project=p"],
        "import_id":    "projects/p/instances/voipbin-mysql/databases/voipbin",
        "parent_check": ["gcloud", "sql", "instances", "describe",
                         "voipbin-mysql", "--project=p"],
    }]


def test_parent_check_defer_path(monkeypatch) -> None:
    """PR-L D4.4 — parent_check rc!=0 → entry deferred, stage returns True."""
    from scripts import terraform_reconcile as tr

    monkeypatch.setattr(tr, "build_registry", lambda cfg: _parent_check_fake_registry())
    monkeypatch.setattr(tr, "terraform_state_list", lambda cfg: set())
    monkeypatch.setattr(tr, "check_exists_in_gcp", lambda cmd: (True, True))

    import_called = {"n": 0}

    def fake_import(addr: str, iid: str, pid: str) -> tuple[bool, str]:
        import_called["n"] += 1
        return True, ""

    monkeypatch.setattr(tr, "import_resource", fake_import)

    def fake_run_cmd(cmd, *args, **kwargs):
        # parent_check returns 1 — parent absent.
        return _FakeCompleted(rc=1, stderr="404 not found")

    monkeypatch.setattr(tr, "run_cmd", fake_run_cmd)

    cfg = _ParentCheckConfig()
    ok = tr.imports(cfg, auto_approve=True)
    assert ok is True, "stage must succeed when every non-imported entry is deferred"
    assert import_called["n"] == 0, "import_resource must NOT be called when parent absent"


def test_parent_check_present_path(monkeypatch) -> None:
    """PR-L D4.5 — parent_check rc=0 → entry attempts import as normal."""
    from scripts import terraform_reconcile as tr

    monkeypatch.setattr(tr, "build_registry", lambda cfg: _parent_check_fake_registry())
    monkeypatch.setattr(tr, "terraform_state_list", lambda cfg: set())
    monkeypatch.setattr(tr, "check_exists_in_gcp", lambda cmd: (True, True))

    import_calls: list[tuple[str, str, str]] = []

    def fake_import(addr: str, iid: str, pid: str) -> tuple[bool, str]:
        import_calls.append((addr, iid, pid))
        return True, ""

    monkeypatch.setattr(tr, "import_resource", fake_import)

    def fake_run_cmd(cmd, *args, **kwargs):
        # parent_check returns 0 — parent exists.
        return _FakeCompleted(rc=0)

    monkeypatch.setattr(tr, "run_cmd", fake_run_cmd)

    cfg = _ParentCheckConfig()
    ok = tr.imports(cfg, auto_approve=True)
    assert ok is True
    assert len(import_calls) == 1, (
        f"import_resource must be called when parent_check rc=0; got {import_calls!r}"
    )
    assert import_calls[0][0] == "google_sql_database.voipbin"


def _all_deferrals_fake_registry() -> list[dict[str, Any]]:
    """4 child entries each with a parent_check — mirrors iter-4 smoke."""
    base = [
        ("google_sql_database.voipbin",      "Cloud SQL Database"),
        ("google_sql_user.voipbin",          "Cloud SQL User"),
        ("google_sql_database.aux1",         "Cloud SQL Database (aux 1)"),
        ("google_sql_database.aux2",         "Cloud SQL Database (aux 2)"),
    ]
    entries: list[dict[str, Any]] = []
    for addr, desc in base:
        entries.append({
            "tf_address":   addr,
            "description":  desc,
            "gcloud_check": ["gcloud", "sql", "databases", "describe", "x",
                             "--instance=voipbin-mysql", "--project=p"],
            "import_id":    f"projects/p/instances/voipbin-mysql/databases/{addr}",
            "parent_check": ["gcloud", "sql", "instances", "describe",
                             "voipbin-mysql", "--project=p"],
        })
    return entries


def test_reconcile_imports_returns_true_when_all_failures_are_deferrals(monkeypatch) -> None:
    """PR-L D4.6 — pipeline integration: 4 deferred → _run_reconcile_imports True."""
    import io
    from scripts import pipeline, terraform_reconcile as tr

    monkeypatch.setattr(sys, "stdin", io.StringIO(""))

    monkeypatch.setattr(tr, "build_registry", lambda cfg: _all_deferrals_fake_registry())
    monkeypatch.setattr(tr, "terraform_state_list", lambda cfg: set())
    monkeypatch.setattr(tr, "check_exists_in_gcp", lambda cmd: (True, True))

    def fake_import(addr: str, iid: str, pid: str) -> tuple[bool, str]:
        raise AssertionError(
            f"import_resource called for deferred entry {addr!r} — bug"
        )

    monkeypatch.setattr(tr, "import_resource", fake_import)
    monkeypatch.setattr(tr, "run_cmd",
                        lambda cmd, *a, **kw: _FakeCompleted(rc=1, stderr="not found"))

    cfg = _ParentCheckConfig()
    ok = pipeline._run_reconcile_imports(cfg, {}, dry_run=False, auto_approve=True)
    assert ok is True, (
        "pipeline stage must succeed when every conflict is a deferred child"
    )


# =============================================================================
# PR-L3: tfvars ↔ variables.tf name parity sweep
# =============================================================================
#
# GAP-37 root cause: `to_terraform_vars` emitted key `projectid` while
# `terraform/variables.tf` declares `project_id`. Terraform prompted
# interactively for the missing variable, blocking auto-approve flow.
#
# This test exercises the actual parity surface: every key emitted by
# to_terraform_vars must be declared in terraform/variables.tf. Synthetic
# injection (revert `project_id` back to `projectid`) demonstrably catches
# the GAP-37 incident shape.


_VARIABLES_TF = Path(__file__).resolve().parent.parent / "terraform" / "variables.tf"
_VAR_DECL_RE = re.compile(r'^variable\s+"([a-z_][a-z0-9_]*)"\s*\{', re.MULTILINE)


def _declared_terraform_variables() -> set[str]:
    """Parse `terraform/variables.tf` and return the set of declared variable names."""
    text = _VARIABLES_TF.read_text(encoding="utf-8")
    return set(_VAR_DECL_RE.findall(text))


def test_to_terraform_vars_keys_match_variables_tf() -> None:
    """PR-L3 — every key emitted by to_terraform_vars must exist in variables.tf.

    Catches the GAP-37 shape: a typo in tfvars key (e.g. `projectid` vs
    `project_id`) silently produces a tfvars.json whose terraform treats as
    a missing required variable, triggering an interactive prompt that
    halts `apply --auto-approve`.
    """
    from scripts.config import InstallerConfig

    cfg = InstallerConfig()
    cfg.set_many({
        "gcp_project_id": "voipbin-install-dev",
        "region": "us-central1",
        "zone": "us-central1-a",
        "gke_type": "zonal",
        "gke_machine_type": "n1-standard-2",
        "gke_node_count": 2,
        "vm_machine_type": "f1-micro",
        "kamailio_count": 1,
        "rtpengine_count": 1,
        "domain": "dev.voipbin-install.example.com",
        "dns_mode": "manual",
        "tls_strategy": "self-signed",
    })
    emitted = set(cfg.to_terraform_vars().keys())
    declared = _declared_terraform_variables()
    missing_in_tf = emitted - declared
    assert not missing_in_tf, (
        f"to_terraform_vars emits keys not declared in variables.tf: "
        f"{sorted(missing_in_tf)} — terraform will prompt for these, "
        f"breaking auto-approve. Add the variable or fix the tfvars key. "
        f"Declared: {sorted(declared)}"
    )


def test_required_terraform_variables_have_tfvars_value() -> None:
    """PR-L3 — every required (no-default) variable in variables.tf must have a tfvars value.

    Reads variables.tf and identifies variables without a `default = ...`
    clause; those are required. Asserts to_terraform_vars supplies a
    non-None value for each.
    """
    from scripts.config import InstallerConfig

    text = _VARIABLES_TF.read_text(encoding="utf-8")
    # Split into per-variable blocks.
    blocks = re.split(r'(?=^variable\s+")', text, flags=re.MULTILINE)
    required: list[str] = []
    for blk in blocks:
        m = _VAR_DECL_RE.search(blk)
        if not m:
            continue
        name = m.group(1)
        if "default" not in blk:
            required.append(name)

    cfg = InstallerConfig()
    cfg.set_many({
        "gcp_project_id": "voipbin-install-dev",
        "region": "us-central1",
        "zone": "us-central1-a",
        "gke_type": "zonal",
        "gke_machine_type": "n1-standard-2",
        "gke_node_count": 2,
        "vm_machine_type": "f1-micro",
        "kamailio_count": 1,
        "rtpengine_count": 1,
        "domain": "dev.voipbin-install.example.com",
        "dns_mode": "manual",
        "tls_strategy": "self-signed",
    })
    tfvars = cfg.to_terraform_vars()
    missing_values: list[str] = []
    for name in required:
        if name not in tfvars or tfvars[name] is None or tfvars[name] == "":
            missing_values.append(name)
    assert not missing_values, (
        f"variables.tf requires {missing_values} but to_terraform_vars did "
        f"not supply non-empty values. terraform will prompt interactively, "
        f"breaking --auto-approve."
    )

