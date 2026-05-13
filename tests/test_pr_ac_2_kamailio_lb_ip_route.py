"""PR-AC-2 — Forwarded-LB-IP local-route shim static tests.

These tests assert structural invariants of the ansible task block + the two
Jinja templates that own the systemd oneshot + bash shim. They are designed
to catch the 11 mutations described in
``notes/2026-05-13-pr-ac-2-design.md`` (see also
``scripts/dev/pr_ac_2_mutant_harness.py``).

No subprocess / no live VM dependency. Pure file-system parsing.
"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parent.parent
KAMAILIO_TASKS = REPO / "ansible" / "roles" / "kamailio" / "tasks" / "main.yml"
SHIM_TEMPLATE = REPO / "ansible" / "roles" / "kamailio" / "templates" / "voipbin-kamailio-lb-routes.sh.j2"
UNIT_TEMPLATE = REPO / "ansible" / "roles" / "kamailio" / "templates" / "voipbin-kamailio-lb-routes.service.j2"


# --- helpers ---------------------------------------------------------------


def _load_tasks() -> list[dict]:
    return yaml.safe_load(KAMAILIO_TASKS.read_text())


def _tasks_named(prefix: str) -> list[dict]:
    return [t for t in _load_tasks() if t.get("name", "").startswith(prefix)]


def _index_of(name_prefix: str) -> int:
    for i, t in enumerate(_load_tasks()):
        if t.get("name", "").startswith(name_prefix):
            return i
    raise AssertionError(f"task with name prefix {name_prefix!r} not found")


# --- M1, M2, M11: presence + become ---------------------------------------


class TestShimTasksPresent:
    """All three shim tasks must be present with become=true."""

    @pytest.mark.parametrize(
        "task_name",
        [
            "Install forwarded-LB-IP shim script",
            "Install forwarded-LB-IP systemd oneshot unit",
            "Enable and start forwarded-LB-IP shim",
        ],
    )
    def test_task_present(self, task_name: str) -> None:
        matches = [t for t in _load_tasks() if t.get("name") == task_name]
        assert len(matches) == 1, f"expected exactly one task {task_name!r}, got {len(matches)}"

    @pytest.mark.parametrize(
        "task_name",
        [
            "Install forwarded-LB-IP shim script",
            "Install forwarded-LB-IP systemd oneshot unit",
            "Enable and start forwarded-LB-IP shim",
        ],
    )
    def test_task_has_become_true(self, task_name: str) -> None:
        """R1 M11: become must be explicit on each shim task (not role-level only)."""
        matches = [t for t in _load_tasks() if t.get("name") == task_name]
        assert matches, f"task {task_name!r} missing"
        assert matches[0].get("become") is True, f"task {task_name!r} must have become: true"

    @pytest.mark.parametrize(
        "task_name",
        [
            "Install forwarded-LB-IP shim script",
            "Install forwarded-LB-IP systemd oneshot unit",
            "Enable and start forwarded-LB-IP shim",
        ],
    )
    def test_task_has_lb_route_tag(self, task_name: str) -> None:
        matches = [t for t in _load_tasks() if t.get("name") == task_name]
        assert matches, f"task {task_name!r} missing"
        tags = matches[0].get("tags", [])
        assert "lb-route" in tags, f"task {task_name!r} must include 'lb-route' tag"


# --- M4: ordering ---------------------------------------------------------


class TestOrdering:
    """The shim must run before docker compose pull/up."""

    def test_shim_before_pull(self) -> None:
        shim_idx = _index_of("Enable and start forwarded-LB-IP shim")
        pull_idx = _index_of("Pull latest Docker images")
        assert shim_idx < pull_idx, (
            f"shim (idx={shim_idx}) must run before docker pull (idx={pull_idx})"
        )

    def test_shim_after_env(self) -> None:
        env_idx = _index_of("Generate .env file from template")
        shim_idx = _index_of("Install forwarded-LB-IP shim script")
        assert env_idx < shim_idx, (
            f"shim install (idx={shim_idx}) should be after .env generation (idx={env_idx})"
        )


# --- M3, M9: variable references in shim script ---------------------------


class TestShimScript:
    """Static structure of the rendered shim shell script."""

    def test_script_template_exists(self) -> None:
        assert SHIM_TEMPLATE.exists(), f"missing shim template {SHIM_TEMPLATE}"

    def test_references_external_lb_ip_var(self) -> None:
        """R1 M3: must reference kamailio_external_lb_ip (the ansible_runner-injected name)."""
        body = SHIM_TEMPLATE.read_text()
        assert "kamailio_external_lb_ip" in body, "shim must reference kamailio_external_lb_ip"

    def test_references_internal_lb_ip_var(self) -> None:
        body = SHIM_TEMPLATE.read_text()
        assert "kamailio_internal_lb_ip" in body, "shim must reference kamailio_internal_lb_ip"

    def test_uses_ansible_default_interface(self) -> None:
        """R1 M9: must not hardcode interface name; use ansible_default_ipv4.interface."""
        body = SHIM_TEMPLATE.read_text()
        assert "ansible_default_ipv4.interface" in body, (
            "shim must derive interface via ansible_default_ipv4.interface, not hardcode"
        )

    def test_no_hardcoded_ens4(self) -> None:
        """Defensive: ensure no literal 'ens4' or 'eth0' slipped in."""
        body = SHIM_TEMPLATE.read_text()
        assert "ens4" not in body, "shim must not hardcode 'ens4'"
        assert "eth0" not in body, "shim must not hardcode 'eth0'"


# --- M5, M6, M10: idempotency + guards in shim script ---------------------


class TestShimIdempotency:
    """Shim must pre-check existence (any proto) and guard empty IPs."""

    def test_uses_match_type_local_precheck(self) -> None:
        """R1 M10: pre-check must use 'match <ip>/32 type local', not naive grep substring."""
        body = SHIM_TEMPLATE.read_text()
        assert "ip route show table local match" in body, (
            "pre-check must use 'ip route show table local match <ip>/32 type local'"
        )
        assert "type local" in body, "pre-check must filter type local to avoid substring matches"
        assert "/32" in body, "pre-check must scope to /32 prefix"

    def test_no_or_true_masking(self) -> None:
        """R1 #1: must NOT mask ip route add failures with '|| true'."""
        body = SHIM_TEMPLATE.read_text()
        assert "|| true" not in body, "shim must not mask ip route add with '|| true'"

    def test_empty_string_guard(self) -> None:
        """R1 M5: shim must skip empty IPs without erroring."""
        body = SHIM_TEMPLATE.read_text()
        assert "[ -z " in body or "[[ -z " in body, (
            "shim must guard empty IPs via [ -z ] check"
        )

    def test_uses_set_euo_pipefail(self) -> None:
        body = SHIM_TEMPLATE.read_text()
        assert "set -euo pipefail" in body, "shim must use 'set -euo pipefail' for strict mode"


