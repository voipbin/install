"""PR-M: ansible_run/ansible_check must invoke ansible-playbook with cwd=ANSIBLE_DIR.

Background (GAP-39): site.yml references `role: common`, which Ansible resolves
via `roles_path = ./roles` in ansible/ansible.cfg. ansible.cfg is only loaded
when ansible-playbook runs from the ansible/ directory (or when ANSIBLE_CONFIG
is exported). The previous implementation passed no cwd to run_cmd, so the
parent process's cwd (typically the repo root) was used, ansible.cfg was
silently ignored, and the playbook failed with:

    ERROR! the role 'common' was not found in
      /home/.../ansible/playbooks/roles:/home/.../.ansible/roles:...

These tests enforce three guarantees:
  1. run_cmd is called with cwd=ANSIBLE_DIR (the cwd wiring).
  2. ansible.cfg exists at ANSIBLE_DIR/ansible.cfg and declares roles_path
     relative to that directory (the contract the cwd wiring depends on).
  3. ansible-playbook --syntax-check resolves all roles successfully when
     run from ANSIBLE_DIR, and FAILS when run from a different cwd. This is
     the actual-execution gate (v4 §5): proves the cwd argument is what
     makes the playbook work, not coincidence.
"""

import shutil
import subprocess
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from scripts.ansible_runner import ANSIBLE_DIR, PLAYBOOK_SITE


def _make_config(data: dict):
    cfg = MagicMock()
    cfg.to_ansible_vars.return_value = dict(data)
    cfg.get.side_effect = lambda key, default="": data.get(key, default)
    return cfg


class TestAnsibleRunCwd:
    """ansible_run must pass cwd=ANSIBLE_DIR and a sanitized env to run_cmd."""

    @patch("scripts.ansible_runner.run_cmd")
    def test_ansible_run_passes_cwd_ansible_dir(self, mock_run_cmd):
        mock_run_cmd.return_value = MagicMock(returncode=0)
        from scripts.ansible_runner import ansible_run
        cfg = _make_config({"gcp_project_id": "p1", "zone": "z1"})
        ansible_run(cfg, {})
        assert mock_run_cmd.called
        _, kwargs = mock_run_cmd.call_args
        assert kwargs.get("cwd") == ANSIBLE_DIR, (
            f"ansible_run must pass cwd=ANSIBLE_DIR ({ANSIBLE_DIR}) so that "
            f"ansible.cfg is loaded. Got cwd={kwargs.get('cwd')!r}."
        )

    @patch("scripts.ansible_runner.run_cmd")
    def test_ansible_check_passes_cwd_ansible_dir(self, mock_run_cmd):
        mock_run_cmd.return_value = MagicMock(returncode=0)
        from scripts.ansible_runner import ansible_check
        cfg = _make_config({"gcp_project_id": "p1", "zone": "z1"})
        ansible_check(cfg, {})
        assert mock_run_cmd.called
        _, kwargs = mock_run_cmd.call_args
        assert kwargs.get("cwd") == ANSIBLE_DIR, (
            f"ansible_check must pass cwd=ANSIBLE_DIR ({ANSIBLE_DIR}). "
            f"Got cwd={kwargs.get('cwd')!r}."
        )

    @patch("scripts.ansible_runner.run_cmd")
    def test_ansible_run_pins_ansible_config_env(self, mock_run_cmd):
        """env must pin ANSIBLE_CONFIG to repo ansible.cfg and strip overrides."""
        mock_run_cmd.return_value = MagicMock(returncode=0)
        from scripts.ansible_runner import ansible_run
        cfg = _make_config({"gcp_project_id": "p1", "zone": "z1"})
        # Simulate a hostile parent env with operator-exported overrides.
        import os
        with patch.dict(os.environ, {
            "ANSIBLE_CONFIG": "/tmp/operator/ansible.cfg",
            "ANSIBLE_ROLES_PATH": "/tmp/operator/roles",
            "ANSIBLE_INVENTORY": "/tmp/operator/inventory",
        }, clear=False):
            ansible_run(cfg, {})
        _, kwargs = mock_run_cmd.call_args
        env = kwargs.get("env")
        assert env is not None, "ansible_run must pass env= to run_cmd"
        assert env.get("ANSIBLE_CONFIG") == str(ANSIBLE_DIR / "ansible.cfg"), (
            f"ANSIBLE_CONFIG must be pinned to repo ansible.cfg, got "
            f"{env.get('ANSIBLE_CONFIG')!r}"
        )
        assert "ANSIBLE_ROLES_PATH" not in env, (
            "ANSIBLE_ROLES_PATH must be stripped from env to prevent "
            "operator overrides from defeating ansible.cfg roles_path."
        )
        assert "ANSIBLE_INVENTORY" not in env, (
            "ANSIBLE_INVENTORY must be stripped from env."
        )

    @patch("scripts.ansible_runner.run_cmd")
    def test_ansible_check_pins_ansible_config_env(self, mock_run_cmd):
        mock_run_cmd.return_value = MagicMock(returncode=0)
        from scripts.ansible_runner import ansible_check
        cfg = _make_config({"gcp_project_id": "p1", "zone": "z1"})
        import os
        with patch.dict(os.environ, {
            "ANSIBLE_CONFIG": "/tmp/operator/ansible.cfg",
            "ANSIBLE_ROLES_PATH": "/tmp/operator/roles",
        }, clear=False):
            ansible_check(cfg, {})
        _, kwargs = mock_run_cmd.call_args
        env = kwargs.get("env")
        assert env is not None
        assert env.get("ANSIBLE_CONFIG") == str(ANSIBLE_DIR / "ansible.cfg")
        assert "ANSIBLE_ROLES_PATH" not in env


