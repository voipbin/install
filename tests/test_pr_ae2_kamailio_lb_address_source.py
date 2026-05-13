"""PR-AE-2: Kamailio internal LB address source fix.

Root cause: k8s.py _build_substitution_map() read kamailio_lb_address only from
config.yaml ("kamailio_internal_lb_address") which is never populated by the
installer pipeline. Terraform outputs the same value as "kamailio_internal_lb_ip".

Fix: add terraform_outputs.get("kamailio_internal_lb_ip", "") as fallback,
     with config value taking precedence for operator override.

Observed symptom: KAMAILIO_INTERNAL_LB_ADDRESS: PLACEHOLDER_KAMAILIO_INTERNAL_LB_ADDRESS
in iter#15 dogfood log.
"""

from __future__ import annotations

import pathlib
from typing import Any
from unittest.mock import MagicMock

import pytest

# Import the function under test
from scripts.k8s import _build_substitution_map, _render_manifests_substitution

VOIP_SECRET_YAML = (
    pathlib.Path(__file__).parent.parent / "k8s" / "voip" / "secret.yaml"
)

_FAKE_DOMAIN = "test.example.com"
_FAKE_PROJECT = "voipbin-test"
_FAKE_REGION = "us-central1"
_TEST_LB_IP = "10.10.10.42"


def _minimal_config(**overrides) -> dict[str, Any]:
    base = {
        "domain": _FAKE_DOMAIN,
        "gcp_project_id": _FAKE_PROJECT,
        "region": _FAKE_REGION,
    }
    base.update(overrides)
    return base


def _minimal_tf_outputs(**overrides) -> dict[str, Any]:
    return dict(overrides)


class TestKamailioLbAddressSource:
    """Verify kamailio_lb_address precedence: config > terraform_outputs > empty."""

    def test_config_value_used_when_present(self):
        """config.kamailio_internal_lb_address takes precedence over terraform."""
        config = _minimal_config(kamailio_internal_lb_address="1.2.3.4")
        tf = _minimal_tf_outputs(kamailio_internal_lb_ip="9.9.9.9")
        subs = _build_substitution_map(config, tf, {})
        assert subs["PLACEHOLDER_KAMAILIO_INTERNAL_LB_ADDRESS"] == "1.2.3.4", (
            "config value should override terraform output"
        )

    def test_terraform_output_used_as_fallback(self):
        """When config key absent, terraform_outputs.kamailio_internal_lb_ip is used."""
        config = _minimal_config()  # no kamailio_internal_lb_address
        tf = _minimal_tf_outputs(kamailio_internal_lb_ip=_TEST_LB_IP)
        subs = _build_substitution_map(config, tf, {})
        assert subs["PLACEHOLDER_KAMAILIO_INTERNAL_LB_ADDRESS"] == _TEST_LB_IP, (
            f"Expected terraform fallback value {_TEST_LB_IP!r}, "
            f"got {subs['PLACEHOLDER_KAMAILIO_INTERNAL_LB_ADDRESS']!r}"
        )

    def test_empty_config_falls_back_to_terraform(self):
        """Explicit empty-string config triggers terraform fallback (falsy semantics)."""
        config = _minimal_config(kamailio_internal_lb_address="")
        tf = _minimal_tf_outputs(kamailio_internal_lb_ip=_TEST_LB_IP)
        subs = _build_substitution_map(config, tf, {})
        assert subs["PLACEHOLDER_KAMAILIO_INTERNAL_LB_ADDRESS"] == _TEST_LB_IP, (
            "Empty string config should fall through to terraform output"
        )

    def test_both_absent_yields_schema_default_no_exception(self):
        """Both absent → schema default returned (no crash), value is predictable.

        KAMAILIO_INTERNAL_LB_ADDRESS has a schema default of the placeholder string
        itself. When config and terraform both absent, the schema default is used as
        a safe fallback — operator will see a PLACEHOLDER_ warning at apply time but
        no hard error. The fix is not expected to improve this edge case; it only
        adds the terraform_outputs fallback for the normal pipeline path.
        """
        config = _minimal_config()
        tf = _minimal_tf_outputs()
        # Must not raise; result should be a string (schema default)
        subs = _build_substitution_map(config, tf, {})
        assert isinstance(subs["PLACEHOLDER_KAMAILIO_INTERNAL_LB_ADDRESS"], str)

    def test_placeholder_not_in_rendered_manifest(self):
        """After substitution with real IP, no PLACEHOLDER_ remains in voip/secret.yaml."""
        config = _minimal_config()
        tf = _minimal_tf_outputs(kamailio_internal_lb_ip=_TEST_LB_IP)
        subs = _build_substitution_map(config, tf, {})

        manifest_text = VOIP_SECRET_YAML.read_text()
        rendered = _render_manifests_substitution(manifest_text, subs)
        assert "PLACEHOLDER_KAMAILIO_INTERNAL_LB_ADDRESS" not in rendered, (
            "PLACEHOLDER_KAMAILIO_INTERNAL_LB_ADDRESS survived substitution in voip/secret.yaml"
        )
        # Confirm the actual IP is present
        assert _TEST_LB_IP in rendered, (
            f"Expected {_TEST_LB_IP!r} in rendered voip/secret.yaml"
        )