# --- M7, M8: route type and scope -----------------------------------------


class TestRouteSemantics:
    """The 'ip route add' invocation must use 'local' type and 'scope host'."""

    def test_route_add_local(self) -> None:
        """R1 M7: route type must be 'local' (not 'unicast')."""
        body = SHIM_TEMPLATE.read_text()
        assert "ip route add local " in body, (
            "route add must specify type 'local' (kernel must own the IP)"
        )

    def test_route_scope_host(self) -> None:
        """R1 M8: route scope must be 'host'."""
        body = SHIM_TEMPLATE.read_text()
        assert "scope host" in body, "route add must use 'scope host'"
        # Harden against partial mutation: the actual `ip route add` line must
        # carry 'scope host', not just echo text.
        add_lines = [
            line for line in body.splitlines()
            if line.lstrip().startswith("ip route add")
        ]
        assert add_lines, "expected at least one 'ip route add' line in shim"
        for line in add_lines:
            assert "scope host" in line, (
                f"'ip route add' line missing 'scope host': {line!r}"
            )


# --- systemd unit semantics -----------------------------------------------


class TestSystemdUnit:
    """Oneshot unit must be ordered Before=docker.service After=network-online."""

    def test_unit_template_exists(self) -> None:
        assert UNIT_TEMPLATE.exists(), f"missing unit template {UNIT_TEMPLATE}"

    def test_type_oneshot(self) -> None:
        body = UNIT_TEMPLATE.read_text()
        assert "Type=oneshot" in body, "unit must be Type=oneshot"

    def test_remain_after_exit(self) -> None:
        body = UNIT_TEMPLATE.read_text()
        assert "RemainAfterExit=yes" in body, (
            "unit must have RemainAfterExit=yes so systemd treats it as active"
        )

    def test_before_docker(self) -> None:
        """Reboot-persistence ordering: routes must land before dockerd starts kamailio."""
        body = UNIT_TEMPLATE.read_text()
        assert "Before=docker.service" in body, "unit must be ordered Before=docker.service"

    def test_after_network_online(self) -> None:
        body = UNIT_TEMPLATE.read_text()
        assert "After=network-online.target" in body, (
            "unit must wait for network-online.target so default route is up"
        )
        assert "Wants=network-online.target" in body, (
            "unit must Wants=network-online.target to activate it"
        )

    def test_execstart_points_to_shim(self) -> None:
        body = UNIT_TEMPLATE.read_text()
        assert "ExecStart=/usr/local/sbin/voipbin-kamailio-lb-routes" in body, (
            "unit ExecStart must invoke the shim script at /usr/local/sbin/voipbin-kamailio-lb-routes"
        )
