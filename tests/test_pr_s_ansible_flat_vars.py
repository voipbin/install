"""Tests for PR-S: ansible_runner flat-var sweep for kamailio_internal_lb_ip and
rtpengine_socks.

Design doc. docs/plans/2026-05-13-pr-s-ansible-flat-vars-sweep-design.md
"""

import json
import os
from unittest.mock import MagicMock

from scripts.ansible_runner import _build_rtpengine_socks, _write_extra_vars


def _make_config(data: dict) -> MagicMock:
    cfg = MagicMock()
    cfg.to_ansible_vars.return_value = dict(data)
    cfg.get.side_effect = lambda key, default="": data.get(key, default)
    return cfg


class TestBuildRtpengineSocks:
    """Helper produces the space-separated `udp:<ip>:22222` string consumed
    by env.j2 RTPENGINE_SOCKS."""

    def test_single_ip(self):
        assert _build_rtpengine_socks(
            {"rtpengine_external_ips": ["1.2.3.4"]}
        ) == "udp:1.2.3.4:22222"

    def test_multi_ip_space_joined_in_input_order(self):
        assert _build_rtpengine_socks(
            {"rtpengine_external_ips": ["1.2.3.4", "5.6.7.8"]}
        ) == "udp:1.2.3.4:22222 udp:5.6.7.8:22222"

    def test_empty_list_returns_empty_string(self):
        assert _build_rtpengine_socks({"rtpengine_external_ips": []}) == ""

    def test_non_list_or_none_returns_empty_string(self):
        # None
        assert _build_rtpengine_socks({"rtpengine_external_ips": None}) == ""
        # missing key
        assert _build_rtpengine_socks({}) == ""
        # non-list scalar (defensive)
        assert _build_rtpengine_socks(
            {"rtpengine_external_ips": "1.2.3.4"}
        ) == ""

    def test_strips_empty_and_whitespace_elements(self):
        """Locks the `ip.strip()` element filter — addresses design §9 risk #1."""
        assert _build_rtpengine_socks(
            {"rtpengine_external_ips": ["1.2.3.4", "", "  ", "5.6.7.8"]}
        ) == "udp:1.2.3.4:22222 udp:5.6.7.8:22222"

    def test_filters_non_string_elements(self):
        """Locks the `isinstance(ip, str)` element filter."""
        assert _build_rtpengine_socks(
            {"rtpengine_external_ips": [1, "1.2.3.4", None]}
        ) == "udp:1.2.3.4:22222"


class TestWriteExtraVarsIncludesKamailioInternalLbIp:

    def test_internal_lb_ip_lands_at_top_level(self):
        cfg = _make_config(
            {
                "gcp_project_id": "test-proj",
                "region": "us-central1",
                "zone": "us-central1-a",
            }
        )
        outputs = {
            "kamailio_internal_ips": [],
            "rtpengine_external_ips": [],
            "kamailio_external_lb_ip": "",
            "kamailio_internal_lb_ip": "10.0.0.2",
        }
        path = _write_extra_vars(cfg, outputs)
        try:
            data = json.loads(path.read_text())
            assert data["kamailio_internal_lb_ip"] == "10.0.0.2"
        finally:
            if path.exists():
                os.unlink(path)


class TestWriteExtraVarsIncludesRtpengineSocks:

    def test_rtpengine_socks_derived_from_external_ips(self):
        cfg = _make_config(
            {
                "gcp_project_id": "test-proj",
                "region": "us-central1",
                "zone": "us-central1-a",
            }
        )
        outputs = {
            "kamailio_internal_ips": [],
            "rtpengine_external_ips": ["34.44.164.191"],
            "kamailio_external_lb_ip": "",
        }
        path = _write_extra_vars(cfg, outputs)
        try:
            data = json.loads(path.read_text())
            assert data["rtpengine_socks"] == "udp:34.44.164.191:22222"
        finally:
            if path.exists():
                os.unlink(path)
