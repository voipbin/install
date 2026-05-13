"""pytest conftest for the install repo test suite.

Registers the repo-local ``ansible/inventory/gcp_inventory.py`` module under
the importable name ``ansible.inventory.gcp_inventory`` so tests can
``from ansible.inventory.gcp_inventory import build_inventory`` without
colliding with the system-wide ``ansible`` package (which also exports a
``ansible.inventory`` submodule, none of whose contents are ours).

Background: PR-N (#30) introduced ``tests/test_pr_n_oslogin.py`` with
``from ansible.inventory.gcp_inventory import ...`` imports. The repo's
``ansible/inventory/`` directory has no ``__init__.py``, so Python's normal
package resolution found the SYSTEM ansible package first and the imports
raised ``ImportError: cannot import name 'gcp_inventory' from
'ansible.inventory'``. The PR-N tests silently failed for every commit
between PR-N (May 12 2026) and PR-W (May 13 2026, this fix).

Fix: load the real module via importlib at session start and register it in
sys.modules under both ``ansible.inventory`` (as a synthetic package wrapping
the system one) and ``ansible.inventory.gcp_inventory``. This is test-only
shim. The production loader (Ansible's dynamic inventory invocation) executes
``gcp_inventory.py`` directly via ``--inventory inventory/gcp_inventory.py``
and is unaffected.

This file MUST live at ``tests/conftest.py`` (not a subdir) so pytest
discovers it before collecting any test that imports from
``ansible.inventory``.
"""

from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path


_REPO_ROOT = Path(__file__).resolve().parent.parent
_GCP_INVENTORY_PATH = _REPO_ROOT / "ansible" / "inventory" / "gcp_inventory.py"


def _register_gcp_inventory_shim() -> None:
    """Make ``ansible.inventory.gcp_inventory`` importable from the repo path.

    Idempotent: re-importing this conftest (e.g. pytest --collect-only twice
    in the same process) leaves sys.modules in a consistent state.
    """
    if "ansible.inventory.gcp_inventory" in sys.modules:
        return  # already registered

    if not _GCP_INVENTORY_PATH.is_file():
        # If the file moves, fail loudly during collection rather than at
        # test time with a confusing ImportError. The path is part of the
        # contract these tests verify.
        raise RuntimeError(
            f"conftest.py expected {_GCP_INVENTORY_PATH} to exist; "
            "the install repo no longer ships the dynamic inventory at "
            "this path. Update conftest.py and any tests that import "
            "ansible.inventory.gcp_inventory."
        )

    # Build a synthetic ``ansible.inventory`` package that exposes our
    # gcp_inventory.py without disturbing the rest of the (system) ansible
    # package. We deliberately do NOT import the system 'ansible' here --
    # the tests only need .inventory.gcp_inventory, so a fresh stand-in
    # package is enough and avoids pulling in the system ansible runtime.
    ansible_pkg = sys.modules.get("ansible")
    if ansible_pkg is None:
        ansible_pkg = types.ModuleType("ansible")
        ansible_pkg.__path__ = []  # mark as package
        sys.modules["ansible"] = ansible_pkg

    inv_pkg_name = "ansible.inventory"
    inv_pkg = types.ModuleType(inv_pkg_name)
    inv_pkg.__path__ = [str(_GCP_INVENTORY_PATH.parent)]
    sys.modules[inv_pkg_name] = inv_pkg
    setattr(ansible_pkg, "inventory", inv_pkg)

    mod_name = "ansible.inventory.gcp_inventory"
    spec = importlib.util.spec_from_file_location(
        mod_name,
        _GCP_INVENTORY_PATH,
        submodule_search_locations=None,
    )
    if spec is None or spec.loader is None:
        raise RuntimeError(
            f"importlib could not build a spec for {_GCP_INVENTORY_PATH}"
        )
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module
    spec.loader.exec_module(module)
    setattr(inv_pkg, "gcp_inventory", module)


_register_gcp_inventory_shim()
