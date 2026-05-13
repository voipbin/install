"""PR-Z Phase B/C tests: ansible role cert deploy path guard."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from unittest import mock

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

    def test_path_guard_rejects_parent_traversal(self):
        """PR-Z D5/D6/D7 fix #4: the assert ``that:`` list must include a
        condition that rejects ``..`` segments in ``certs_deploy_path``."""
        tasks = _load_tasks()
        assert_task = next(
            t for t in tasks
            if isinstance(t, dict)
            and t.get("name", "").lower().startswith("assert cert deploy path")
        )
        conds = assert_task["assert"]["that"]
        joined = " ".join(str(c) for c in conds)
        assert ".." in joined and "not in" in joined, (
            "path guard must reject '..' traversal; got: " + joined
        )

    def test_synchronize_src_uses_cert_staging_dir_extra_var(self):
        """PR-Z D5/D6/D7 BLOCKER fix: src must reference ``cert_staging_dir``
        rather than ``{{ playbook_dir }}/../.cert-staging/`` which resolves
        to ansible/.cert-staging/, not INSTALLER_DIR/.cert-staging/."""
        tasks = _load_tasks()
        sync_task = next(
            t for t in tasks
            if isinstance(t, dict) and "ansible.posix.synchronize" in t
        )
        src = sync_task["ansible.posix.synchronize"]["src"]
        assert "cert_staging_dir" in src, (
            f"synchronize src must reference {{{{ cert_staging_dir }}}}, got {src!r}"
        )
        assert "playbook_dir" not in src, (
            "synchronize src must NOT use {{ playbook_dir }}/../.cert-staging "
            "(resolves to ansible/.cert-staging not INSTALLER_DIR/.cert-staging)"
        )

    def test_staging_precondition_assert_and_fail_present(self):
        """PR-Z D5/D6/D7 BLOCKER fix: precondition pair stat+fail must guard
        the synchronize task so a missing staging dir produces a clear
        operator-actionable error rather than rsync's opaque exit 23."""
        tasks = _load_tasks()
        stat_tasks = [
            t for t in tasks
            if isinstance(t, dict)
            and t.get("name", "").lower().startswith("assert cert staging")
        ]
        fail_tasks = [
            t for t in tasks
            if isinstance(t, dict)
            and t.get("name", "").lower().startswith("fail if cert staging")
        ]
        assert len(stat_tasks) == 1
        assert len(fail_tasks) == 1
        # stat task must inspect cert_staging_dir
        assert "cert_staging_dir" in str(stat_tasks[0].get("stat") or {})
        # fail must reference the stat register
        when_clause = str(fail_tasks[0].get("when") or "")
        assert "_cert_staging_stat" in when_clause

    def test_fullchain_chmod_task_notifies_recreate(self):
        """PR-Z D5/D6/D7 fix #6: the fullchain 0644 task must notify the
        kamailio container recreate handler so a chmod-only change
        (operator runs apply on an existing host) triggers a restart."""
        tasks = _load_tasks()
        target = next(
            t for t in tasks
            if isinstance(t, dict)
            and "fullchain.pem world-readable" in (t.get("name") or "")
        )
        notify = target.get("notify")
        joined = notify if isinstance(notify, str) else " ".join(notify or [])
        assert "Recreate kamailio containers" in joined


class TestCertStagingExtraVar:
    """PR-Z D5/D6/D7 BLOCKER fix: ``_write_extra_vars`` must inject the
    absolute ``cert_staging_dir`` path so the kamailio role's synchronize
    src resolves to the same location that the cert_provision pipeline
    stage wrote PEMs into."""

    def test_extra_vars_includes_cert_staging_dir(self, tmp_path, monkeypatch):
        # Stub out the imports so we don't have to spin up a real config.
        from scripts import ansible_runner
        from scripts import pipeline as pl

        # Force ansible_runner.INSTALLER_DIR to a controlled value.
        monkeypatch.setattr(ansible_runner, "INSTALLER_DIR", tmp_path)

        # Build a minimal config double + terraform_outputs.
        class _Cfg:
            def to_ansible_vars(self):
                return {}
            def get(self, key, default=None):
                return default
        cfg = _Cfg()
        tf_outputs = {}

        # _build_kamailio_auth_db_url short-circuits on empty mysql_host;
        # _build_rtpengine_socks short-circuits on empty list. Both fine.
        extra_vars_path = ansible_runner._write_extra_vars(cfg, tf_outputs)
        try:
            payload = json.loads(extra_vars_path.read_text())
        finally:
            extra_vars_path.unlink(missing_ok=True)

        assert "cert_staging_dir" in payload
        expected = str(tmp_path / pl.CERT_STAGING_DIRNAME)
        assert payload["cert_staging_dir"] == expected
