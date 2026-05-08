# tests/test_dns.py
"""Tests for DNS guide shown after apply success."""

import sys
from io import StringIO
from pathlib import Path
from typing import Any, Callable

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from rich.console import Console
import scripts.display as display_mod

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


class TestApplyShowsDnsSection1:
    """Section 1 of the DNS guide must appear in the apply success output."""

    def test_dns_section1_shown_after_apply_success(self):
        from unittest.mock import MagicMock, patch
        from scripts.commands.apply import cmd_apply

        mock_config = MagicMock()
        mock_config.exists.return_value = True
        mock_config.validate.return_value = []
        mock_config.get.side_effect = lambda key, default="": {
            "gcp_project_id": "my-project",
            "region": "us-central1",
            "domain": "example.com",
        }.get(key, default)

        with patch("scripts.commands.apply.InstallerConfig", return_value=mock_config), \
             patch("scripts.commands.apply.load_state", return_value={}), \
             patch("scripts.commands.apply.run_pre_apply_checks", return_value=True), \
             patch("scripts.commands.apply.run_pipeline", return_value=True):
            out = _capture(cmd_apply, auto_approve=True)

        assert "DNS Records" in out
        for sub in ("api", "admin", "talk", "meet", "sip"):
            assert sub in out
        assert "voipbin-install verify" in out
