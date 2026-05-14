"""Tests for scripts/terraform.py — tfvars generation from config."""

import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.config import InstallerConfig
from scripts.terraform import TFVARS_FILE, terraform_state_list, terraform_state_rm, write_tfvars


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
        assert data["project_id"] == "test-project-123"

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
            "project_id", "region", "zone", "gke_type", "gke_machine_type",
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


class TestRunStateList:
    def test_returns_empty_set_when_state_list_fails(self, monkeypatch):
        import subprocess
        monkeypatch.setattr(
            "scripts.terraform.run_cmd",
            lambda *a, **kw: subprocess.CompletedProcess([], 1, stdout="", stderr="error"),
        )
        result = terraform_state_list(InstallerConfig())
        assert result == set()

    def test_returns_set_of_addresses(self, monkeypatch):
        import subprocess
        output = "google_compute_network.voipbin\ngoogle_service_account.sa_kamailio\n"
        monkeypatch.setattr(
            "scripts.terraform.run_cmd",
            lambda *a, **kw: subprocess.CompletedProcess([], 0, stdout=output, stderr=""),
        )
        result = terraform_state_list(InstallerConfig())
        assert result == {"google_compute_network.voipbin", "google_service_account.sa_kamailio"}

    def test_handles_empty_state(self, monkeypatch):
        import subprocess
        monkeypatch.setattr(
            "scripts.terraform.run_cmd",
            lambda *a, **kw: subprocess.CompletedProcess([], 0, stdout="", stderr=""),
        )
        result = terraform_state_list(InstallerConfig())
        assert result == set()

    def test_ignores_whitespace_only_lines(self, monkeypatch):
        import subprocess
        output = "google_compute_network.voipbin\n   \ngoogle_service_account.sa_kamailio\n"
        monkeypatch.setattr(
            "scripts.terraform.run_cmd",
            lambda *a, **kw: subprocess.CompletedProcess([], 0, stdout=output, stderr=""),
        )
        result = terraform_state_list(InstallerConfig())
        assert result == {"google_compute_network.voipbin", "google_service_account.sa_kamailio"}


class TestTerraformStateRm:
    """Tests for terraform_state_rm (T-AI-4 through T-AI-7)."""

    def test_tai_4_returns_true_on_exit_0(self, monkeypatch):
        """T-AI-4: returns True when terraform state rm exits 0."""
        monkeypatch.setattr(
            "scripts.terraform.run_cmd",
            lambda *a, **kw: subprocess.CompletedProcess([], 0, stdout="Removed 1 object(s).", stderr=""),
        )
        assert terraform_state_rm(["some.resource"]) is True

    def test_tai_5_returns_true_idempotent_not_found(self, monkeypatch):
        """T-AI-5: returns True (idempotent) when exit non-zero + 'No matching objects found'."""
        monkeypatch.setattr(
            "scripts.terraform.run_cmd",
            lambda *a, **kw: subprocess.CompletedProcess([], 1, stdout="No matching objects found", stderr=""),
        )
        assert terraform_state_rm(["missing.resource"]) is True

    def test_tai_5_not_found_in_stderr(self, monkeypatch):
        """T-AI-5 variant: 'No matching objects found' can also appear in stderr."""
        monkeypatch.setattr(
            "scripts.terraform.run_cmd",
            lambda *a, **kw: subprocess.CompletedProcess([], 1, stdout="", stderr="No matching objects found"),
        )
        assert terraform_state_rm(["missing.resource"]) is True

    def test_tai_6_returns_false_on_unrecognised_error(self, monkeypatch):
        """T-AI-6: returns False on non-zero exit with unrecognised error message."""
        monkeypatch.setattr(
            "scripts.terraform.run_cmd",
            lambda *a, **kw: subprocess.CompletedProcess([], 1, stdout="", stderr="Error: state locked"),
        )
        assert terraform_state_rm(["bad.resource"]) is False

    def test_tai_7_processes_each_resource_independently(self, monkeypatch):
        """T-AI-7: processes each resource independently; returns False only because of c.error."""
        responses = {
            "a.ok": subprocess.CompletedProcess([], 0, stdout="Removed 1 object(s).", stderr=""),
            "b.missing": subprocess.CompletedProcess([], 1, stdout="No matching objects found", stderr=""),
            "c.error": subprocess.CompletedProcess([], 1, stdout="", stderr="Error: something unexpected"),
        }

        def fake_run_cmd(cmd, **kw):
            # The resource address is the last element of the command
            addr = cmd[-1]
            return responses[addr]

        monkeypatch.setattr("scripts.terraform.run_cmd", fake_run_cmd)
        result = terraform_state_rm(["a.ok", "b.missing", "c.error"])
        assert result is False, "Should return False because c.error had unexpected error"

    def test_tai_7_all_succeed(self, monkeypatch):
        """T-AI-7 variant: all resources succeed → returns True."""
        responses = {
            "a.ok": subprocess.CompletedProcess([], 0, stdout="Removed 1 object(s).", stderr=""),
            "b.missing": subprocess.CompletedProcess([], 1, stdout="No matching objects found", stderr=""),
        }

        def fake_run_cmd(cmd, **kw):
            addr = cmd[-1]
            return responses[addr]

        monkeypatch.setattr("scripts.terraform.run_cmd", fake_run_cmd)
        result = terraform_state_rm(["a.ok", "b.missing"])
        assert result is True

    def test_tai_4_empty_list_returns_true(self, monkeypatch):
        """Edge case: empty list returns True (vacuously all succeeded)."""
        called = []
        monkeypatch.setattr(
            "scripts.terraform.run_cmd",
            lambda *a, **kw: called.append(True) or subprocess.CompletedProcess([], 0, "", ""),
        )
        result = terraform_state_rm([])
        assert result is True
        assert not called, "run_cmd should not be called for empty list"
