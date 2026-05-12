"""PR-N: OS Login SSH access for Kamailio/RTPEngine VMs (GAP-40).

Background: dogfood run #8 failed at the Ansible stage with
'sa_ansible@instance-kamailio-...: Permission denied (publickey)'. Root
cause: VMs had enable-oslogin=TRUE but the inventory hardcoded the POSIX
username 'sa_ansible' and connected via IAP. OS Login generates per-IAM
POSIX usernames (e.g. pchero_voipbin_net) and authenticates with the
operator's OS Login-registered SSH key, so the hardcoded user could never
succeed.

Decision (CEO + CPO, May 12 2026): grant Kamailio/RTPEngine ephemeral
public IPs, allow port 22 from 0.0.0.0/0 (OS Login + publickey enforces
auth), and drop the IAP wrapper for a flatter operator UX.

Tests below pin the contract this PR enforces:
  - terraform/kamailio.tf has an access_config block (public IP).
  - terraform/firewall.tf has fw_vm_ssh with source_ranges 0.0.0.0/0 and
    targets both kamailio and rtpengine tags.
  - terraform/outputs.tf exposes kamailio_external_ips.
  - The inventory uses the resolved OS Login username and the external IP.
  - preflight.check_oslogin_setup() returns the expected remediation when
    any of the three OS Login prerequisites are missing.
  - pipeline._run_ansible() calls check_oslogin_setup() before invoking
    ansible_run.
"""

import os
import re
import subprocess
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest


REPO = Path(__file__).resolve().parent.parent


class TestTerraformPublicIp:
    def test_kamailio_has_access_config(self):
        content = (REPO / "terraform" / "kamailio.tf").read_text()
        # access_config{} block (empty body) inside network_interface signals
        # an ephemeral public IP.
        assert re.search(r"access_config\s*\{\s*\}", content), (
            "kamailio.tf must declare access_config {} on network_interface "
            "so Kamailio VMs receive an ephemeral public IP for OS Login SSH."
        )

    def test_rtpengine_keeps_static_external_ip(self):
        content = (REPO / "terraform" / "rtpengine.tf").read_text()
        # RTPEngine already had access_config with nat_ip; PR-N must not
        # remove it.
        assert "nat_ip" in content and "google_compute_address.rtpengine" in content


class TestFirewallVmSsh:
    def test_fw_vm_ssh_replaces_fw_iap_ssh(self):
        content = (REPO / "terraform" / "firewall.tf").read_text()
        assert "fw_iap_ssh" not in content, (
            "fw_iap_ssh (IAP-only ingress) must be replaced by fw_vm_ssh "
            "(direct ingress) under the OS Login model."
        )
        assert "fw_vm_ssh" in content

    def test_fw_vm_ssh_allows_world_on_port_22(self):
        content = (REPO / "terraform" / "firewall.tf").read_text()
        # Locate the fw_vm_ssh block by splitting on the resource header.
        m = re.search(
            r'resource\s+"google_compute_firewall"\s+"fw_vm_ssh"',
            content,
        )
        assert m, "fw_vm_ssh resource block missing"
        # Take everything until the next 'resource "google_compute_firewall"'
        # declaration (or end of file).
        tail = content[m.end():]
        next_resource = re.search(
            r'\nresource\s+"google_compute_firewall"', tail,
        )
        block = tail if next_resource is None else tail[: next_resource.start()]
        sr = re.search(r'source_ranges\s*=\s*\[([^\]]*)\]', block)
        assert sr, "fw_vm_ssh must declare source_ranges"
        assert '"0.0.0.0/0"' in sr.group(1), (
            "fw_vm_ssh source_ranges must be 0.0.0.0/0 (per CEO decision F2: "
            "OS Login publickey is the enforcement layer)."
        )
        # The block must also open port 22.
        assert '"22"' in block, "fw_vm_ssh must allow port 22"

    def test_fw_vm_ssh_targets_both_vm_tags(self):
        content = (REPO / "terraform" / "firewall.tf").read_text()
        m = re.search(
            r'resource\s+"google_compute_firewall"\s+"fw_vm_ssh"',
            content,
        )
        tail = content[m.end():]
        next_resource = re.search(
            r'\nresource\s+"google_compute_firewall"', tail,
        )
        block = tail if next_resource is None else tail[: next_resource.start()]
        tt = re.search(r'target_tags\s*=\s*\[([^\]]*)\]', block)
        assert tt
        tags = tt.group(1)
        assert '"kamailio"' in tags and '"rtpengine"' in tags


