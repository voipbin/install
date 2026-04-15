"""Tests for scripts/display.py — display helpers and formatting."""

from io import StringIO

from rich.console import Console

from scripts.display import (
    BANNER_TEXT,
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
        output = _capture_output(print_banner)
        assert "VoIPBin" in output or "Installer" in output

    def test_banner_text_constant(self):
        assert "VoIPBin" in BANNER_TEXT


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
