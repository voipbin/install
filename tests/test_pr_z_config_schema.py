"""PR-Z Phase B tests: config schema cert_mode + cert_manual_dir."""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.config import InstallerConfig


def _mk_config(**overrides) -> InstallerConfig:
    cfg = InstallerConfig()
    cfg._data = {
        "gcp_project_id": "voipbin-test-1",
        "region": "us-central1",
        "domain": "dev.voipbin.example.com",
    }
    cfg._data.update(overrides)
    return cfg


class TestConfigSchemaCertMode:
    def test_accepts_self_signed(self):
        cfg = _mk_config(cert_mode="self_signed", cert_manual_dir=None)
        errors = cfg.validate()
        assert errors == [], errors

    def test_accepts_manual_with_dir(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = _mk_config(cert_mode="manual", cert_manual_dir=td)
            errors = cfg.validate()
            assert errors == [], errors

    def test_rejects_acme_cert_mode(self):
        cfg = _mk_config(cert_mode="acme")
        errors = cfg.validate()
        assert errors, "'acme' is not a valid cert_mode; expected validation errors"
        joined = " ".join(errors)
        assert "not supported" in joined

    def test_rejects_unknown_mode(self):
        cfg = _mk_config(cert_mode="bogus", cert_manual_dir=None)
        errors = cfg.validate()
        assert errors, "expected validation errors for bogus cert_mode"

    def test_manual_requires_cert_manual_dir(self):
        # manual mode with no cert_manual_dir / null → must fail
        cfg = _mk_config(cert_mode="manual", cert_manual_dir=None)
        errors = cfg.validate()
        assert errors, "manual mode without cert_manual_dir must error"

    def test_self_signed_forbids_cert_manual_dir(self):
        cfg = _mk_config(
            cert_mode="self_signed", cert_manual_dir="/tmp/something",
        )
        errors = cfg.validate()
        assert errors, "self_signed with cert_manual_dir set must error"