class TestKamailioExternalIpOutput:
    def test_output_declared(self):
        content = (REPO / "terraform" / "outputs.tf").read_text()
        assert 'output "kamailio_external_ips"' in content, (
            "outputs.tf must expose kamailio_external_ips so the inventory "
            "can target the VMs over their ephemeral public IPs."
        )
        # Must reference access_config[0].nat_ip from the kamailio resource.
        assert "access_config[0].nat_ip" in content


class TestInventoryOsLoginUser:
    def test_inventory_imports_subprocess(self):
        # get_oslogin_username uses subprocess; sanity check it imports.
        from ansible.inventory import gcp_inventory  # noqa: F401  -- import smoke
        assert hasattr(gcp_inventory, "get_oslogin_username")

    def test_inventory_no_longer_hardcodes_sa_ansible(self):
        content = (REPO / "ansible" / "inventory" / "gcp_inventory.py").read_text()
        assert "sa_ansible" not in content, (
            "Inventory must not hardcode sa_ansible; OS Login resolves the "
            "POSIX username per IAM identity."
        )

    def test_inventory_no_longer_uses_iap_tunnel(self):
        content = (REPO / "ansible" / "inventory" / "gcp_inventory.py").read_text()
        assert "start-iap-tunnel" not in content, (
            "Inventory must not build IAP ProxyCommand; PR-N connects directly "
            "to the VM's ephemeral external IP via OS Login."
        )

    def test_get_oslogin_username_honors_env_override(self, monkeypatch):
        from ansible.inventory.gcp_inventory import get_oslogin_username
        monkeypatch.setenv("ANSIBLE_USER", "custom_user")
        assert get_oslogin_username() == "custom_user"

    def test_get_oslogin_username_queries_gcloud(self, monkeypatch):
        from ansible.inventory import gcp_inventory
        monkeypatch.delenv("ANSIBLE_USER", raising=False)
        fake = MagicMock(returncode=0, stdout="pchero_voipbin_net\n", stderr="")
        with patch.object(gcp_inventory.subprocess, "run", return_value=fake) as mock:
            assert gcp_inventory.get_oslogin_username() == "pchero_voipbin_net"
            args = mock.call_args[0][0]
            assert args[:5] == ["gcloud", "compute", "os-login", "describe-profile",
                                "--format=value(posixAccounts[0].username)"]

    def test_get_oslogin_username_returns_empty_on_failure(self, monkeypatch):
        from ansible.inventory import gcp_inventory
        monkeypatch.delenv("ANSIBLE_USER", raising=False)
        fake = MagicMock(returncode=1, stdout="", stderr="ERROR")
        with patch.object(gcp_inventory.subprocess, "run", return_value=fake):
            assert gcp_inventory.get_oslogin_username() == ""

    def test_get_oslogin_username_handles_missing_gcloud(self, monkeypatch):
        """If gcloud is not on PATH, get_oslogin_username must return ''
        rather than propagating FileNotFoundError."""
        from ansible.inventory import gcp_inventory
        monkeypatch.delenv("ANSIBLE_USER", raising=False)
        with patch.object(gcp_inventory.subprocess, "run",
                          side_effect=FileNotFoundError("gcloud")):
            assert gcp_inventory.get_oslogin_username() == ""


class TestInventoryBuildsExternalIp:
    def _outputs(self, **kwargs):
        defaults = {
            "project_id": {"value": "p1"},
            "region": {"value": "us-central1"},
            "zone": {"value": "us-central1-a"},
            "kamailio_instance_names": {"value": ["instance-kamailio-0"]},
            "kamailio_internal_ips": {"value": ["10.0.0.3"]},
            "kamailio_external_ips": {"value": ["34.10.10.10"]},
            "rtpengine_instance_names": {"value": ["instance-rtpengine-0"]},
            "rtpengine_internal_ips": {"value": ["10.0.0.4"]},
            "rtpengine_external_ips": {"value": ["34.10.10.20"]},
        }
        defaults.update(kwargs)
        return defaults

    def test_kamailio_ansible_host_is_external_ip(self, monkeypatch):
        monkeypatch.setenv("ANSIBLE_USER", "test_user")
        from ansible.inventory.gcp_inventory import build_inventory
        inv = build_inventory(self._outputs())
        host = inv["_meta"]["hostvars"]["instance-kamailio-0"]
        assert host["ansible_host"] == "34.10.10.10"
        assert host["ansible_user"] == "test_user"
        assert "google_compute_engine" in host["ansible_ssh_common_args"]
        assert "ProxyCommand" not in host["ansible_ssh_common_args"]

    def test_rtpengine_ansible_host_is_external_ip(self, monkeypatch):
        monkeypatch.setenv("ANSIBLE_USER", "test_user")
        from ansible.inventory.gcp_inventory import build_inventory
        inv = build_inventory(self._outputs())
        host = inv["_meta"]["hostvars"]["instance-rtpengine-0"]
        assert host["ansible_host"] == "34.10.10.20"

    def test_falls_back_to_instance_name_when_external_ips_empty(self, monkeypatch):
        """Defensive: an older terraform state may not yet expose the new
        kamailio_external_ips output (or the output may be []). The inventory
        must fall back to the instance name rather than crashing or assigning
        an empty ansible_host."""
        monkeypatch.setenv("ANSIBLE_USER", "test_user")
        from ansible.inventory.gcp_inventory import build_inventory
        out = self._outputs(kamailio_external_ips={"value": []})
        inv = build_inventory(out)
        host = inv["_meta"]["hostvars"]["instance-kamailio-0"]
        # No external IP -> fall back to instance name (DNS-resolvable inside GCP)
        assert host["ansible_host"] == "instance-kamailio-0"
        assert host["external_ip"] == ""

    def test_falls_back_when_external_ips_output_absent(self, monkeypatch):
        """Even more defensive: the entire kamailio_external_ips output is
        missing from terraform state (state generated by an older install)."""
        monkeypatch.setenv("ANSIBLE_USER", "test_user")
        from ansible.inventory.gcp_inventory import build_inventory
        out = self._outputs()
        del out["kamailio_external_ips"]
        inv = build_inventory(out)
        host = inv["_meta"]["hostvars"]["instance-kamailio-0"]
        assert host["ansible_host"] == "instance-kamailio-0"


