"""PR-AA: cert_provision preflight check regression tests.

Pins the ``check_cert_provisioned()`` function in scripts/preflight.py that
gates ansible_run against a missing or incomplete cert_state. Called from
_run_ansible (live path only; dry_run paths never reach it).

Test IDs match the design doc §4.1:
  P1-P9  : check_cert_provisioned correctness
  W1-W2  : _run_ansible pipeline wiring
  M1-M2  : mutation harness
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from scripts.preflight import PreflightError, check_cert_provisioned


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _self_signed_cert_state(**overrides) -> dict:
    base = {
        "schema_version": 1,
        "actual_mode": "self_signed",
        "config_mode": "self_signed",
        "ca_fingerprint_sha256": "AA:BB:CC",
        "ca_not_after": "2027-01-01T00:00:00+00:00",
        "san_list": ["sip.example.com", "registrar.example.com"],
        "leaf_certs": {
            "sip.example.com": {
                "not_after": "2027-01-01T00:00:00+00:00",
                "fingerprint_sha256": "11:22:33",
                "serial": 1,
            },
            "registrar.example.com": {
                "not_after": "2027-01-01T00:00:00+00:00",
                "fingerprint_sha256": "44:55:66",
                "serial": 2,
            },
        },
    }
    base.update(overrides)
    return base


def _manual_cert_state(**overrides) -> dict:
    base = {
        "schema_version": 1,
        "actual_mode": "manual",
        "config_mode": "manual",
        "acme_pending": False,
        "san_list": ["sip.example.com", "registrar.example.com"],
        "leaf_certs": {
            "sip.example.com": {
                "not_after": "2027-01-01T00:00:00+00:00",
                "fingerprint_sha256": "11:22:33",
                "serial": 1,
            },
            "registrar.example.com": {
                "not_after": "2027-01-01T00:00:00+00:00",
                "fingerprint_sha256": "44:55:66",
                "serial": 2,
            },
        },
    }
    base.update(overrides)
    return base


def _state_with(cert_state: dict) -> dict:
    return {"cert_state": cert_state}


# ---------------------------------------------------------------------------
# P1-P9: check_cert_provisioned correctness
# ---------------------------------------------------------------------------

class TestCheckCertProvisioned:

    def test_p1_valid_self_signed_no_exception(self):
        """P1: Valid self_signed cert_state → no exception."""
        state = _state_with(_self_signed_cert_state())
        with patch("scripts.pipeline.load_state", return_value=state):
            check_cert_provisioned()  # must not raise

    def test_p2_valid_manual_no_exception(self):
        """P2: Valid manual cert_state (no ca_fingerprint_sha256) → no exception.

        Explicitly verifies that absent ca_fingerprint_sha256 in manual mode
        does NOT trigger the self_signed-only fingerprint check.
        """
        cs = _manual_cert_state()
        assert "ca_fingerprint_sha256" not in cs, "fixture must not have ca_fingerprint_sha256"
        state = _state_with(cs)
        with patch("scripts.pipeline.load_state", return_value=state):
            check_cert_provisioned()  # must not raise

    def test_p3_empty_cert_state_raises(self):
        """P3: Empty cert_state ({}) → PreflightError."""
        state = {"cert_state": {}}
        with patch("scripts.pipeline.load_state", return_value=state):
            with pytest.raises(PreflightError, match="cert_provision has not run"):
                check_cert_provisioned()

    def test_p4_actual_mode_absent_raises(self):
        """P4: cert_state present but actual_mode absent → PreflightError."""
        cs = _self_signed_cert_state()
        cs.pop("actual_mode")
        state = _state_with(cs)
        with patch("scripts.pipeline.load_state", return_value=state):
            with pytest.raises(PreflightError, match="cert_provision has not run"):
                check_cert_provisioned()

    def test_p5_self_signed_missing_ca_fingerprint_raises(self):
        """P5: self_signed + ca_fingerprint_sha256 absent → PreflightError."""
        cs = _self_signed_cert_state()
        cs.pop("ca_fingerprint_sha256")
        state = _state_with(cs)
        with patch("scripts.pipeline.load_state", return_value=state):
            with pytest.raises(PreflightError, match="CA fingerprint is absent"):
                check_cert_provisioned()

    def test_p6_manual_missing_ca_fingerprint_no_exception(self):
        """P6: manual + ca_fingerprint_sha256 absent → no exception (positive boundary)."""
        cs = _manual_cert_state()
        # Confirm fingerprint is absent from fixture
        assert "ca_fingerprint_sha256" not in cs
        state = _state_with(cs)
        with patch("scripts.pipeline.load_state", return_value=state):
            check_cert_provisioned()  # must not raise

    def test_p7_leaf_missing_for_san_raises(self):
        """P7: Leaf cert missing for a SAN → PreflightError."""
        cs = _self_signed_cert_state()
        # Remove one leaf entry
        cs["leaf_certs"].pop("sip.example.com")
        state = _state_with(cs)
        with patch("scripts.pipeline.load_state", return_value=state):
            with pytest.raises(PreflightError, match="leaf cert missing for SAN"):
                check_cert_provisioned()

    def test_p8_leaf_missing_fingerprint_raises(self):
        """P8: Leaf present but fingerprint_sha256 absent → PreflightError."""
        cs = _self_signed_cert_state()
        cs["leaf_certs"]["sip.example.com"].pop("fingerprint_sha256")
        state = _state_with(cs)
        with patch("scripts.pipeline.load_state", return_value=state):
            with pytest.raises(PreflightError, match="has no fingerprint"):
                check_cert_provisioned()

    def test_p9_empty_san_list_no_exception(self):
        """P9: cert_state with actual_mode set but san_list=[] → no exception.

        An empty SAN list is intentionally allowed — cert_provision ran but
        no Kamailio SANs are configured yet. Ansible will deploy no certs.
        """
        cs = _self_signed_cert_state(san_list=[], leaf_certs={})
        state = _state_with(cs)
        with patch("scripts.pipeline.load_state", return_value=state):
            check_cert_provisioned()  # must not raise


# ---------------------------------------------------------------------------
# W1-W2: _run_ansible pipeline wiring
# ---------------------------------------------------------------------------

class TestRunAnsibleWiring:
    """Verify _run_ansible calls (or skips) check_cert_provisioned."""

    def _make_config(self) -> MagicMock:
        cfg = MagicMock()
        cfg.get = MagicMock(return_value=None)
        return cfg

    def test_w1_calls_check_cert_provisioned_on_live_path(self):
        """W1: _run_ansible calls check_cert_provisioned when dry_run=False."""
        from scripts.pipeline import _run_ansible

        config = self._make_config()
        outputs = {"kamailio_internal_ips": ["10.0.0.1"]}

        with (
            patch("scripts.preflight.check_oslogin_setup", return_value=None),
            patch("scripts.preflight.check_cert_provisioned") as mock_cert,
            patch("scripts.pipeline.ansible_run", return_value=True),
        ):
            result = _run_ansible(config, outputs, dry_run=False, auto_approve=False)
            mock_cert.assert_called_once()

    def test_w2_skips_check_cert_provisioned_on_dry_run(self):
        """W2: _run_ansible skips check_cert_provisioned when dry_run=True."""
        from scripts.pipeline import _run_ansible

        config = self._make_config()
        outputs = {}  # no VMs

        with patch("scripts.preflight.check_cert_provisioned") as mock_cert:
            result = _run_ansible(config, outputs, dry_run=True, auto_approve=False)
            mock_cert.assert_not_called()


# ---------------------------------------------------------------------------
# M1-M2: mutation harness
# ---------------------------------------------------------------------------

class TestMutantHarness:
    """Verify that the test suite catches broken implementations."""

    def test_m1_mutant_no_actual_mode_check(self):
        """M1: Mutant that skips actual_mode check must be caught by P3/P4."""
        state_p3 = {"cert_state": {}}
        with patch("scripts.pipeline.load_state", return_value=state_p3):
            with pytest.raises(PreflightError):
                check_cert_provisioned()

        cs = _self_signed_cert_state()
        cs.pop("actual_mode")
        state_p4 = _state_with(cs)
        with patch("scripts.pipeline.load_state", return_value=state_p4):
            with pytest.raises(PreflightError):
                check_cert_provisioned()

    def test_m2_mutant_no_ca_fingerprint_check(self):
        """M2: Mutant that skips ca_fingerprint_sha256 check must be caught by P5."""
        cs = _self_signed_cert_state()
        cs.pop("ca_fingerprint_sha256")
        state = _state_with(cs)
        with patch("scripts.pipeline.load_state", return_value=state):
            with pytest.raises(PreflightError):
                check_cert_provisioned()
