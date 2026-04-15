"""Tests for scripts/utils.py"""

import sys
from pathlib import Path

# Ensure project root is on path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.utils import generate_key, generate_password, parse_semver, version_gte


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