class TestPreflightOsLoginSetup:
    def test_returns_error_when_key_missing(self, tmp_path, monkeypatch):
        from scripts import preflight
        monkeypatch.setattr(preflight.os.path, "expanduser",
                            lambda p: str(tmp_path / "does-not-exist"))
        err = preflight.check_oslogin_setup()
        assert err is not None
        assert "gcloud compute config-ssh" in err
        assert "google_compute_engine" in err

    def test_returns_error_when_profile_query_fails(self, tmp_path, monkeypatch):
        from scripts import preflight
        key_file = tmp_path / "k"
        key_file.write_text("x")
        monkeypatch.setattr(preflight.os.path, "expanduser", lambda p: str(key_file))
        calls = {"n": 0}

        def fake_run_cmd(*args, **kwargs):
            calls["n"] += 1
            return MagicMock(returncode=1, stdout="", stderr="err")

        monkeypatch.setattr(preflight, "run_cmd", fake_run_cmd)
        err = preflight.check_oslogin_setup()
        assert err is not None
        assert "Could not query" in err, (
            "When describe-profile returns non-zero, the error must pin the "
            "'Could not query' branch rather than fall through to a different "
            "remediation that happens to share substrings."
        )

    def test_returns_error_when_no_keys_registered(self, tmp_path, monkeypatch):
        from scripts import preflight
        key_file = tmp_path / "k"
        key_file.write_text("x")
        monkeypatch.setattr(preflight.os.path, "expanduser", lambda p: str(key_file))
        results = iter([
            MagicMock(returncode=0, stdout="pchero_voipbin_net\n", stderr=""),
            MagicMock(returncode=0, stdout="", stderr=""),
        ])
        monkeypatch.setattr(preflight, "run_cmd",
                            lambda *a, **k: next(results))
        err = preflight.check_oslogin_setup()
        assert err is not None
        assert "no ssh keys" in err.lower() or "ssh-keys" in err

    def test_returns_none_on_full_success(self, tmp_path, monkeypatch):
        from scripts import preflight
        key_file = tmp_path / "k"
        key_file.write_text("x")
        monkeypatch.setattr(preflight.os.path, "expanduser", lambda p: str(key_file))
        results = iter([
            MagicMock(returncode=0, stdout="pchero_voipbin_net\n", stderr=""),
            MagicMock(returncode=0, stdout="ssh-rsa AAAA...\n", stderr=""),
        ])
        monkeypatch.setattr(preflight, "run_cmd",
                            lambda *a, **k: next(results))
        assert preflight.check_oslogin_setup() is None


class TestPipelineCallsOsLoginPreflight:
    def test_run_ansible_invokes_check_oslogin_setup(self):
        from scripts import pipeline
        cfg = MagicMock()
        outputs = {"kamailio_internal_ips": ["10.0.0.3"]}
        with patch("scripts.preflight.check_oslogin_setup",
                   return_value="forced error") as mock:
            ok = pipeline._run_ansible(cfg, outputs, dry_run=False,
                                       auto_approve=True)
        assert mock.called
        assert ok is False

    def test_run_ansible_skips_preflight_on_dry_run(self):
        from scripts import pipeline
        cfg = MagicMock()
        with patch("scripts.preflight.check_oslogin_setup") as mock:
            pipeline._run_ansible(cfg, {}, dry_run=True, auto_approve=True)
        assert not mock.called
