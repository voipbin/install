"""Tests for scripts/display.py — display helpers and formatting."""

from io import StringIO
from unittest.mock import patch

from rich.console import Console

from scripts.display import (
    _LOGO,
    create_progress,
    print_banner,
    print_check,
    print_cost_table,
    print_error,
    print_header,
    print_result_box,
    print_step,
    print_success,
    print_warning,
)
from scripts.display import print_fix


def _capture_output(fn, *args, **kwargs) -> str:
    """Capture Rich console output by temporarily replacing the console."""
    import scripts.display as mod
    original = mod.console
    buf = StringIO()
    mod.console = Console(file=buf, no_color=True, width=120)
    try:
        fn(*args, **kwargs)
        return buf.getvalue()
    finally:
        mod.console = original


class TestPrintFunctions:
    def test_print_success_contains_message(self):
        output = _capture_output(print_success, "All good")
        assert "All good" in output

    def test_print_error_contains_message(self):
        output = _capture_output(print_error, "Something broke")
        assert "Something broke" in output

    def test_print_warning_contains_message(self):
        output = _capture_output(print_warning, "Watch out")
        assert "Watch out" in output

    def test_print_step_contains_message(self):
        output = _capture_output(print_step, "Doing a thing")
        assert "Doing a thing" in output

    def test_print_header_contains_message(self):
        output = _capture_output(print_header, "Section Title")
        assert "Section Title" in output


class TestPrintCheck:
    def test_ok_check(self):
        output = _capture_output(print_check, "gcloud", "456.0.0", True, "400.0.0")
        assert "gcloud" in output
        assert "456.0.0" in output

    def test_failed_check(self):
        output = _capture_output(print_check, "terraform", "", False, "1.5.0")
        assert "terraform" in output
        assert "not found" in output

    def test_no_required_version(self):
        output = _capture_output(print_check, "python3", "3.12.3", True)
        assert "python3" in output
        assert "3.12.3" in output


class TestPrintResultBox:
    def test_renders_lines(self):
        output = _capture_output(print_result_box, ["Line 1", "Line 2"])
        assert "Line 1" in output
        assert "Line 2" in output


class TestPrintBanner:
    def test_banner_renders(self):
        output = _capture_output(print_banner, force=True)
        assert "VoIPBin" in output or "INSTALLER" in output or "I N S T A L L E R" in output

    def test_logo_constant_contains_installer_text(self):
        assert "I N S T A L L E R" in _LOGO


class TestCreateProgress:
    def test_returns_progress_instance(self):
        from rich.progress import Progress
        p = create_progress()
        assert isinstance(p, Progress)


class TestPrintCostTable:
    def test_zonal_renders(self):
        output = _capture_output(print_cost_table, "zonal")
        assert "GKE" in output
        assert "$0" in output or "free" in output.lower()

    def test_regional_renders(self):
        output = _capture_output(print_cost_table, "regional")
        assert "GKE" in output
        assert "$73" in output


class TestPrintFix:
    @patch("scripts.display.console")
    def test_string_input(self, mock_console):
        print_fix("How to fix", "gcloud auth application-default login")
        mock_console.print.assert_called_once()
        panel = mock_console.print.call_args.args[0]
        assert "How to fix" in str(panel.renderable)

    @patch("scripts.display.console")
    def test_list_input(self, mock_console):
        print_fix("Likely causes", ["Billing disabled", "ADC expired"])
        mock_console.print.assert_called_once()
        panel = mock_console.print.call_args.args[0]
        assert "Likely causes" in str(panel.renderable)
        assert "  Billing disabled" in str(panel.renderable)
        assert "  ADC expired" in str(panel.renderable)

    @patch("scripts.display.console")
    def test_single_item_list(self, mock_console):
        print_fix("Fix", ["run: gcloud auth login"])
        mock_console.print.assert_called_once()
        panel = mock_console.print.call_args.args[0]
        assert "Fix" in str(panel.renderable)
        assert "  run: gcloud auth login" in str(panel.renderable)
