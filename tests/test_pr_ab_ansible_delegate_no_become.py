"""PR-AB: ansible delegate_to: localhost become: false regression tests.

Pins the dogfood iter#9 (2026-05-13) failure mode where PR-Z added two
tasks with `delegate_to: localhost` but no `become: false` override.
ansible.cfg's global `become = True / become_method = sudo` applies to
the delegated task, requiring sudo on the operator's local machine and
failing with `sudo: a password is required`.

Fix: every `delegate_to: localhost` task in any role/playbook MUST also
declare `become: false`. Static YAML-shape check; no live ansible
execution.
"""

from __future__ import annotations

import configparser
import sys
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
ANSIBLE_DIR = REPO_ROOT / "ansible"


def _collect_tasks(yaml_doc) -> list[dict]:
    """Walk a parsed ansible YAML doc and return every task dict.

    Handles playbooks (top-level list of plays each with `tasks:`, `pre_tasks:`,
    `post_tasks:`, `handlers:`) AND role task files (top-level list of tasks
    directly). Recurses into `block:` / `rescue:` / `always:` and
    `include_tasks:` is treated as opaque.
    """
    tasks: list[dict] = []
    if yaml_doc is None:
        return tasks

    def _walk_task_list(items):
        for item in items or []:
            if not isinstance(item, dict):
                continue
            # Block-style — recurse.
            for key in ("block", "rescue", "always"):
                if key in item and isinstance(item[key], list):
                    _walk_task_list(item[key])
            tasks.append(item)

    if isinstance(yaml_doc, list):
        # Could be a playbook (list of plays) or a tasks file (list of tasks).
        # Heuristic: if any top-level entry has `hosts:`, it's a playbook.
        if any(isinstance(p, dict) and "hosts" in p for p in yaml_doc):
            for play in yaml_doc:
                if not isinstance(play, dict):
                    continue
                for key in ("tasks", "pre_tasks", "post_tasks", "handlers"):
                    if key in play and isinstance(play[key], list):
                        _walk_task_list(play[key])
        else:
            _walk_task_list(yaml_doc)
    return tasks


def _ansible_yaml_files() -> list[Path]:
    """Return every *.yml/*.yaml file under ansible/ excluding vars/templates.

    Walks the whole ansible/ tree (not just roles+playbooks) so a future
    contributor adding `ansible/tasks/`, `ansible/bootstrap/`, etc. cannot
    bypass the regression guard. Skips templates/files/group_vars/host_vars
    where YAML may contain ansible-specific !vault tags or jinja templates
    that yaml.safe_load cannot parse.
    """
    paths: list[Path] = []
    base = ANSIBLE_DIR
    if not base.exists():
        return paths
    skip_segments = {"templates", "files", "group_vars", "host_vars"}
    for ext in ("*.yml", "*.yaml"):
        for path in base.rglob(ext):
            if any(seg in skip_segments for seg in path.parts):
                continue
            paths.append(path)
    return sorted(paths)


def _is_falsy_become(value) -> bool:
    """Return True iff value is explicitly false/no/0/'false'/'no'/'False'/'n'/'f'.

    Mirrors ansible's BOOLEANS_FALSE set
    (ansible/module_utils/parsing/convert_bool.py): {'n','no','false','f','0','off'}.
    Anything else (including missing/None/True) is treated as truthy here, which
    is the safe direction for this regression guard.
    """
    if value is False or value == 0:
        return True
    if isinstance(value, str):
        return value.strip().lower() in {"false", "no", "off", "0", "n", "f"}
    return False


# Local-equivalent delegate targets. Ansible treats `localhost`, `127.0.0.1`,
# and `::1` as referring to the controller host (subject to inventory
# overrides). The regression guard normalises across these so an operator
# copy-pasting ansible docs that use `127.0.0.1` cannot bypass the guard.
_LOCAL_DELEGATE_TARGETS = frozenset({"localhost", "127.0.0.1", "::1"})


def _is_local_delegate(value) -> bool:
    if not isinstance(value, str):
        return False
    return value.strip().lower() in _LOCAL_DELEGATE_TARGETS


