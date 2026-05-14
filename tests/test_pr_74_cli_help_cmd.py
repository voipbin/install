"""Tests for PR #74: CLI UX fixes — prog_name and 'help' command."""

import sys
from click.testing import CliRunner

sys.path.insert(0, ".")
from scripts.cli import cli


def _run(*args):
    runner = CliRunner()
    return runner.invoke(cli, list(args), prog_name="voipbin-install", catch_exceptions=False)


class TestProgName:
    """Usage and error messages must show 'voipbin-install', not 'cli.py'."""

    def test_help_usage_shows_prog_name(self):
        result = _run("--help")
        assert result.exit_code == 0
        assert "Usage: voipbin-install" in result.output

    def test_unknown_command_error_shows_prog_name(self):
        runner = CliRunner()
        result = runner.invoke(cli, ["badcmd"], prog_name="voipbin-install")
        assert result.exit_code != 0
        assert "voipbin-install" in result.output


class TestHelpCommand:
    """'help' alias command behaviour."""

    def test_help_no_arg_shows_top_level_help(self):
        result = _run("help")
        assert result.exit_code == 0
        assert "Usage: voipbin-install" in result.output
        assert "init" in result.output
        assert "apply" in result.output

    def test_help_with_valid_subcommand(self):
        result = _run("help", "init")
        assert result.exit_code == 0
        assert "Usage: voipbin-install init" in result.output

    def test_help_with_unknown_subcommand_exits_nonzero(self):
        runner = CliRunner()
        result = runner.invoke(cli, ["help", "badcmd"], prog_name="voipbin-install")
        assert result.exit_code != 0
        assert "No such command" in result.output

    def test_help_is_hidden_from_command_list(self):
        result = _run("--help")
        # 'help' should not appear in the Commands listing
        lines = [l.strip() for l in result.output.splitlines() if l.strip().startswith("help")]
        assert lines == [], f"'help' should be hidden but appeared: {lines}"
