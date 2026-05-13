"""PR-U-3: Kamailio heplify-client sidecar + HOMER_URI wiring tests.

17 cases across 8 classes mirroring design v3 §7.2.
"""

from __future__ import annotations

import json
import re
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

from scripts import ansible_runner as ar_mod
from scripts import preflight as preflight_mod

REPO_ROOT = Path(__file__).resolve().parent.parent
COMPOSE_J2 = REPO_ROOT / "ansible" / "roles" / "kamailio" / "templates" / "docker-compose.yml.j2"
ENV_J2 = REPO_ROOT / "ansible" / "roles" / "kamailio" / "templates" / "env.j2"
GROUP_VARS = REPO_ROOT / "ansible" / "inventory" / "group_vars" / "kamailio.yml"
ANSIBLE_RUNNER_PY = REPO_ROOT / "scripts" / "ansible_runner.py"
SCHEMA_PY = REPO_ROOT / "config" / "schema.py"


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


# ============================================================================
# §7.2 TestDockerComposeShape — 5 cases.
# ============================================================================

class TestDockerComposeShape:
    """5 cases. docker-compose.yml.j2 sidecar block has correct shape."""

    def test_sidecar_block_present(self):
        text = _read(COMPOSE_J2)
        assert "heplify-client:" in text, "heplify-client service block missing"
        assert "image: sipcapture/heplify:1.56" in text, (
            "Sidecar image must be sipcapture/heplify:1.56 (production parity)"
        )

    def test_network_mode_host(self):
        text = _read(COMPOSE_J2)
        # Find the heplify-client block and assert network_mode: host inside it
        m = re.search(
            r"heplify-client:\s*\n(.*?)(?=^\s{2}\w+:|\Z)",
            text, re.DOTALL | re.MULTILINE,
        )
        assert m, "could not isolate heplify-client block"
        assert re.search(r"network_mode:\s*host", m.group(1)), (
            "Sidecar must use network_mode: host for -i any sniffing"
        )

    def test_restart_unless_stopped(self):
        text = _read(COMPOSE_J2)
        m = re.search(r"heplify-client:.*?logging:", text, re.DOTALL)
        assert m, "could not isolate heplify-client block"
        assert "restart: unless-stopped" in m.group(0), (
            "Sidecar must have restart: unless-stopped"
        )

    def test_depends_on_kamailio_short_form(self):
        text = _read(COMPOSE_J2)
        m = re.search(r"heplify-client:.*?logging:", text, re.DOTALL)
        assert m, "could not isolate heplify-client block"
        # Short-form list, NOT long-form condition: service_healthy
        # (Kamailio has no healthcheck — long form would block forever)
        body = m.group(0)
        assert re.search(r"depends_on:\s*\n\s+-\s*kamailio", body), (
            "Sidecar depends_on must use short-form list (- kamailio)"
        )
        assert "condition: service_healthy" not in body, (
            "Sidecar must NOT use long-form service_healthy (Kamailio has no healthcheck)"
        )

    def test_command_argv_production_parity(self):
        text = _read(COMPOSE_J2)
        m = re.search(r"heplify-client:.*?(?=depends_on:)", text, re.DOTALL)
        assert m, "could not isolate heplify-client block"
        body = m.group(0)
        # All four production argv items must be present in order.
        for arg in ['"-i"', '"any"', '"-hs"', '"${HOMER_URI}"',
                    '"-m"', '"SIP"', '"-dim"', '"REGISTER"']:
            assert arg in body, f"missing argv element {arg} (production parity)"


# ============================================================================
# §7.2 TestComposeJinjaGate — 1 case. Mutant #14 catcher.
# ============================================================================

class TestComposeJinjaGate:
    """1 case. Sidecar block is wrapped in Jinja conditional."""

    def test_jinja_gate_wraps_sidecar(self):
        text = _read(COMPOSE_J2)
        # The `{% if homer_enabled | bool and heplify_lb_ip %}` MUST appear
        # before `heplify-client:` and `{% endif %}` MUST appear after.
        idx_if = text.find("{% if homer_enabled")
        idx_client = text.find("heplify-client:")
        idx_endif = text.find("{% endif %}", idx_client)
        assert idx_if != -1, "missing {% if homer_enabled ... %} gate"
        assert idx_client != -1, "missing heplify-client block"
        assert idx_endif != -1, "missing {% endif %} after heplify-client"
        assert idx_if < idx_client < idx_endif, (
            "Jinja gate must wrap heplify-client (if -> client -> endif order)"
        )
        # Gate must include both conditions
        gate_line = text[idx_if : text.index("%}", idx_if) + 2]
        assert "homer_enabled" in gate_line and "heplify_lb_ip" in gate_line, (
            f"gate must check both homer_enabled AND heplify_lb_ip; got: {gate_line}"
        )


# ============================================================================
# §7.2 TestEnvJ2Wiring — 2 cases. Regression guards.
# ============================================================================

class TestEnvJ2Wiring:
    """2 cases. env.j2 HOMER_URI/HOMER_ENABLED Jinja vars remain wired."""

    def test_homer_uri_jinja_var(self):
        text = _read(ENV_J2)
        assert "HOMER_URI={{ homer_uri }}" in text, (
            "env.j2 must keep HOMER_URI={{ homer_uri }} regression guard"
        )

    def test_homer_enabled_jinja_var(self):
        text = _read(ENV_J2)
        assert "HOMER_ENABLED={{ homer_enabled }}" in text, (
            "env.j2 must keep HOMER_ENABLED={{ homer_enabled }} regression guard"
        )


# ============================================================================
# §7.2 TestGroupVarsDefaults — 2 cases.
# ============================================================================

