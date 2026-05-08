# tests/test_dns.py
"""Tests for scripts/commands/dns.py"""

import sys
from io import StringIO
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from rich.console import Console
import scripts.display as display_mod
from scripts.commands.dns import print_dns_section1

_ROOT = Path(__file__).resolve().parent.parent


def _capture(fn, *args, **kwargs) -> str:
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
