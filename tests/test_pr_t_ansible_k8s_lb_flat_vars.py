"""Tests for PR-T: ansible_runner flat-var sweep for k8s LoadBalancer IPs.

PR-T extends `_write_extra_vars` so the 5 k8s LB IPs harvested by PR-R's
`reconcile_k8s_outputs` stage land as TOP-LEVEL Ansible vars consumed by
`ansible/roles/kamailio/templates/env.j2`. Without these flat-vars Kamailio
boots with empty REDIS_CACHE_ADDRESS / ASTERISK_*_LB_ADDR / RABBITMQ_URL host
and CrashLoops.

Flat-var keys must match `scripts.k8s._LB_SERVICES` tuple-3 column exactly:
  - redis_lb_ip
  - rabbitmq_lb_ip
  - asterisk_call_lb_ip
  - asterisk_registrar_lb_ip
  - asterisk_conference_lb_ip

Design philosophy mirrors PR-S (test_pr_s_ansible_flat_vars.py): defensive
`.get(..., "")` so dev / early-apply / dry-run flows do not crash before
harvest has populated outputs.
"""

import json
import os
from unittest.mock import MagicMock

import pytest

from scripts.ansible_runner import _write_extra_vars
from scripts.k8s import _LB_SERVICES


# Source-of-truth flat-var keys, derived from k8s._LB_SERVICES so test
# drift surfaces if either side changes unilaterally.
_FLAT_KEYS = [out_key for (_, _, out_key) in _LB_SERVICES]


def _make_config(data: dict) -> MagicMock:
    cfg = MagicMock()
    cfg.to_ansible_vars.return_value = dict(data)
    cfg.get.side_effect = lambda key, default="": data.get(key, default)
    return cfg


def _base_config() -> MagicMock:
    return _make_config(
        {
            "gcp_project_id": "test-proj",
            "region": "us-central1",
            "zone": "us-central1-a",
        }
    )


def _write_and_load(cfg: MagicMock, outputs: dict) -> dict:
    path = _write_extra_vars(cfg, outputs)
    try:
        return json.loads(path.read_text())
    finally:
        if path.exists():
            os.unlink(path)


class TestKeyContractMatchesK8sLBServices:
    """Source-of-truth invariant. ansible_runner flat-vars must mirror
    scripts.k8s._LB_SERVICES exactly. Drift here means env.j2 silently
    renders an empty slot in some flow and Kamailio CrashLoops."""

    def test_lb_services_has_expected_keys(self):
        # Sanity. Pin the 5-key contract so a refactor of _LB_SERVICES
        # that drops a Service is caught upstream of ansible.
        assert set(_FLAT_KEYS) == {
            "redis_lb_ip",
            "rabbitmq_lb_ip",
            "asterisk_call_lb_ip",
            "asterisk_registrar_lb_ip",
            "asterisk_conference_lb_ip",
        }


class TestK8sLbIpsLandAtTopLevel:
    """Each k8s LB IP from terraform_outputs (post-harvest/hydration) must
    appear at the JSON top level so env.j2 Jinja2 resolution succeeds."""

    @pytest.mark.parametrize(
        "key,value",
        [
            ("redis_lb_ip", "10.164.0.10"),
            ("rabbitmq_lb_ip", "10.164.0.11"),
            ("asterisk_call_lb_ip", "10.164.0.18"),
            ("asterisk_registrar_lb_ip", "10.164.0.20"),
            ("asterisk_conference_lb_ip", "10.164.0.21"),
        ],
    )
    def test_individual_key_lands_at_top_level(self, key, value):
        outputs = {
            "kamailio_internal_ips": [],
            "rtpengine_external_ips": [],
            "kamailio_external_lb_ip": "",
            "kamailio_internal_lb_ip": "10.0.0.2",
            key: value,
        }
        data = _write_and_load(_base_config(), outputs)
        assert data[key] == value, (
            f"flat-var {key} missing/wrong after _write_extra_vars. "
            f"Expected {value!r}, got {data.get(key)!r}."
        )

    def test_all_five_keys_present_together(self):
        outputs = {
            "kamailio_internal_ips": [],
            "rtpengine_external_ips": [],
            "kamailio_external_lb_ip": "",
            "kamailio_internal_lb_ip": "10.0.0.2",
            "redis_lb_ip": "10.164.0.10",
            "rabbitmq_lb_ip": "10.164.0.11",
            "asterisk_call_lb_ip": "10.164.0.18",
            "asterisk_registrar_lb_ip": "10.164.0.20",
            "asterisk_conference_lb_ip": "10.164.0.21",
        }
        data = _write_and_load(_base_config(), outputs)
        for k in _FLAT_KEYS:
            assert data[k] == outputs[k], f"flat-var {k} drift: {data.get(k)!r}"

    def test_separate_tcp_and_udp_asterisk_call_ips(self):
        """PR-R live-verified: asterisk-call has DISTINCT TCP and UDP LB IPs.
        Kamailio's env.j2 has one slot; harvest_loadbalancer_ips selects UDP.
        The flat-var must therefore carry the UDP IP unchanged from outputs
        (UDP selection is the harvester's job, not _write_extra_vars's)."""
        outputs = {
            "kamailio_internal_ips": [],
            "rtpengine_external_ips": [],
            # PR-R harvester resolved asterisk-call-udp -> 10.164.0.18,
            # asterisk-call-tcp -> 10.164.0.17 (unused by Kamailio).
            "asterisk_call_lb_ip": "10.164.0.18",
        }
        data = _write_and_load(_base_config(), outputs)
        assert data["asterisk_call_lb_ip"] == "10.164.0.18"