class TestGroupVarsDefaults:
    """2 cases. group_vars/kamailio.yml carries the new homer_* literals."""

    def test_homer_uri_jinja_gated(self):
        text = _read(GROUP_VARS)
        # Exact line (Jinja-quoted defense-in-depth)
        expected = (
            'homer_uri: "{% if heplify_lb_ip %}{{ heplify_lb_ip }}:9060{% endif %}"'
        )
        assert expected in text, (
            f"group_vars must carry exact homer_uri line:\n  {expected}"
        )

    def test_homer_enabled_literal_true(self):
        text = _read(GROUP_VARS)
        # Literal "true" baseline (no _flag suffix indirection per iter-2 B2)
        assert re.search(r'^homer_enabled:\s*"true"\s*$', text, re.MULTILINE), (
            'group_vars must carry homer_enabled: "true" literal'
        )


# ============================================================================
# §7.2 TestPreflightGate — 3 cases.
# ============================================================================

class TestPreflightGate:
    """3 cases. check_kamailio_homer_uri_present honors the gates."""

    def test_empty_ip_homer_enabled_true_raises(self):
        config = {"homer_enabled": True}
        with pytest.raises(preflight_mod.PreflightError) as excinfo:
            preflight_mod.check_kamailio_homer_uri_present(
                {"heplify_lb_ip": ""}, config
            )
        msg = str(excinfo.value).lower()
        assert "homer" in msg and "heplify_lb_ip" in msg, (
            "error must name HOMER + heplify_lb_ip for operator clarity"
        )

    def test_nonempty_ip_homer_enabled_true_passes(self):
        config = {"homer_enabled": True}
        result = preflight_mod.check_kamailio_homer_uri_present(
            {"heplify_lb_ip": "10.108.0.5"}, config
        )
        assert result is None

    def test_empty_ip_homer_enabled_false_noop(self):
        config = {"homer_enabled": False}
        # No raise even with empty IP — operator disabled HOMER
        result = preflight_mod.check_kamailio_homer_uri_present(
            {"heplify_lb_ip": ""}, config
        )
        assert result is None


# ============================================================================
# §7.2 TestPreflightRegistration — 1 case. Mutant #12 catcher.
# ============================================================================

class TestPreflightRegistration:
    """1 case. ansible_run invokes check_kamailio_homer_uri_present."""

    def test_ansible_run_imports_and_invokes(self):
        src = _read(ANSIBLE_RUNNER_PY)
        # Multi-line import block tolerated
        assert re.search(
            r"from\s+scripts\.preflight\s+import\s*\([^)]*check_kamailio_homer_uri_present",
            src, re.DOTALL,
        ), "ansible_run must import check_kamailio_homer_uri_present from preflight"
        assert "check_kamailio_homer_uri_present(terraform_outputs, config)" in src, (
            "ansible_run must invoke check_kamailio_homer_uri_present(terraform_outputs, config)"
        )


# ============================================================================
# §7.2 TestAnsibleFlatVarWiring — 2 cases.
# ============================================================================

class TestAnsibleFlatVarWiring:
    """2 cases. ansible_runner emits heplify_lb_ip (regression) + homer_enabled (new)."""

    def _invoke(self, config_data: dict, terraform_outputs: dict) -> dict:
        """Call _write_extra_vars with a MagicMock config and load resulting JSON."""
        from unittest.mock import MagicMock
        cfg = MagicMock()
        cfg.to_ansible_vars.return_value = dict(config_data)
        cfg.get.side_effect = lambda key, default="": config_data.get(key, default)
        path = ar_mod._write_extra_vars(cfg, terraform_outputs)
        try:
            return json.loads(Path(path).read_text())
        finally:
            Path(path).unlink(missing_ok=True)

    def test_heplify_lb_ip_emitted(self):
        # PR-U-1 regression guard
        cfg = {"homer_enabled": True}
        outs = {"heplify_lb_ip": "10.108.0.5"}
        ev = self._invoke(cfg, outs)
        assert ev.get("heplify_lb_ip") == "10.108.0.5", (
            "heplify_lb_ip flat-var must still be emitted (PR-U-1 regression)"
        )

    def test_homer_enabled_emitted_direct_key(self):
        # PR-U-3: direct key, no _flag suffix
        cfg = {"homer_enabled": False}
        outs = {"heplify_lb_ip": "10.108.0.5"}
        ev = self._invoke(cfg, outs)
        assert ev.get("homer_enabled") == "false", (
            "homer_enabled flat-var must be 'false' string when config disables"
        )
        # Default True case
        cfg2 = {}
        ev2 = self._invoke(cfg2, outs)
        assert ev2.get("homer_enabled") == "true", (
            "homer_enabled flat-var must default to 'true' string"
        )
        # MUST NOT carry the _flag suffix from iter-1 design
        assert "homer_enabled_flag" not in ev2, (
            "homer_enabled_flag must NOT exist (iter-2 dropped the indirection)"
        )


# ============================================================================
# §7.2 TestConfigSchemaDefault — 1 case.
# ============================================================================

class TestConfigSchemaDefault:
    """1 case. config schema declares homer_enabled boolean property."""

    def test_schema_has_homer_enabled_boolean(self):
        text = _read(SCHEMA_PY)
        # The key must be declared under properties (otherwise additionalProperties:False
        # rejects operator overrides).
        assert '"homer_enabled"' in text, (
            "schema must declare homer_enabled key under properties"
        )
        # Locate the block and check type=boolean
        m = re.search(
            r'"homer_enabled"\s*:\s*\{[^}]*?"type"\s*:\s*"boolean"',
            text, re.DOTALL,
        )
        assert m, "homer_enabled must have type: boolean in schema"
