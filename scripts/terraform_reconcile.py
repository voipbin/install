"""Terraform state reconciliation for VoIPBin installer.

Detects GCP resources that exist outside Terraform state and imports them
before terraform apply runs, making deployments resumable without 409 errors.
"""

from scripts.utils import run_cmd


# Covers "NOT FOUND", "NOT_FOUND" (gcloud style), "notfound", "404 Not Found", etc.
_NOT_FOUND_PHRASES = ("not found", "notfound", "not_found", "does not exist", "404", "no such")


def check_exists_in_gcp(check_cmd: list[str]) -> tuple[bool, bool]:
    """Check whether a GCP resource exists.

    Returns:
        (exists, check_succeeded): exists=True if the resource is present.
        check_succeeded=False means the check could not be completed (e.g.
        permission error) — callers should warn but not block.
    """
    result = run_cmd(check_cmd, capture=True, timeout=30)
    if result.returncode == 0:
        return True, True
    stderr_lower = (result.stderr or "").lower()
    if any(phrase in stderr_lower for phrase in _NOT_FOUND_PHRASES):
        return False, True
    return False, False
