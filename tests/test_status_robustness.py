"""Tests for voipbin-install status command robustness.

Covers graceful degradation when Terraform state backend is unavailable
(e.g. immediately after destroy when the GCS bucket is gone).
"""

from __future__ import annotations

from unittest.mock import patch

import pytest


class TestPrintTerraformStatusRobustness:
    """Ensure _print_terraform_status never raises — only warns."""

    def test_exception_from_terraform_resource_count_is_caught(self):
        """If terraform_resource_count raises any Exception, status prints a
        warning instead of propagating the traceback to the user."""
        from scripts.commands.status import _print_terraform_status

        dummy_config = {
            "gcp_project_id": "test-project",
            "region": "us-central1",
            "zone": "us-central1-a",
            "env": "test",
        }

        with patch(
            "scripts.commands.status.terraform_resource_count",
            side_effect=Exception("GCS backend unavailable"),
        ):
            # Must not raise — should silently degrade to a warning
            try:
                _print_terraform_status(dummy_config)
            except Exception as exc:
                pytest.fail(
                    f"_print_terraform_status raised {type(exc).__name__}: {exc} "
                    "instead of handling gracefully."
                )

    def test_negative_return_value_shows_warning(self):
        """returncode != 0 path: terraform_resource_count returns -1."""
        from scripts.commands.status import _print_terraform_status

        dummy_config = {}
        with patch(
            "scripts.commands.status.terraform_resource_count",
            return_value=-1,
        ):
            _print_terraform_status(dummy_config)  # must not raise

    def test_zero_resources_shows_no_resources(self):
        """Empty state: terraform_resource_count returns 0."""
        from scripts.commands.status import _print_terraform_status

        dummy_config = {}
        with patch(
            "scripts.commands.status.terraform_resource_count",
            return_value=0,
        ):
            _print_terraform_status(dummy_config)  # must not raise

    def test_positive_resources_shows_count(self):
        """Normal path: terraform_resource_count returns a positive int."""
        from scripts.commands.status import _print_terraform_status

        dummy_config = {}
        with patch(
            "scripts.commands.status.terraform_resource_count",
            return_value=42,
        ):
            _print_terraform_status(dummy_config)  # must not raise
