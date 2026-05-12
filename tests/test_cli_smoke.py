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
import shutil
import subprocess
import sys
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
