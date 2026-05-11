"""Tests for scripts/config.py"""

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import yaml

from scripts.config import InstallerConfig


def _make_config(tmp_path: Path) -> InstallerConfig:
    cfg = InstallerConfig(config_dir=tmp_path)
    cfg.set_many({
        "gcp_project_id": "test-project-123",
        "region": "us-central1",
        "zone": "us-central1-a",
        "gke_type": "zonal",
        "tls_strategy": "self-signed",
        "image_tag_strategy": "pinned",
        "domain": "voipbin.example.com",
        "dns_mode": "auto",
    })
    cfg.apply_defaults()
    return cfg


class TestInstallerConfig:
    def test_roundtrip(self, tmp_path):
        cfg = _make_config(tmp_path)
        cfg.save()

        cfg2 = InstallerConfig(config_dir=tmp_path)
        cfg2.load()
        assert cfg2.get("gcp_project_id") == "test-project-123"
        assert cfg2.get("region") == "us-central1"
        assert cfg2.get("domain") == "voipbin.example.com"

    def test_validate_valid(self, tmp_path):
        cfg = _make_config(tmp_path)
        errors = cfg.validate()
        assert errors == []

    def test_validate_missing_project(self, tmp_path):
        cfg = InstallerConfig(config_dir=tmp_path)
        cfg.set_many({"region": "us-central1", "domain": "test.example.com"})
        errors = cfg.validate()
        assert len(errors) > 0

    def test_validate_invalid_gke_type(self, tmp_path):
        cfg = _make_config(tmp_path)
        cfg.set("gke_type", "invalid")
        errors = cfg.validate()
        assert len(errors) > 0

    def test_validate_accepts_self_signed_tls(self, tmp_path):
        cfg = _make_config(tmp_path)
        cfg.set("tls_strategy", "self-signed")
        assert cfg.validate() == []

    def test_validate_accepts_byoc_tls(self, tmp_path):
        cfg = _make_config(tmp_path)
        cfg.set("tls_strategy", "byoc")
        assert cfg.validate() == []

    def test_validate_rejects_letsencrypt_tls(self, tmp_path):
        cfg = _make_config(tmp_path)
        cfg.set("tls_strategy", "letsencrypt")
        assert len(cfg.validate()) > 0

    def test_validate_rejects_gcp_managed_tls(self, tmp_path):
        cfg = _make_config(tmp_path)
        cfg.set("tls_strategy", "gcp-managed")
        assert len(cfg.validate()) > 0

    def test_default_tls_strategy_is_self_signed(self, tmp_path):
        cfg = InstallerConfig(config_dir=tmp_path)
        cfg.set_many({
            "gcp_project_id": "test-123456",
            "region": "us-central1",
            "domain": "t.example.com",
        })
        cfg.apply_defaults()
        assert cfg.get("tls_strategy") == "self-signed"

    def test_env_override(self, tmp_path):
        cfg = _make_config(tmp_path)
        os.environ["VOIPBIN_REGION"] = "europe-west1"
        try:
            assert cfg.get("region") == "europe-west1"
        finally:
            del os.environ["VOIPBIN_REGION"]

    def test_exists(self, tmp_path):
        cfg = InstallerConfig(config_dir=tmp_path)
        assert not cfg.exists()
        cfg.set_many({"gcp_project_id": "test-123456", "region": "us-central1", "domain": "t.example.com"})
        cfg.save()
        assert cfg.exists()

    def test_to_terraform_vars(self, tmp_path):
        cfg = _make_config(tmp_path)
        tf_vars = cfg.to_terraform_vars()
        assert tf_vars["projectid"] == "test-project-123"
        assert tf_vars["region"] == "us-central1"
        assert tf_vars["domain"] == "voipbin.example.com"

    def test_to_ansible_vars(self, tmp_path):
        cfg = _make_config(tmp_path)
        ans_vars = cfg.to_ansible_vars()
        assert ans_vars["gcp_project_id"] == "test-project-123"
        assert ans_vars["domain"] == "voipbin.example.com"
