"""PR-Z Phase B/C tests: ansible role cert deploy path guard."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

import yaml


ROLE_TASKS = (
    Path(__file__).resolve().parent.parent
    / "ansible" / "roles" / "kamailio" / "tasks" / "main.yml"
)


def _load_tasks():
    return yaml.safe_load(ROLE_TASKS.read_text()) or []


class TestCertDeployPathGuard:
    def test_assert_task_present_with_three_conditions(self):
        tasks = _load_tasks()
        assert_tasks = [
            t for t in tasks
            if isinstance(t, dict)
            and t.get("name", "").lower().startswith("assert cert deploy path")
        ]
        assert len(assert_tasks) == 1, (
            "expected exactly one 'Assert cert deploy path is safe' task"
        )
        body = assert_tasks[0].get("assert") or {}
        conds = body.get("that") or []
        # Three guard conditions per design §6.3
        joined = " ".join(str(c) for c in conds)
        assert "is string" in joined
        assert "startswith" in joined and "/opt/kamailio-docker/" in joined
        assert "length" in joined or "| length" in joined
        assert len(conds) >= 3

    def test_synchronize_task_uses_delete_and_chmod(self):
        tasks = _load_tasks()
        sync_tasks = [
            t for t in tasks
            if isinstance(t, dict) and "ansible.posix.synchronize" in (t or {})
        ]
        assert len(sync_tasks) == 1
        block = sync_tasks[0]["ansible.posix.synchronize"]
        assert block.get("delete") is True
        rsync_opts = " ".join(block.get("rsync_opts") or [])
        assert "D700" in rsync_opts and "F600" in rsync_opts
        # Notify handler.
        notify = sync_tasks[0].get("notify")
        assert notify and "Recreate kamailio containers" in (
            notify if isinstance(notify, str) else " ".join(notify)
        )

    def test_assert_precedes_synchronize_in_file(self):
        text = ROLE_TASKS.read_text()
        i_assert = text.find("Assert cert deploy path")
        i_sync = text.find("ansible.posix.synchronize")
        assert i_assert > -1
        assert i_sync > -1
        assert i_assert < i_sync, (
            "path-guard assert must appear BEFORE synchronize task"
        )
