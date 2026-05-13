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
sys.modules under both ``ansible.inventory`` (as a synthetic stand-in
package that does NOT wrap the system one) and
``ansible.inventory.gcp_inventory``. This is a test-only shim. The
production loader (Ansible's dynamic inventory invocation) executes
``gcp_inventory.py`` directly via ``--inventory inventory/gcp_inventory.py``
and is unaffected.

Compatibility with a future ``ansible/inventory/__init__.py``: when a
developer later adds that file (the "proper" fix), the shim will detect
the pre-existing real ``ansible.inventory`` package in sys.modules and
will EXTEND its ``__path__`` rather than overwriting the module, so the
real package's module-level side effects continue to run. The shim also
sentinel-tags the synthetic package it creates so a subsequent re-run can
distinguish "we own this" from "system code owns this".

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
_SHIM_SENTINEL = "_voipbin_install_test_shim"


def _register_gcp_inventory_shim() -> None:
    """Make ``ansible.inventory.gcp_inventory`` importable from the repo path.

    Idempotent and robust to:
      - Re-import in the same process (e.g. pytest re-collection).
      - A pre-existing real ``ansible`` package (we extend, not replace).
      - A pre-existing real ``ansible.inventory`` package with its own
        ``__init__.py`` (we extend its ``__path__``).
      - ``del sys.modules["ansible.inventory.gcp_inventory"]`` reload
        patterns (we re-register).
    """
    # Strong idempotency: all three modules must be in place AND the leaf's
    # __file__ must match the pinned path. Otherwise re-install.
    existing = sys.modules.get("ansible.inventory.gcp_inventory")
    if (
        existing is not None
        and Path(getattr(existing, "__file__", "") or "") == _GCP_INVENTORY_PATH
        and "ansible" in sys.modules
        and "ansible.inventory" in sys.modules
    ):
        return

    if not _GCP_INVENTORY_PATH.is_file():
        # If the file moves, fail loudly during collection rather than at
        # test time with a confusing ImportError. The path is part of the
        # contract these tests verify.
        raise RuntimeError(
            f"conftest.py expected {_GCP_INVENTORY_PATH} to exist; "
            "the install repo no longer ships the dynamic inventory at "
            "this path. Update conftest.py _GCP_INVENTORY_PATH and any "
            "tests that import ansible.inventory.gcp_inventory."
        )

    # 1. ``ansible`` package: respect any pre-existing real one; only create
    #    a synthetic stand-in if nothing else has registered it. The
    #    synthetic stand-in is tagged so the next call can recognize it.
    ansible_pkg = sys.modules.get("ansible")
    if ansible_pkg is None:
        ansible_pkg = types.ModuleType("ansible")
        # Empty __path__: we deliberately do NOT want filesystem-based
        # submodule discovery on the synthetic stand-in. Submodules are
        # only what we explicitly register.
        ansible_pkg.__path__ = []
        setattr(ansible_pkg, _SHIM_SENTINEL, True)
        sys.modules["ansible"] = ansible_pkg

    # 2. ``ansible.inventory`` subpackage: same policy. If a real one is
    #    already loaded (future ``ansible/inventory/__init__.py``), extend
    #    its __path__ to include our directory so it can still find
    #    gcp_inventory as a sibling; otherwise synthesize a stand-in.
    inv_pkg_name = "ansible.inventory"
    inv_pkg = sys.modules.get(inv_pkg_name)
    if inv_pkg is None:
        inv_pkg = types.ModuleType(inv_pkg_name)
        inv_pkg.__path__ = [str(_GCP_INVENTORY_PATH.parent)]
        setattr(inv_pkg, _SHIM_SENTINEL, True)
        sys.modules[inv_pkg_name] = inv_pkg
        setattr(ansible_pkg, "inventory", inv_pkg)
    else:
        # Real or synthetic from a previous run: ensure our directory is on
        # __path__ so submodule resolution can find gcp_inventory.py.
        path_list = getattr(inv_pkg, "__path__", None)
        if path_list is None:
            inv_pkg.__path__ = [str(_GCP_INVENTORY_PATH.parent)]
        elif str(_GCP_INVENTORY_PATH.parent) not in list(path_list):
            try:
                path_list.append(str(_GCP_INVENTORY_PATH.parent))
            except AttributeError:
                # _NamespacePath etc. may not support append; fall back to
                # a fresh list.
                inv_pkg.__path__ = list(path_list) + [str(_GCP_INVENTORY_PATH.parent)]

    # 3. The leaf module: load explicitly from the pinned path. Always re-
    #    register so a reload pattern in some test cannot leave a stale
    #    entry.
    mod_name = "ansible.inventory.gcp_inventory"
    spec = importlib.util.spec_from_file_location(mod_name, _GCP_INVENTORY_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(
            f"importlib could not build a spec for {_GCP_INVENTORY_PATH}"
        )
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module
    spec.loader.exec_module(module)
    setattr(inv_pkg, "gcp_inventory", module)


_register_gcp_inventory_shim()