class TestKamailioDelegateTasksNoBecome:
    """Guard 1: the exact tasks that caused iter#9 must have become: false."""

    def test_kamailio_main_yml_delegate_localhost_tasks_have_no_become(self):
        kamailio_main = ANSIBLE_DIR / "roles" / "kamailio" / "tasks" / "main.yml"
        assert kamailio_main.exists(), f"missing {kamailio_main}"
        with open(kamailio_main) as f:
            doc = yaml.safe_load(f)
        tasks = _collect_tasks(doc)
        delegated = [
            t for t in tasks if _is_local_delegate(t.get("delegate_to"))
        ]
        assert delegated, (
            "kamailio/tasks/main.yml has no local-delegate tasks. "
            "Did the cert-staging validation get moved? Update or delete "
            "this test if the move was intentional."
        )
        offenders = [
            t for t in delegated
            if not _is_falsy_become(t.get("become"))
        ]
        assert not offenders, (
            f"kamailio/tasks/main.yml has local-delegate tasks "
            f"WITHOUT `become: false`: "
            f"{[t.get('name', '<unnamed>') for t in offenders]}. "
            f"ansible.cfg's global `become = True` would force sudo on the "
            f"operator's localhost and fail with `sudo: a password is "
            f"required` (dogfood iter#9 lesson, 2026-05-13)."
        )


class TestRepoWideDelegateLocalhostBecomeFalse:
    """Guard 2: any role or playbook that adds a new `delegate_to: localhost`
    task in the future must also declare `become: false`."""

    def test_every_delegate_localhost_task_has_become_false(self):
        offenders: list[tuple[Path, str]] = []
        files_scanned = 0
        for path in _ansible_yaml_files():
            try:
                with open(path) as f:
                    doc = yaml.safe_load(f)
            except yaml.YAMLError:
                # Templates or vars files with !vault tags may not parse;
                # skip rather than fail.
                continue
            files_scanned += 1
            for task in _collect_tasks(doc):
                if not _is_local_delegate(task.get("delegate_to")):
                    continue
                if not _is_falsy_become(task.get("become")):
                    offenders.append(
                        (path.relative_to(REPO_ROOT), task.get("name", "<unnamed>"))
                    )
        assert files_scanned >= 5, (
            f"Test scanned only {files_scanned} ansible YAML file(s); expected "
            f">= 5 for current repo layout. Either layout shrank dramatically "
            f"or the rglob is broken. Investigate before merging."
        )
        assert not offenders, (
            f"{len(offenders)} `delegate_to: localhost` task(s) without "
            f"`become: false`:\n" +
            "\n".join(f"  {p}: {n}" for p, n in offenders) +
            "\n\nAdd `become: false` to each. ansible.cfg's global "
            "`become = True` would otherwise force sudo on the operator's "
            "localhost (dogfood iter#9 lesson, 2026-05-13)."
        )


class TestAnsibleCfgGlobalBecomeStillTrue:
    """Guard 3: PR-AB must NOT have "fixed" iter#9 by flipping global
    become to false. That would break every VM-side task. The fix is a
    per-task override, not a global relaxation."""

    def test_global_become_remains_true(self):
        cfg_path = ANSIBLE_DIR / "ansible.cfg"
        assert cfg_path.exists(), f"missing {cfg_path}"
        cfg = configparser.ConfigParser()
        cfg.read(cfg_path)
        section = "privilege_escalation"
        assert cfg.has_section(section), (
            f"ansible.cfg has no [{section}] section; PR-AB invariant lost"
        )
        become_raw = cfg.get(section, "become", fallback=None)
        assert become_raw is not None, (
            f"ansible.cfg [{section}] missing `become` key"
        )
        assert become_raw.strip().lower() in {"true", "yes", "on", "1"}, (
            f"ansible.cfg [{section}] become = {become_raw!r}; "
            f"PR-AB requires global become to REMAIN True. The iter#9 fix "
            f"is a per-task `become: false` override on delegate_to: "
            f"localhost tasks ONLY. Flipping the global breaks VM-side "
            f"Docker/systemd/file-ownership tasks across every role."
        )