class TestAnsibleCfgContract:
    """The cwd wiring is only useful if ansible.cfg actually lives at
    ANSIBLE_DIR and uses a relative roles_path. Lock that contract."""

    def test_ansible_cfg_exists_at_ansible_dir(self):
        cfg_path = ANSIBLE_DIR / "ansible.cfg"
        assert cfg_path.exists(), (
            f"ansible.cfg must exist at {cfg_path} for cwd-based config loading."
        )

    def test_ansible_cfg_declares_relative_roles_path(self):
        cfg_path = ANSIBLE_DIR / "ansible.cfg"
        content = cfg_path.read_text()
        assert "roles_path" in content, "ansible.cfg must declare roles_path."
        # roles_path must be relative ("./roles" or "roles") so that it resolves
        # via cwd. An absolute path would mask the bug we are guarding against.
        for line in content.splitlines():
            stripped = line.strip()
            if stripped.startswith("roles_path"):
                _, _, value = stripped.partition("=")
                value = value.strip()
                assert not value.startswith("/"), (
                    f"roles_path must be relative; got {value!r}. An absolute "
                    "path would defeat the cwd-based resolution this PR enforces."
                )
                break

    def test_common_role_exists(self):
        # site.yml references `role: common`. The role directory must exist
        # under ansible/roles/common/ (NOT under playbooks/roles/).
        common_role = ANSIBLE_DIR / "roles" / "common"
        assert common_role.is_dir(), (
            f"Expected role directory at {common_role}. site.yml's `role: common` "
            "reference resolves here via ansible.cfg's roles_path."
        )


class TestActualExecutionGate:
    """v4 §5 actual-execution gate: invoke ansible-playbook --syntax-check
    and prove that running from ANSIBLE_DIR succeeds while running from a
    different cwd reproduces the GAP-39 failure mode.

    Skipped when ansible-playbook is not installed (CI environments without
    Ansible). The pre-push verification suite must run this in an env that
    has it.
    """

    def _ansible_playbook_available(self) -> bool:
        return shutil.which("ansible-playbook") is not None

    def test_syntax_check_passes_when_cwd_is_ansible_dir(self, tmp_path):
        if not self._ansible_playbook_available():
            pytest.skip("ansible-playbook not installed")
        result = subprocess.run(
            ["ansible-playbook", str(PLAYBOOK_SITE), "--syntax-check",
             "--inventory", "localhost,", "--connection", "local"],
            cwd=ANSIBLE_DIR,
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert result.returncode == 0, (
            f"ansible-playbook --syntax-check failed from cwd={ANSIBLE_DIR}.\n"
            f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )

    def test_syntax_check_fails_when_cwd_is_wrong(self, tmp_path):
        """Synthetic injection: remove the cwd fix's effect by running from
        an unrelated directory. The playbook MUST fail to resolve `role: common`,
        proving the cwd argument is load-bearing."""
        if not self._ansible_playbook_available():
            pytest.skip("ansible-playbook not installed")
        # Run from tmp_path with empty ANSIBLE_CONFIG so the repo's ansible.cfg
        # is not auto-loaded by any environment variable.
        import os
        env = os.environ.copy()
        env.pop("ANSIBLE_CONFIG", None)
        # Explicitly unset roles_path via env to avoid user-level overrides.
        env["ANSIBLE_ROLES_PATH"] = str(tmp_path / "nonexistent-roles")
        result = subprocess.run(
            ["ansible-playbook", str(PLAYBOOK_SITE), "--syntax-check",
             "--inventory", "localhost,", "--connection", "local"],
            cwd=tmp_path,
            capture_output=True,
            text=True,
            timeout=60,
            env=env,
        )
        assert result.returncode != 0, (
            "Expected ansible-playbook --syntax-check to FAIL when run from a "
            "wrong cwd without ansible.cfg, but it succeeded. This means the "
            "cwd argument may not be load-bearing and the regression guard is "
            "ineffective.\nstdout:\n" + result.stdout + "\nstderr:\n" + result.stderr
        )
        # And the failure should be the specific role-resolution failure we
        # are guarding against.
        combined = (result.stdout + result.stderr).lower()
        assert "common" in combined and (
            "not found" in combined or "could not find" in combined
        ), (
            "Wrong-cwd failure was not the expected 'role common not found' "
            "error. Got:\nstdout:\n" + result.stdout + "\nstderr:\n" + result.stderr
        )
