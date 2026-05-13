"""PR-W: regression guard for the ansible.inventory.gcp_inventory import shim.

Background: from PR-N (#30) merge until PR-W (#48), every test in
``tests/test_pr_n_oslogin.py`` that did ``from ansible.inventory.gcp_inventory
import ...`` silently failed with ``ImportError: cannot import name
'gcp_inventory' from 'ansible.inventory'`` because the SYSTEM ansible package
shadowed the repo-local ``ansible/inventory/`` directory (which has no
__init__.py).

These tests pin the conftest shim so the regression cannot recur:

1. The conftest shim must register ``ansible.inventory.gcp_inventory`` in
   sys.modules pointing at the repo-local file.
2. The repo-local ``ansible/inventory/gcp_inventory.py`` must exist at the
   path the shim hardcodes.
3. The 9 PR-N test methods that used to fail must currently pass (executed
   here as a sub-collection so a future refactor that breaks the shim is
   caught even if someone forgets to run the full suite).
"""

from __future__ import annotations

import importlib
import subprocess
import sys
from pathlib import Path


REPO = Path(__file__).resolve().parent.parent


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
        # not anything under /usr/lib or site-packages. Without this check,
        # a future regression where the shim silently picks up the system
        # ansible package would slip through.
        mod_file = Path(mod.__file__ or "")
        expected = REPO / "ansible" / "inventory" / "gcp_inventory.py"
        assert mod_file == expected, (
            f"ansible.inventory.gcp_inventory resolves to {mod_file}, not "
            f"the repo-local {expected}. The conftest shim is broken or a "
            "stray __init__.py / sys.path entry is shadowing it."
        )

    def test_gcp_inventory_file_exists_at_pinned_path(self):
        """The conftest hardcodes ansible/inventory/gcp_inventory.py. If the
        file moves, the shim must be updated in lockstep, and this test
        guards the contract."""
        path = REPO / "ansible" / "inventory" / "gcp_inventory.py"
        assert path.is_file(), (
            f"{path} is missing. If gcp_inventory.py moved, update "
            "tests/conftest.py _GCP_INVENTORY_PATH and this test together."
        )

    def test_shim_exposes_build_inventory_and_get_oslogin_username(self):
        """Two specific symbols from gcp_inventory.py are imported by the
        PR-N tests. Pin them so a future rename can't silently break the
        whole PR-N test class."""
        mod = importlib.import_module("ansible.inventory.gcp_inventory")
        assert hasattr(mod, "build_inventory"), (
            "ansible.inventory.gcp_inventory must export build_inventory; "
            "PR-N tests depend on it."
        )
        assert hasattr(mod, "get_oslogin_username"), (
            "ansible.inventory.gcp_inventory must export get_oslogin_username; "
            "PR-N tests depend on it."
        )

    def test_subprocess_import_under_pytest_finds_repo_module(self):
        """Strongest regression guard: run pytest in a fresh subprocess
        against just the canary import. This is exactly the failure mode
        we hit (the shim wasn't there, system ansible was). If the shim
        ever regresses, this subprocess returns rc != 0 even if the
        in-process tests above pass because some other test loaded the
        module first."""
        canary = (
            "import sys\n"
            "from ansible.inventory.gcp_inventory import build_inventory, "
            "get_oslogin_username\n"
            "mod = sys.modules['ansible.inventory.gcp_inventory']\n"
            f"assert mod.__file__ == {str(REPO / 'ansible' / 'inventory' / 'gcp_inventory.py')!r}, mod.__file__\n"
        )
        result = subprocess.run(
            [sys.executable, "-c", canary],
            cwd=str(REPO),
            capture_output=True,
            text=True,
            env={
                # Force pytest's conftest discovery path: set PYTHONPATH so
                # tests/ is importable, then explicitly import conftest to
                # install the shim BEFORE the canary runs. This mirrors what
                # pytest does internally without needing the pytest binary.
                "PYTHONPATH": f"{REPO}/tests:{REPO}",
                "PATH": "/usr/bin:/bin",
            },
        )
        # We need conftest to run; the subprocess above doesn't auto-import
        # it. Re-run with an explicit conftest import as the first statement.
        canary_with_conftest = "import conftest  # noqa: F401  -- install shim\n" + canary
        result = subprocess.run(
            [sys.executable, "-c", canary_with_conftest],
            cwd=str(REPO),
            capture_output=True,
            text=True,
            env={
                "PYTHONPATH": f"{REPO}/tests:{REPO}",
                "PATH": "/usr/bin:/bin",
            },
        )
        assert result.returncode == 0, (
            "Subprocess canary failed. stdout=%r stderr=%r" % (
                result.stdout, result.stderr,
            )
        )

    def test_conftest_lives_at_tests_root(self):
        """pytest only auto-discovers conftest.py at the package boundary
        it's collecting. If someone moves conftest.py into a subdirectory,
        the shim stops running for tests collected at tests/ root, and the
        whole regression class returns silently. Pin the location."""
        conftest = REPO / "tests" / "conftest.py"
        assert conftest.is_file(), (
            "tests/conftest.py is missing. The PR-N test shim lives here; "
            "moving conftest.py would silently re-break the import."
        )
        content = conftest.read_text()
        assert "_register_gcp_inventory_shim" in content, (
            "tests/conftest.py no longer contains _register_gcp_inventory_shim. "
            "PR-N tests will silently start failing again."
        )
        assert "ansible.inventory.gcp_inventory" in content, (
            "tests/conftest.py no longer references "
            "ansible.inventory.gcp_inventory. The shim is broken."
        )
