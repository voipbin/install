"""Shared utilities for VoIPBin installer."""

import base64
import os
import re
import secrets
import shlex
import shutil
import string
import subprocess
import time
from pathlib import Path
from typing import Optional, Union


INSTALLER_DIR = Path(__file__).resolve().parent.parent

# Allowed characters for GCP identifiers used in commands
_SAFE_ID_RE = re.compile(r"^[a-zA-Z0-9._:/@-]+$")


def _validate_cmd_arg(value: str, name: str) -> None:
    """Validate that a value is safe to include in a command argument.

    Raises ValueError if the value contains shell metacharacters or
    other potentially dangerous characters.
    """
    if not value:
        return
    if not _SAFE_ID_RE.match(value):
        raise ValueError(
            f"Unsafe characters in {name}: {value!r}. "
            "Only alphanumerics, dots, dashes, underscores, colons, slashes, and @ are allowed."
        )


def run_cmd(
    cmd: Union[str, list[str]],
    capture: bool = True,
    check: bool = False,
    timeout: int = 300,
) -> subprocess.CompletedProcess:
    """Run a command and return the CompletedProcess result.

    Accepts either a command string (split via shlex) or a list of
    arguments. Never uses shell=True to avoid command injection.
    """
    if isinstance(cmd, str):
        args = shlex.split(cmd)
    else:
        args = cmd
    return subprocess.run(
        args,
        capture_output=capture,
        text=True,
        check=check,
        timeout=timeout,
    )


def run_cmd_with_retry(
    cmd: Union[str, list[str]],
    retries: int = 3,
    delay: float = 5.0,
    backoff: float = 2.0,
    timeout: int = 300,
) -> subprocess.CompletedProcess:
    """Run a command with exponential-backoff retry."""
    last_result = None
    current_delay = delay
    for attempt in range(retries):
        result = run_cmd(cmd, capture=True, check=False, timeout=timeout)
        if result.returncode == 0:
            return result
        last_result = result
        if attempt < retries - 1:
            time.sleep(current_delay)
            current_delay *= backoff
    return last_result  # type: ignore[return-value]


def check_tool_exists(tool: str) -> bool:
    """Return True if *tool* is on PATH. Uses shutil.which (no shell)."""
    return shutil.which(tool) is not None


def parse_semver(version_string: str) -> tuple[int, int, int]:
    """Extract (major, minor, patch) from a version string.

    Handles formats like "v1.9.2", "Terraform v1.9.2", "gcloud 485.0.0", etc.
    """
    match = re.search(r"(\d+)\.(\d+)\.(\d+)", version_string)
    if not match:
        raise ValueError(f"Cannot parse version from: {version_string!r}")
    return (int(match.group(1)), int(match.group(2)), int(match.group(3)))


def version_gte(actual: str, minimum: str) -> bool:
    """Return True if *actual* version >= *minimum* version."""
    return parse_semver(actual) >= parse_semver(minimum)


def generate_password(length: int = 24) -> str:
    """Generate a cryptographically secure random password."""
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(length))


def generate_key(length: int = 32) -> str:
    """Generate a random key, returned as URL-safe base64."""
    return base64.urlsafe_b64encode(secrets.token_bytes(length)).decode()


def ensure_dir(path: Path) -> Path:
    """Create directory (and parents) if it doesn't exist. Returns the path."""
    path.mkdir(parents=True, exist_ok=True)
    return path
