"""PR-P: Debian 12 PEP 668 fix — no system-Python pip install.

GAP-42 discovered in dogfood 9w ansible_run: the common role had

    - name: Install Docker Python library
      pip:
        name: [docker, docker-compose]
        executable: pip3

which fails on Debian 12 with 'error: externally-managed-environment'.
The PEP 668 marker file PRECLUDES system-wide pip installation. Fix:
drop the pip task entirely and rely on python3-docker (apt) for the
community.docker.docker_compose_v2 module's import requirement. The
legacy docker-compose pip package is unused (docker_compose_v2 invokes
the v2 CLI, shipped via docker-compose-plugin which is already
apt-installed).

These tests pin the contract so the pip-on-system regression cannot
silently return.
"""

import re
from pathlib import Path

import yaml


REPO = Path(__file__).resolve().parent.parent
COMMON_TASKS = REPO / "ansible" / "roles" / "common" / "tasks" / "main.yml"


def _load_tasks() -> list[dict]:
    with COMMON_TASKS.open() as f:
        data = yaml.safe_load(f)
    assert isinstance(data, list), "common role tasks must be a list"
    return data


def _task_modules(tasks: list[dict]) -> list[str]:
    """Return the module name used by each task (e.g. 'apt', 'pip',
    'community.docker.docker_compose_v2')."""
    modules = []
    builtin_keys = {"name", "when", "register", "changed_when", "failed_when",
                    "ignore_errors", "tags", "loop", "with_items", "vars",
                    "become", "become_user", "notify", "block", "rescue",
                    "always", "delegate_to", "run_once", "no_log", "args",
                    "environment", "retries", "delay", "until"}
    for t in tasks:
        if not isinstance(t, dict):
            continue
        for k in t.keys():
            if k not in builtin_keys:
                modules.append(k)
                break
    return modules


class TestNoPipOnSystemPython:
    def test_no_pip_module_in_common_role(self):
        tasks = _load_tasks()
        modules = _task_modules(tasks)
        assert "pip" not in modules, (
            "The common role must not invoke the pip module against the "
            "system Python on Debian 12 (PEP 668). Use python3-docker via "
            "apt instead. See PR-P / GAP-42."
        )

    def test_no_docker_compose_legacy_pip_anywhere(self):
        """docker-compose v1 (the Python package) is unused; community.docker.
        docker_compose_v2 calls the docker compose CLI shipped by
        docker-compose-plugin (apt). Make sure no role re-introduces it."""
        roles_dir = REPO / "ansible" / "roles"
        for yml in roles_dir.rglob("*.yml"):
            content = yml.read_text()
            # Allow the comment that explains the removal, but no actual
            # task that lists docker-compose as a pip package.
            for m in re.finditer(
                r"^\s*name:\s*['\"]?docker-compose['\"]?\s*$",
                content, re.MULTILINE,
            ):
                # If this 'name:' appears in a context where the parent
                # module is 'pip', that's the regression. Crude but
                # effective: scan 10 lines above for 'pip:'.
                start = max(0, content.rfind("\n", 0, m.start()) - 500)
                window = content[start:m.start()]
                assert "pip:" not in window.split("apt:")[-1], (
                    f"{yml}: docker-compose listed under pip task — this is "
                    "the GAP-42 regression. Use apt python3-docker instead."
                )


class TestPython3DockerInstalled:
    def test_apt_dependencies_include_python3_docker(self):
        tasks = _load_tasks()
        # Find any apt task whose name contains 'Docker dependencies' or
        # which lists python3-docker in pkg/name.
        found = False
        for t in tasks:
            apt = t.get("apt")
            if not isinstance(apt, dict):
                continue
            names = apt.get("pkg") or apt.get("name") or []
            if isinstance(names, str):
                names = [names]
            if "python3-docker" in names:
                found = True
                break
        assert found, (
            "common role must install python3-docker via apt so "
            "community.docker.docker_compose_v2 can import the docker SDK "
            "on Debian 12 without invoking pip."
        )

    def test_python3_pip_no_longer_required(self):
        """python3-pip was only there to enable the removed pip task. With
        the pip task gone, python3-pip should not appear either — keeping
        it would invite future regressions ('pip is available, just use
        it')."""
        content = COMMON_TASKS.read_text()
        # Only flag if python3-pip appears under an apt pkg list (not in
        # a comment).
        for line in content.splitlines():
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            assert "python3-pip" not in stripped, (
                "common role no longer needs python3-pip on the VM; remove "
                "it to discourage re-adding pip tasks."
            )


class TestDockerComposePluginStillPresent:
    """Defense-in-depth: removing python3-pip and the pip task must not
    accidentally drop docker-compose-plugin, which provides the v2 CLI."""

    def test_docker_compose_plugin_is_installed(self):
        tasks = _load_tasks()
        for t in tasks:
            apt = t.get("apt")
            if not isinstance(apt, dict):
                continue
            names = apt.get("pkg") or apt.get("name") or []
            if isinstance(names, str):
                names = [names]
            if "docker-compose-plugin" in names:
                return
        raise AssertionError(
            "docker-compose-plugin must remain apt-installed; "
            "community.docker.docker_compose_v2 depends on its CLI."
        )
