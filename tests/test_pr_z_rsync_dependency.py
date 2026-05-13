"""PR-Z D5/D6/D7 fix #5: rsync must be in the common role package list.

The kamailio role uses ``ansible.posix.synchronize`` which requires the
``rsync`` binary on both control node AND target. Without an explicit
install, fresh Ubuntu GCE images may not ship rsync (or the version may
be unmaintained), causing the cert deploy synchronize task to fail with
``rsync: command not found`` on first apply.
"""

from __future__ import annotations

from pathlib import Path

import yaml


COMMON_TASKS = (
    Path(__file__).resolve().parent.parent
    / "ansible" / "roles" / "common" / "tasks" / "main.yml"
)


def test_rsync_in_common_role_package_list():
    tasks = yaml.safe_load(COMMON_TASKS.read_text()) or []
    apt_tasks = [
        t for t in tasks
        if isinstance(t, dict) and "apt" in t
    ]
    assert apt_tasks, "common role should install packages via apt"
    # rsync must appear in at least one apt task's pkg list.
    found = False
    for t in apt_tasks:
        apt_block = t["apt"] or {}
        pkgs = apt_block.get("pkg") or apt_block.get("name") or []
        if isinstance(pkgs, str):
            pkgs = [pkgs]
        if "rsync" in pkgs:
            found = True
            break
    assert found, (
        "rsync must be listed in the common role package install task — "
        "ansible.posix.synchronize requires rsync on both ends"
    )
