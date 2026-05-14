"""Tests for PR #75: ASCII banner in display.py."""

import sys

sys.path.insert(0, ".")
from scripts.display import print_banner, _LOGO, _DIVIDER
from rich.console import Console
from io import StringIO


def _capture_banner(force=True):
    """Capture print_banner output as a string."""
    buf = StringIO()
    cap = Console(file=buf, highlight=False, markup=False)
    # Monkeypatch the module-level console temporarily
    import scripts.display as display_mod
    orig = display_mod.console
    display_mod.console = cap
    try:
        print_banner(force=force)
    finally:
        display_mod.console = orig
    return buf.getvalue()


class TestPrintBanner:
    def test_banner_contains_connect_text(self):
        out = _capture_banner()
        assert "Connect & Collaborate" in out

    def test_banner_contains_installer_text(self):
        out = _capture_banner()
        assert "I N S T A L L E R" in out

    def test_banner_contains_divider(self):
        out = _capture_banner()
        assert "━" in out

    def test_banner_skipped_when_not_tty(self):
        """Without force=True and not a TTY, output should be empty."""
        import os
        # stdout fd 1 is not a TTY in pytest (piped)
        assert not os.isatty(1), "This test assumes non-TTY environment"
        out = _capture_banner(force=False)
        assert out == ""

    def test_banner_force_overrides_tty_check(self):
        out = _capture_banner(force=True)
        assert "I N S T A L L E R" in out

    def test_logo_constant_is_non_empty(self):
        assert len(_LOGO.strip()) > 0

    def test_divider_length(self):
        assert len(_DIVIDER) == 75