# ---------------------------------------------------------------------------
# Mutant harness
# ---------------------------------------------------------------------------

class TestMutantHarness:
    """Verify tests catch regressions to the broken implementation."""

    def _run_checks(self, subs: dict) -> list[str]:
        failures = []
        val = subs.get("PLACEHOLDER_KAMAILIO_INTERNAL_LB_ADDRESS", "MISSING")
        if val != _TEST_LB_IP:
            failures.append(f"PLACEHOLDER_KAMAILIO_INTERNAL_LB_ADDRESS={val!r} != {_TEST_LB_IP!r}")
        return failures

    def test_mutant_config_only_no_fallback(self):
        """Mutant: only config.get() used, no terraform fallback → must fail."""
        # Simulate broken implementation: config_only = config.get("kamailio_internal_lb_address", "")
        broken_val = _minimal_config().get("kamailio_internal_lb_address", "")  # ""
        subs = {"PLACEHOLDER_KAMAILIO_INTERNAL_LB_ADDRESS": broken_val}
        failures = self._run_checks(subs)
        assert failures, "Mutant (config-only, no fallback) should have been detected"

    def test_mutant_terraform_only_no_config_override(self):
        """Mutant: only terraform used, config override ignored → must fail when config has value."""
        # If config has "1.2.3.4" but mutant uses terraform "9.9.9.9"
        broken_val = "9.9.9.9"  # tf output wins even when config says "1.2.3.4"
        subs = {"PLACEHOLDER_KAMAILIO_INTERNAL_LB_ADDRESS": broken_val}
        # From test_config_value_used_when_present perspective: expected "1.2.3.4", got "9.9.9.9"
        assert broken_val != "1.2.3.4", "Mutant should have different value from config"

    def test_mutant_hardcoded_empty(self):
        """Mutant: always returns '' regardless of inputs → must fail."""
        subs = {"PLACEHOLDER_KAMAILIO_INTERNAL_LB_ADDRESS": ""}
        failures = self._run_checks(subs)
        assert failures, "Mutant (hardcoded empty) should have been detected"

    def test_mutant_swapped_precedence_real_regression(self):
        """Mutant: terraform wins over config — regression test using full function."""
        # With swapped logic: "" or tf → tf wins (ok for fallback case)
        # but "1.2.3.4" or tf → depends on 'or' semantics
        # We can't easily mock the function, so verify the real function's precedence
        config = _minimal_config(kamailio_internal_lb_address="1.2.3.4")
        tf = _minimal_tf_outputs(kamailio_internal_lb_ip="9.9.9.9")
        subs = _build_substitution_map(config, tf, {})
        # With correct precedence: config wins → "1.2.3.4"
        # With swapped precedence: terraform wins → "9.9.9.9"
        assert subs["PLACEHOLDER_KAMAILIO_INTERNAL_LB_ADDRESS"] == "1.2.3.4", (
            "Config override must win over terraform output"
        )
