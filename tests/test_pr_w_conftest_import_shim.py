"""PR-W: regression guards for the ansible.inventory.gcp_inventory import shim.

Background: from PR-N (#30) merge until PR-W (#48), every test in
``tests/test_pr_n_oslogin.py`` that did ``from ansible.inventory.gcp_inventory
import ...`` silently failed with ``ImportError: cannot import name
'gcp_inventory' from 'ansible.inventory'`` because the SYSTEM ansible package
shadowed the repo-local ``ansible/inventory/`` directory (which has no
__init__.py).

These tests pin the conftest shim so the regression cannot recur. Iter-1 and
iter-2 PR review caught several weak spots in the original draft; this file
is the post-review version with stronger guards and no dead code.
"""

from __future__ import annotations

import importlib
import json
import os
import subprocess
import sys
import types
from pathlib import Path

import pytest


REPO = Path(__file__).resolve().parent.parent
GCP_INVENTORY_PATH = REPO / "ansible" / "inventory" / "gcp_inventory.py"


class TestConftestShim:
    def test_gcp_inventory_module_registered(self):
        """conftest.py must place the repo-local module in sys.modules so
        ``from ansible.inventory.gcp_inventory import X`` finds the right
        file, not the system ansible package."""
        mod = sys.modules.get("ansible.inventory.gcp_inventory")
        assert mod is not None, (
            "conftest.py shim did not register ansible.inventory.gcp_inventory. "
            "Check tests/conftest.py:_register_gcp_inventory_shim()."
        )
        # The registered module's __file__ MUST be the repo-local file,
        # not anything under /usr/lib or site-packages.
        mod_file = Path(mod.__file__ or "")
        assert mod_file == GCP_INVENTORY_PATH, (
            f"ansible.inventory.gcp_inventory resolves to {mod_file}, not "
            f"the repo-local {GCP_INVENTORY_PATH}. The conftest shim is "
            "broken or a stray __init__.py / sys.path entry is shadowing it."
        )

    def test_shim_exposes_build_inventory_and_get_oslogin_username(self):
        """Two specific symbols from gcp_inventory.py are imported by the
        PR-N tests. Pin them so a future rename can't silently break the
        whole PR-N test class. Also exercises the package-traversal path
        (importlib.import_module of the dotted name), not just an attribute
        lookup, so a mutant that drops __path__ on ansible.inventory is
        caught here."""
        mod = importlib.import_module("ansible.inventory.gcp_inventory")
        assert hasattr(mod, "build_inventory"), (
            "ansible.inventory.gcp_inventory must export build_inventory; "
            "PR-N tests depend on it."
        )
        assert hasattr(mod, "get_oslogin_username"), (
            "ansible.inventory.gcp_inventory must export get_oslogin_username; "
            "PR-N tests depend on it."
        )
        # Submodule resolution via the package must also work — this fails
        # if the synthetic ansible.inventory has no __path__ set.
        from ansible.inventory import gcp_inventory as via_package
        assert via_package is mod

    def test_shim_re_registers_after_module_deletion(self):
        """A test that does ``del sys.modules["ansible.inventory.gcp_inventory"]``
        (a common reload pattern) followed by re-running the shim must
        re-install the leaf module against the same pinned file. Without
        the strengthened idempotency guard this used to silently no-op."""
        from tests import conftest  # type: ignore

        try:
            sys.modules.pop("ansible.inventory.gcp_inventory", None)
            conftest._register_gcp_inventory_shim()
            mod = sys.modules.get("ansible.inventory.gcp_inventory")
            assert mod is not None
            assert Path(mod.__file__ or "") == GCP_INVENTORY_PATH
        finally:
            # Leave sys.modules in a sane state for any later test.
            conftest._register_gcp_inventory_shim()

    def test_shim_preserves_real_ansible_inventory_package(self, monkeypatch):
        """If a future developer adds ``ansible/inventory/__init__.py`` (the
        "proper" fix), the shim must NOT replace the real package — it
        should extend its __path__ instead, preserving any module-level
        side effects in the real __init__.py."""
        from tests import conftest  # type: ignore

        # Simulate the future state: a real ansible.inventory package is
        # already registered with a sentinel attribute we own.
        sentinel = object()
        fake_ansible = types.ModuleType("ansible")
        fake_ansible.__path__ = ["/imaginary/system/ansible"]
        fake_inv = types.ModuleType("ansible.inventory")
        fake_inv.__path__ = ["/imaginary/system/ansible/inventory"]
        fake_inv.real_marker = sentinel  # type: ignore[attr-defined]

        # Save and restore real sys.modules so other tests aren't affected.
        saved = {k: sys.modules.get(k) for k in (
            "ansible", "ansible.inventory", "ansible.inventory.gcp_inventory",
        )}
        try:
            sys.modules["ansible"] = fake_ansible
            sys.modules["ansible.inventory"] = fake_inv
            sys.modules.pop("ansible.inventory.gcp_inventory", None)

            conftest._register_gcp_inventory_shim()

            inv = sys.modules["ansible.inventory"]
            assert inv is fake_inv, (
                "Shim replaced the real ansible.inventory package instead "
                "of extending it. This will silently swallow any module-"
                "level code in a future real __init__.py."
            )
            assert getattr(inv, "real_marker", None) is sentinel
            assert str(GCP_INVENTORY_PATH.parent) in list(inv.__path__), (
                "Shim must append the repo inventory dir to the real "
                "package's __path__ so gcp_inventory.py is resolvable as "
                "a submodule."
            )
        finally:
            for k, v in saved.items():
                if v is None:
                    sys.modules.pop(k, None)
                else:
                    sys.modules[k] = v
            conftest._register_gcp_inventory_shim()

    def test_shim_raises_when_inventory_file_missing(self, monkeypatch):
        """If the inventory file moves or is deleted, the shim must raise
        loudly during collection rather than silently skip registration."""
        from tests import conftest  # type: ignore

        bogus = Path("/tmp/voipbin-install-test-nonexistent.py")
        # Wipe registration so the idempotency early-return doesn't bypass
        # the file-existence check.
        saved = {k: sys.modules.get(k) for k in (
            "ansible", "ansible.inventory", "ansible.inventory.gcp_inventory",
        )}
        for k in list(saved):
            sys.modules.pop(k, None)
        monkeypatch.setattr(conftest, "_GCP_INVENTORY_PATH", bogus)
        try:
            with pytest.raises(RuntimeError, match="no longer ships"):
                conftest._register_gcp_inventory_shim()
        finally:
            for k, v in saved.items():
                if v is not None:
                    sys.modules[k] = v
            # Restore the real path and re-register.
            monkeypatch.undo()
            conftest._register_gcp_inventory_shim()

    def test_subprocess_canary_with_conftest_succeeds(self):
        """Positive subprocess canary: a fresh Python process that imports
        conftest THEN imports ansible.inventory.gcp_inventory must succeed
        and resolve to the repo-local file. This catches regressions where
        the in-process shim works by accident (e.g. because a prior test
        already loaded the module) but a clean process would fail."""
        canary = (
            "import conftest  # noqa: F401  -- install shim\n"
            "import sys\n"
            "from ansible.inventory.gcp_inventory import build_inventory, "
            "get_oslogin_username  # noqa: F401\n"
            "mod = sys.modules['ansible.inventory.gcp_inventory']\n"
            f"expected = {json.dumps(str(GCP_INVENTORY_PATH))}\n"
            "assert mod.__file__ == expected, mod.__file__\n"
        )
        env = os.environ.copy()
        env["PYTHONPATH"] = f"{REPO}/tests:{REPO}"
        result = subprocess.run(
            [sys.executable, "-c", canary],
            cwd=str(REPO),
            capture_output=True,
            text=True,
            env=env,
        )
        assert result.returncode == 0, (
            "Subprocess canary failed.\nstdout=%r\nstderr=%r" % (
                result.stdout, result.stderr,
            )
        )

    def test_subprocess_canary_without_conftest_fails(self):
        """Negative subprocess canary: a process that does NOT import
        conftest must fail to import ansible.inventory.gcp_inventory.
        Pins the "system ansible would shadow us" failure mode that PR-W
        was created to fix — if this test ever passes, either Python's
        ansible package landscape changed or someone added an __init__.py
        and PR-W is obsolete (update both this test and the shim)."""
        canary = (
            "from ansible.inventory.gcp_inventory import build_inventory\n"
        )
        env = os.environ.copy()
        env["PYTHONPATH"] = str(REPO)  # no tests/ on path -> no conftest
        result = subprocess.run(
            [sys.executable, "-c", canary],
            cwd=str(REPO),
            capture_output=True,
            text=True,
            env=env,
        )
        assert result.returncode != 0, (
            "Negative canary unexpectedly succeeded — either system ansible "
            "no longer ships ansible/inventory/, or ansible/inventory/ "
            "now has __init__.py and PR-W's shim is obsolete. Re-evaluate.\n"
            "stdout=%r stderr=%r" % (result.stdout, result.stderr)
        )

    def test_conftest_lives_at_tests_root(self):
        """pytest only auto-discovers conftest.py at the package boundary
        it's collecting. If someone moves conftest.py into a subdirectory,
        the shim stops running for tests collected at tests/ root, and the
        whole regression class returns silently. Pin the location AND the
        callable contract behaviorally — a string-grep on source would
        give a false positive for renamed-but-still-working refactors."""
        conftest_path = REPO / "tests" / "conftest.py"
        assert conftest_path.is_file(), (
            "tests/conftest.py is missing. The PR-N test shim lives here; "
            "moving conftest.py would silently re-break the import."
        )
        from tests import conftest  # type: ignore
        assert callable(getattr(conftest, "_register_gcp_inventory_shim", None)), (
            "tests/conftest.py must expose a callable "
            "_register_gcp_inventory_shim. If you rename it, update this "
            "test in lockstep so the behavioral contract stays pinned."
        )
