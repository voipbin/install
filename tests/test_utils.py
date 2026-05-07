"""Tests for scripts/utils.py"""

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Ensure project root is on path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.utils import (
    _validate_cmd_arg,
    check_tool_exists,
    generate_key,
    generate_password,
    parse_semver,
    run_cmd,
    run_cmd_with_retry,
    version_gte,
)


class TestParseSemver:
    def test_simple(self):
        assert parse_semver("1.9.2") == (1, 9, 2)

    def test_v_prefix(self):
        assert parse_semver("v1.9.2") == (1, 9, 2)

    def test_terraform_output(self):
        assert parse_semver("Terraform v1.9.2\non linux_amd64") == (1, 9, 2)

    def test_gcloud_output(self):
        assert parse_semver("Google Cloud SDK 485.0.0") == (485, 0, 0)

    def test_ansible_output(self):
        assert parse_semver("ansible [core 2.17.1]") == (2, 17, 1)

    def test_invalid_raises(self):
        import pytest
        with pytest.raises(ValueError):
            parse_semver("no version here")


class TestVersionGte:
    def test_equal(self):
        assert version_gte("1.5.0", "1.5.0")

    def test_greater_major(self):
        assert version_gte("2.0.0", "1.5.0")

    def test_greater_minor(self):
        assert version_gte("1.6.0", "1.5.0")

    def test_greater_patch(self):
        assert version_gte("1.5.1", "1.5.0")

    def test_less_than(self):
        assert not version_gte("1.4.9", "1.5.0")

    def test_with_prefix(self):
        assert version_gte("v1.9.2", "1.5.0")


class TestGeneratePassword:
    def test_length(self):
        pw = generate_password(24)
        assert len(pw) == 24

    def test_alphanumeric(self):
        pw = generate_password(100)
        assert pw.isalnum()

    def test_unique(self):
        pw1 = generate_password(24)
        pw2 = generate_password(24)
        assert pw1 != pw2


class TestGenerateKey:
    def test_not_empty(self):
        key = generate_key(32)
        assert len(key) > 0

    def test_base64(self):
        import base64
        key = generate_key(32)
        # Should decode without error
        base64.urlsafe_b64decode(key)

    def test_unique(self):
        k1 = generate_key(32)
        k2 = generate_key(32)
        assert k1 != k2


class TestCheckToolExists:
    def test_finds_python3(self):
        assert check_tool_exists("python3") is True

    def test_missing_tool(self):
        assert check_tool_exists("this_tool_does_not_exist_xyzzy") is False

    @patch("scripts.utils.shutil.which")
    def test_uses_shutil_which(self, mock_which):
        mock_which.return_value = "/usr/bin/mytool"
        assert check_tool_exists("mytool") is True
        mock_which.assert_called_once_with("mytool")

    @patch("scripts.utils.shutil.which")
    def test_returns_false_when_which_none(self, mock_which):
        mock_which.return_value = None
        assert check_tool_exists("nope") is False


class TestValidateCmdArg:
    def test_valid_project_id(self):
        _validate_cmd_arg("my-project-123", "project_id")  # no exception

    def test_valid_region(self):
        _validate_cmd_arg("us-central1", "region")  # no exception

    def test_valid_email_format(self):
        _validate_cmd_arg("sa@project.iam.gserviceaccount.com", "email")

    def test_valid_kms_path(self):
        _validate_cmd_arg(
            "projects/my-proj/locations/global/keyRings/ring/cryptoKeys/key",
            "kms_key",
        )

    def test_empty_string_allowed(self):
        _validate_cmd_arg("", "anything")  # no exception

    def test_rejects_semicolon(self):
        with pytest.raises(ValueError, match="Unsafe characters"):
            _validate_cmd_arg("proj; rm -rf /", "project_id")

    def test_rejects_backtick(self):
        with pytest.raises(ValueError, match="Unsafe characters"):
            _validate_cmd_arg("`whoami`", "project_id")

    def test_rejects_dollar(self):
        with pytest.raises(ValueError, match="Unsafe characters"):
            _validate_cmd_arg("$(cat /etc/passwd)", "project_id")

    def test_rejects_pipe(self):
        with pytest.raises(ValueError, match="Unsafe characters"):
            _validate_cmd_arg("proj | cat", "project_id")

    def test_rejects_ampersand(self):
        with pytest.raises(ValueError, match="Unsafe characters"):
            _validate_cmd_arg("proj && echo pwned", "project_id")


class TestRunCmd:
    def test_no_shell_true(self):
        """run_cmd must not use shell=True."""
        result = run_cmd(["echo", "hello"])
        assert result.returncode == 0
        assert "hello" in result.stdout

    def test_accepts_string(self):
        result = run_cmd("echo hello")
        assert result.returncode == 0
        assert "hello" in result.stdout

    def test_accepts_list(self):
        result = run_cmd(["echo", "world"])
        assert result.returncode == 0
        assert "world" in result.stdout

    def test_timeout_returns_124_instead_of_raising(self):
        """run_cmd should swallow TimeoutExpired and return rc=124."""
        import subprocess
        with patch(
            "scripts.utils.subprocess.run",
            side_effect=subprocess.TimeoutExpired(
                cmd=["sleep", "5"], timeout=1, output="", stderr=""
            ),
        ):
            result = run_cmd(["sleep", "5"], timeout=1)
        assert result.returncode == 124
        assert "timed out after 1s" in result.stderr


class TestRunCmdWithRetry:
    @patch("scripts.utils.run_cmd")
    def test_does_not_retry_on_timeout(self, mock_run_cmd):
        """rc=124 (timeout) should bypass the retry loop."""
        mock_run_cmd.return_value = MagicMock(returncode=124, stderr="timed out")
        result = run_cmd_with_retry(["foo"], retries=3, delay=0)
        assert result.returncode == 124
        assert mock_run_cmd.call_count == 1

    @patch("scripts.utils.time.sleep")
    @patch("scripts.utils.run_cmd")
    def test_retries_other_failures(self, mock_run_cmd, mock_sleep):
        mock_run_cmd.side_effect = [
            MagicMock(returncode=1),
            MagicMock(returncode=1),
            MagicMock(returncode=0),
        ]
        result = run_cmd_with_retry(["foo"], retries=3, delay=0)
        assert result.returncode == 0
        assert mock_run_cmd.call_count == 3
