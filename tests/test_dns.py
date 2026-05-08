# tests/test_dns.py
"""Tests for scripts/commands/dns.py"""

import sys
from io import StringIO
from pathlib import Path
from typing import Any, Callable

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from rich.console import Console
import scripts.display as display_mod
from scripts.commands.dns import print_dns_section1
from scripts.commands.dns import print_dns_section2
from scripts.commands.dns import print_dns_section3
from scripts.commands.dns import cmd_dns

_ROOT = Path(__file__).resolve().parent.parent


def _capture(fn: "Callable[..., Any]", *args: Any, **kwargs: Any) -> str:
    """Capture Rich output by replacing the shared display console."""
    original = display_mod.console
    buf = StringIO()
    display_mod.console = Console(file=buf, no_color=True, width=120)
    try:
        fn(*args, **kwargs)
        return buf.getvalue()
    finally:
        display_mod.console = original


class TestSection1:
    def test_contains_all_five_subdomains(self):
        out = _capture(print_dns_section1)
        for sub in ("api", "admin", "talk", "meet", "sip"):
            assert sub in out

    def test_contains_example_domain(self):
        out = _capture(print_dns_section1)
        assert "example.com" in out

    def test_contains_example_ip(self):
        out = _capture(print_dns_section1)
        assert "1.2.3.4" in out

    def test_mentions_auto_dns_mode(self):
        out = _capture(print_dns_section1)
        assert "auto" in out.lower()

    def test_mentions_verify_command(self):
        out = _capture(print_dns_section1)
        assert "voipbin-install verify" in out


class TestSection2:
    def test_contains_domain_key(self):
        out = _capture(print_dns_section2)
        assert "DOMAIN    example.com" in out

    def test_contains_domain_name_trunk(self):
        out = _capture(print_dns_section2)
        assert "DOMAIN_NAME_TRUNK" in out

    def test_contains_domain_name_extension(self):
        out = _capture(print_dns_section2)
        assert "DOMAIN_NAME_EXTENSION" in out

    def test_contains_namespace(self):
        out = _capture(print_dns_section2)
        assert "bin-manager" in out

    def test_mentions_audit(self):
        out = _capture(print_dns_section2)
        assert "audit" in out.lower()


class TestSection3:
    def test_contains_base_domain(self):
        out = _capture(print_dns_section3)
        assert "BASE_DOMAIN" in out

    def test_contains_domain_name_extension(self):
        out = _capture(print_dns_section3)
        assert "DOMAIN_NAME_EXTENSION" in out

    def test_contains_domain_name_trunk(self):
        out = _capture(print_dns_section3)
        assert "DOMAIN_NAME_TRUNK" in out

    def test_contains_env_path(self):
        out = _capture(print_dns_section3)
        assert "/opt/kamailio-docker/.env" in out

    def test_mentions_rtpengine(self):
        out = _capture(print_dns_section3)
        assert "RTPEngine" in out


class TestCmdDns:
    def test_calls_all_three_sections(self):
        out = _capture(cmd_dns)
        assert "DNS Records" in out
        assert "Kubernetes" in out
        assert "Kamailio VM" in out

    def test_sections_in_order(self):
        out = _capture(cmd_dns)
        assert out.index("DNS Records") < out.index("Kubernetes") < out.index("Kamailio VM")
