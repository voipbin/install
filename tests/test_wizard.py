"""Tests for scripts/wizard.py"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.wizard import _validate_domain, _validate_project_id, derive_zone


class TestValidateDomain:
    def test_valid(self):
        assert _validate_domain("voipbin.example.com") is None

    def test_valid_subdomain(self):
        assert _validate_domain("voip.prod.example.com") is None

    def test_empty(self):
        assert _validate_domain("") is not None

    def test_with_http(self):
        assert _validate_domain("https://example.com") is not None

    def test_no_dot(self):
        assert _validate_domain("localhost") is not None

    def test_uppercase(self):
        assert _validate_domain("VOIPBIN.COM") is not None

    def test_valid_short(self):
        assert _validate_domain("v.io") is None


class TestValidateProjectId:
    def test_valid(self):
        assert _validate_project_id("my-gcp-project") is None

    def test_too_short(self):
        assert _validate_project_id("abc") is not None

    def test_empty(self):
        assert _validate_project_id("") is not None

    def test_uppercase(self):
        assert _validate_project_id("My-Project") is not None


class TestDeriveZone:
    def test_zonal(self):
        assert derive_zone("us-central1", "zonal") == "us-central1-a"

    def test_regional(self):
        assert derive_zone("us-central1", "regional") == "us-central1"

    def test_europe(self):
        assert derive_zone("europe-west4", "zonal") == "europe-west4-a"
