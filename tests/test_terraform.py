"""Tests for scripts/terraform.py — tfvars generation from config."""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.config import InstallerConfig
from scripts.terraform import TFVARS_FILE, write_tfvars


def _make_config(tmp_path: Path) -> InstallerConfig:
    cfg = InstallerConfig(config_dir=tmp_path)
    cfg.set_many({
        "gcp_project_id": "test-project-123",
        "region": "us-central1",
        "zone": "us-central1-a",
        "gke_type": "zonal",
        "tls_strategy": "letsencrypt",
        "image_tag_strategy": "pinned",
        "domain": "voipbin.example.com",
        "dns_mode": "auto",
    })
    cfg.apply_defaults()
    return cfg


class TestWriteTfvars:
    def test_creates_file(self, tmp_path, monkeypatch):
        tfvars_path = tmp_path / "terraform.tfvars.json"
        monkeypatch.setattr("scripts.terraform.TFVARS_FILE", tfvars_path)

        cfg = _make_config(tmp_path)
        result_path = write_tfvars(cfg)

        assert result_path == tfvars_path
        assert tfvars_path.exists()

    def test_valid_json(self, tmp_path, monkeypatch):
        tfvars_path = tmp_path / "terraform.tfvars.json"
        monkeypatch.setattr("scripts.terraform.TFVARS_FILE", tfvars_path)

        cfg = _make_config(tmp_path)
        write_tfvars(cfg)

        with open(tfvars_path) as f:
            data = json.load(f)
        assert isinstance(data, dict)

    def test_contains_project_id(self, tmp_path, monkeypatch):
        tfvars_path = tmp_path / "terraform.tfvars.json"
        monkeypatch.setattr("scripts.terraform.TFVARS_FILE", tfvars_path)

        cfg = _make_config(tmp_path)
        write_tfvars(cfg)

        with open(tfvars_path) as f:
            data = json.load(f)
        assert data["projectid"] == "test-project-123"

    def test_contains_region(self, tmp_path, monkeypatch):
        tfvars_path = tmp_path / "terraform.tfvars.json"
        monkeypatch.setattr("scripts.terraform.TFVARS_FILE", tfvars_path)

        cfg = _make_config(tmp_path)
        write_tfvars(cfg)

        with open(tfvars_path) as f:
            data = json.load(f)
        assert data["region"] == "us-central1"

    def test_contains_domain(self, tmp_path, monkeypatch):
        tfvars_path = tmp_path / "terraform.tfvars.json"
        monkeypatch.setattr("scripts.terraform.TFVARS_FILE", tfvars_path)

        cfg = _make_config(tmp_path)
        write_tfvars(cfg)

        with open(tfvars_path) as f:
            data = json.load(f)
        assert data["domain"] == "voipbin.example.com"

    def test_contains_gke_fields(self, tmp_path, monkeypatch):
        tfvars_path = tmp_path / "terraform.tfvars.json"
        monkeypatch.setattr("scripts.terraform.TFVARS_FILE", tfvars_path)

        cfg = _make_config(tmp_path)
        write_tfvars(cfg)

        with open(tfvars_path) as f:
            data = json.load(f)
        assert data["gke_type"] == "zonal"
        assert data["gke_machine_type"] == "n1-standard-2"
        assert data["gke_node_count"] == 2

    def test_contains_vm_fields(self, tmp_path, monkeypatch):
        tfvars_path = tmp_path / "terraform.tfvars.json"
        monkeypatch.setattr("scripts.terraform.TFVARS_FILE", tfvars_path)

        cfg = _make_config(tmp_path)
        write_tfvars(cfg)

        with open(tfvars_path) as f:
            data = json.load(f)
        assert data["vm_machine_type"] == "f1-micro"
        assert data["kamailio_count"] == 1
        assert data["rtpengine_count"] == 1

    def test_overwrites_existing(self, tmp_path, monkeypatch):
        tfvars_path = tmp_path / "terraform.tfvars.json"
        monkeypatch.setattr("scripts.terraform.TFVARS_FILE", tfvars_path)

        # Write once
        cfg = _make_config(tmp_path)
        write_tfvars(cfg)

        # Change config and write again
        cfg.set("region", "europe-west1")
        write_tfvars(cfg)

        with open(tfvars_path) as f:
            data = json.load(f)
        assert data["region"] == "europe-west1"

    def test_all_terraform_var_keys_present(self, tmp_path, monkeypatch):
        tfvars_path = tmp_path / "terraform.tfvars.json"
        monkeypatch.setattr("scripts.terraform.TFVARS_FILE", tfvars_path)

        cfg = _make_config(tmp_path)
        write_tfvars(cfg)

        with open(tfvars_path) as f:
            data = json.load(f)

        expected_keys = {
            "projectid", "region", "zone", "gke_type", "gke_machine_type",
            "gke_node_count", "vm_machine_type", "kamailio_count",
            "rtpengine_count", "domain", "dns_mode", "tls_strategy",
        }
        assert set(data.keys()) == expected_keys

    def test_restricts_file_permissions(self, tmp_path, monkeypatch):
        tfvars_path = tmp_path / "terraform.tfvars.json"
        monkeypatch.setattr("scripts.terraform.TFVARS_FILE", tfvars_path)

        cfg = _make_config(tmp_path)
        write_tfvars(cfg)

        mode = tfvars_path.stat().st_mode & 0o777
        assert mode == 0o600, f"Expected 0o600, got {oct(mode)}"
