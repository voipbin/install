"""Ansible operations for VoIPBin installer."""

import json
import tempfile
from pathlib import Path
from typing import Any

from scripts.config import InstallerConfig
from scripts.display import print_error, print_step, print_success
from scripts.utils import INSTALLER_DIR, run_cmd


ANSIBLE_DIR = INSTALLER_DIR / "ansible"
PLAYBOOK_SITE = ANSIBLE_DIR / "playbooks" / "site.yml"
INVENTORY_SCRIPT = ANSIBLE_DIR / "inventory" / "gcp_inventory.py"


def _write_extra_vars(
    config: InstallerConfig,
    terraform_outputs: dict[str, Any],
) -> Path:
    """Write a temporary extra-vars JSON file combining config + Terraform outputs."""
    ansible_vars = config.to_ansible_vars()
    ansible_vars["terraform_outputs"] = terraform_outputs
    # Flatten common Terraform outputs into top-level vars for Ansible roles
    ansible_vars["cloudsql_connection_name"] = terraform_outputs.get(
        "cloudsql_connection_name", ""
    )
    ansible_vars["cloudsql_ip"] = terraform_outputs.get("cloudsql_ip", "")
    ansible_vars["kamailio_internal_ips"] = terraform_outputs.get(
        "kamailio_internal_ips", []
    )
    ansible_vars["rtpengine_external_ips"] = terraform_outputs.get(
        "rtpengine_external_ips", []
    )
    ansible_vars["kamailio_external_lb_ip"] = terraform_outputs.get(
        "kamailio_external_lb_ip", ""
    )
    tmp = tempfile.NamedTemporaryFile(
        mode="w", suffix=".json", prefix="voipbin_extra_vars_", delete=False
    )
    json.dump(ansible_vars, tmp, indent=2)
    tmp.close()
    return Path(tmp.name)


def ansible_run(
    config: InstallerConfig,
    terraform_outputs: dict[str, Any],
) -> bool:
    """Run site.yml with inventory and extra vars. Returns True on success."""
    extra_vars_path = _write_extra_vars(config, terraform_outputs)
    project_id = config.get("gcp_project_id", "")
    zone = config.get("zone", "")
    cmd = (
        f"ansible-playbook {PLAYBOOK_SITE}"
        f" --inventory {INVENTORY_SCRIPT}"
        f" --extra-vars @{extra_vars_path}"
        f" -e gcp_project={project_id}"
        f" -e gcp_zone={zone}"
    )
    print_step("Running: ansible-playbook site.yml")
    result = run_cmd(cmd, capture=False, timeout=1800)
    # Clean up temp file
    extra_vars_path.unlink(missing_ok=True)
    if result.returncode != 0:
        print_error("Ansible playbook failed")
        return False
    print_success("Ansible playbook complete")
    return True


def ansible_check(
    config: InstallerConfig,
    terraform_outputs: dict[str, Any],
) -> bool:
    """Dry-run Ansible with --check. Returns True on success."""
    extra_vars_path = _write_extra_vars(config, terraform_outputs)
    project_id = config.get("gcp_project_id", "")
    zone = config.get("zone", "")
    cmd = (
        f"ansible-playbook {PLAYBOOK_SITE}"
        f" --inventory {INVENTORY_SCRIPT}"
        f" --extra-vars @{extra_vars_path}"
        f" -e gcp_project={project_id}"
        f" -e gcp_zone={zone}"
        f" --check --diff"
    )
    print_step("Running: ansible-playbook --check (dry run)")
    result = run_cmd(cmd, capture=False, timeout=600)
    extra_vars_path.unlink(missing_ok=True)
    if result.returncode != 0:
        print_error("Ansible check failed")
        return False
    print_success("Ansible check passed")
    return True
