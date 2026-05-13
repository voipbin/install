"""PR-AB-1: ansible.posix.synchronize remote-side privilege regression tests.

Pins the dogfood iter#10 (2026-05-13) failure where PR-AB's `become: false`
on the synchronize task removed BOTH controller-side sudo (intended fix
for iter#9) AND remote-side root privilege (unintended, breaks rsync
writing to root-owned /opt/kamailio-docker/certs/). The correct pattern,
recommended by PR-AB's R1 review but only half-implemented in PR-AB v2,
is `rsync_path: "sudo rsync"` so the receiver-side rsync execs through
sudo while the controller-side stays no-sudo.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
ANSIBLE_DIR = REPO_ROOT / "ansible"


def _collect_tasks(yaml_doc) -> list[dict]:
    """Walk parsed ansible YAML and return every task dict.

    See test_pr_ab_ansible_delegate_no_become.py for the same helper.
    Duplicated rather than imported to keep this regression test
    self-contained.
    """
    tasks: list[dict] = []
    if yaml_doc is None:
        return tasks

    def _walk(items):
        for item in items or []:
            if not isinstance(item, dict):
                continue
            for key in ("block", "rescue", "always"):
                if key in item and isinstance(item[key], list):
                    _walk(item[key])
            tasks.append(item)

    if isinstance(yaml_doc, list):
        if any(isinstance(p, dict) and "hosts" in p for p in yaml_doc):
            for play in yaml_doc:
                if not isinstance(play, dict):
                    continue
                for key in ("tasks", "pre_tasks", "post_tasks", "handlers"):
                    if key in play and isinstance(play[key], list):
                        _walk(play[key])
        else:
            _walk(yaml_doc)
    return tasks


def _ansible_yaml_files() -> list[Path]:
    paths: list[Path] = []
    if not ANSIBLE_DIR.exists():
        return paths
    skip = {"templates", "files", "group_vars", "host_vars"}
    for ext in ("*.yml", "*.yaml"):
        for path in ANSIBLE_DIR.rglob(ext):
            if any(seg in skip for seg in path.parts):
                continue
            paths.append(path)
    return sorted(paths)


_ROOT_OWNED_PREFIXES = ("/opt/", "/etc/", "/usr/", "/var/", "/root/")


def _synchronize_module_payload(task: dict) -> dict | None:
    """Return the synchronize-module argument dict, or None if this task
    is not a synchronize task. Accepts both fully-qualified
    `ansible.posix.synchronize:` and the bare `synchronize:` shorthand.
    """
    for key in ("ansible.posix.synchronize", "synchronize"):
        payload = task.get(key)
        if isinstance(payload, dict):
            return payload
    return None


def _dest_targets_root_owned(dest) -> bool:
    """True iff dest is a string path under a conventionally root-owned tree.

    Jinja `{{ }}` expressions are treated as "could be anything; assume
    root-owned" because cert deploy paths in this repo all resolve into
    /opt/kamailio-docker/. If a future contributor adds a synchronize
    with a Jinja dest that resolves into a user-owned tree, they can
    override with become: true / explicit no-sudo and document.
    """
    if not isinstance(dest, str):
        return False
    if "{{" in dest:
        # Cannot evaluate Jinja at static-check time. The only synchronize
        # in this repo today does template into /opt/kamailio-docker, so
        # assume root-owned. Update this heuristic if the assumption changes.
        return True
    return dest.startswith(_ROOT_OWNED_PREFIXES)


def _has_sudo_rsync_path(payload: dict) -> bool:
    rp = payload.get("rsync_path")
    if not isinstance(rp, str):
        return False
    return "sudo" in rp.split() and "rsync" in rp


def _is_truthy_become(task: dict) -> bool:
    val = task.get("become")
    if val is True or val == 1:
        return True
    if isinstance(val, str):
        return val.strip().lower() in {"true", "yes", "on", "1"}
    return False


def _is_falsy_become(value) -> bool:
    if value is False or value == 0:
        return True
    if isinstance(value, str):
        return value.strip().lower() in {"false", "no", "off", "0", "n", "f"}
    return False


_LOCAL_DELEGATE = frozenset({"localhost", "127.0.0.1", "::1"})


def _is_local_delegate(value) -> bool:
    if not isinstance(value, str):
        return False
    return value.strip().lower() in _LOCAL_DELEGATE


class TestSynchronizeRemotePrivilege:
    """Guard 1: ANY synchronize task pushing to a root-owned dest MUST
    grant remote-side root privilege, via either `rsync_path: "sudo
    rsync"` OR `become: true`. Otherwise rsync receiver hits
    `Permission denied` (dogfood iter#10 lesson, 2026-05-13)."""

    def test_synchronize_to_root_owned_dest_has_remote_sudo(self):
        offenders: list[tuple[Path, str, str]] = []
        scanned = 0
        for path in _ansible_yaml_files():
            try:
                with open(path) as f:
                    doc = yaml.safe_load(f)
            except yaml.YAMLError:
                continue
            scanned += 1
            for task in _collect_tasks(doc):
                payload = _synchronize_module_payload(task)
                if payload is None:
                    continue
                dest = payload.get("dest")
                if not _dest_targets_root_owned(dest):
                    continue
                if _has_sudo_rsync_path(payload):
                    continue
                if _is_truthy_become(task):
                    continue
                offenders.append((
                    path.relative_to(REPO_ROOT),
                    task.get("name", "<unnamed>"),
                    str(dest),
                ))
        assert scanned >= 5, (
            f"Scanned only {scanned} ansible YAML file(s); expected >= 5"
        )
        assert not offenders, (
            f"{len(offenders)} synchronize task(s) push to a root-owned "
            f"dest without remote-side sudo "
            f"(neither rsync_path: \"sudo rsync\" nor become: true):\n" +
            "\n".join(f"  {p}: {n} -> {d}" for p, n, d in offenders) +
            "\n\nReceiver-side rsync will fail with `Permission denied` "
            "writing to the root-owned destination (dogfood iter#10 "
            "lesson, 2026-05-13)."
        )


class TestKamailioSynchronizeSpecificFix:
    """Guard 2: pin the exact PR-AB-1 fix on the cert-staging sync task."""

    def test_kamailio_cert_sync_has_rsync_path_sudo(self):
        path = ANSIBLE_DIR / "roles" / "kamailio" / "tasks" / "main.yml"
        with open(path) as f:
            doc = yaml.safe_load(f)
        synchronize_tasks = [
            t for t in _collect_tasks(doc)
            if _synchronize_module_payload(t) is not None
        ]
        assert synchronize_tasks, (
            "kamailio/tasks/main.yml has no synchronize task. Did "
            "cert-staging deploy move? Update or delete this test."
        )
        # Specifically, the cert-staging task — match by dest pattern.
        cert_sync = [
            t for t in synchronize_tasks
            if "certs_deploy_path" in str(
                _synchronize_module_payload(t).get("dest", "")
            )
        ]
        assert cert_sync, (
            "kamailio/tasks/main.yml has synchronize task(s) but none "
            "matching the cert-staging dest pattern. Layout drift?"
        )
        for t in cert_sync:
            payload = _synchronize_module_payload(t)
            assert _has_sudo_rsync_path(payload), (
                f"kamailio cert-staging synchronize task "
                f"{t.get('name', '<unnamed>')!r} missing "
                f"`rsync_path: \"sudo rsync\"`. Without it the receiver-"
                f"side rsync runs as non-root SSH user and fails with "
                f"Permission denied on /opt/kamailio-docker/certs/ "
                f"(PR-AB-1 / iter#10 lesson)."
            )


class TestSynchronizeControllerSideNoSudo:
    """Guard 3: synchronize tasks that delegate to localhost MUST keep
    become: false so the controller-side rsync does not prompt for sudo
    on the operator's machine (iter#9 failure mode)."""

    def test_local_delegated_synchronize_keeps_no_become(self):
        offenders: list[tuple[Path, str]] = []
        for path in _ansible_yaml_files():
            try:
                with open(path) as f:
                    doc = yaml.safe_load(f)
            except yaml.YAMLError:
                continue
            for task in _collect_tasks(doc):
                payload = _synchronize_module_payload(task)
                if payload is None:
                    continue
                if not _is_local_delegate(task.get("delegate_to")):
                    continue
                if not _is_falsy_become(task.get("become")):
                    offenders.append(
                        (path.relative_to(REPO_ROOT),
                         task.get("name", "<unnamed>"))
                    )
        assert not offenders, (
            f"{len(offenders)} local-delegated synchronize task(s) "
            f"without `become: false`:\n" +
            "\n".join(f"  {p}: {n}" for p, n in offenders) +
            "\n\nWould prompt for sudo on the operator's machine "
            "(iter#9 regression)."
        )