class TestDefensiveDefaultsForMissingKeys:
    """`.get(..., "")` keeps dev / early-apply / dry-run from crashing when
    harvest has not populated outputs yet. group_vars/kamailio.yml then
    supplies fallbacks where applicable (e.g. redis_cache_address)."""

    def test_missing_keys_default_to_empty_string(self):
        outputs = {
            "kamailio_internal_ips": [],
            "rtpengine_external_ips": [],
            "kamailio_external_lb_ip": "",
            # All 5 k8s LB keys deliberately absent.
        }
        data = _write_and_load(_base_config(), outputs)
        for k in _FLAT_KEYS:
            assert k in data, (
                f"flat-var {k} missing entirely. Must be set to '' default, "
                f"not omitted, so env.j2 Jinja2 never raises UndefinedError."
            )
            assert data[k] == "", (
                f"flat-var {k} should default to '' but got {data[k]!r}"
            )

    def test_none_value_is_coerced_to_empty_string(self):
        """Operator-supplied `None` (e.g. from a YAML `~` in state.yaml) MUST
        NOT propagate. env.j2 has no `default('')` filter on these LB IP slots,
        so a None would render as the literal string `None` in `REDIS_URL`
        etc., causing Kamailio DNS resolution failure and CrashLoop. The
        `or ""` coercion in `_write_extra_vars` collapses both missing- and
        None-valued cases to empty string before they reach Ansible."""
        outputs = {
            "kamailio_internal_ips": [],
            "rtpengine_external_ips": [],
            "redis_lb_ip": None,
            "rabbitmq_lb_ip": None,
            "asterisk_call_lb_ip": None,
            "asterisk_registrar_lb_ip": None,
            "asterisk_conference_lb_ip": None,
        }
        data = _write_and_load(_base_config(), outputs)
        for k in _FLAT_KEYS:
            assert data[k] == "", (
                f"flat-var {k} = {data[k]!r} after None input. "
                f"Expected '' (Jinja2-safe). Missing `or \"\"` coercion?"
            )


class TestNoRegressionOnExistingFlatVars:
    """PR-T must NOT disturb PR-S flat-vars (kamailio_internal_lb_ip,
    rtpengine_socks) or PR-D2c (kamailio_auth_db_url) wiring."""

    def test_kamailio_internal_lb_ip_still_flat(self):
        outputs = {
            "kamailio_internal_ips": [],
            "rtpengine_external_ips": [],
            "kamailio_internal_lb_ip": "10.0.0.2",
            "redis_lb_ip": "10.164.0.10",
        }
        data = _write_and_load(_base_config(), outputs)
        assert data["kamailio_internal_lb_ip"] == "10.0.0.2"
        assert data["redis_lb_ip"] == "10.164.0.10"

    def test_rtpengine_socks_still_built(self):
        outputs = {
            "kamailio_internal_ips": [],
            "rtpengine_external_ips": ["34.44.164.191"],
            "redis_lb_ip": "10.164.0.10",
        }
        data = _write_and_load(_base_config(), outputs)
        assert data["rtpengine_socks"] == "udp:34.44.164.191:22222"
        assert data["redis_lb_ip"] == "10.164.0.10"

    def test_terraform_outputs_dict_preserved_under_nested_key(self):
        outputs = {
            "kamailio_internal_ips": [],
            "rtpengine_external_ips": [],
            "redis_lb_ip": "10.164.0.10",
            "some_other_terraform_output": "value",
        }
        data = _write_and_load(_base_config(), outputs)
        # Nested terraform_outputs preserved verbatim (used by some roles).
        assert data["terraform_outputs"]["redis_lb_ip"] == "10.164.0.10"
        assert (
            data["terraform_outputs"]["some_other_terraform_output"] == "value"
        )
