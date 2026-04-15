"""Terraform operations for VoIPBin installer."""

import json
from pathlib import Path
from typing import Any

from scripts.config import InstallerConfig
from scripts.display import console, print_error, print_step, print_success
from scripts.utils import INSTALLER_DIR, run_cmd


TERRAFORM_DIR = INSTALLER_DIR / "terraform"
TFVARS_FILE = TERRAFORM_DIR / "terraform.tfvars.json"


def write_tfvars(config: InstallerConfig) -> Path:
    """Write terraform.tfvars.json from installer config. Returns the file path."""
    tf_vars = config.to_terraform_vars()
    with open(TFVARS_FILE, "w") as f:
        json.dump(tf_vars, f, indent=2)
    return TFVARS_FILE


def terraform_init(config: InstallerConfig) -> bool:
    """Run terraform init. Returns True on success."""
    write_tfvars(config)
    project_id = config.get("gcp_project_id", "")
    cmd = (
        f"terraform -chdir={TERRAFORM_DIR} init"
        f" -backend-config=prefix=voipbin/{project_id}"
    )
    print_step(f"Running: terraform init")
    result = run_cmd(cmd, capture=True, timeout=300)
    if result.returncode != 0:
        print_error(f"terraform init failed:\n{result.stderr}")
        return False
    print_success("Terraform initialized")
    return True


def terraform_plan(config: InstallerConfig) -> bool:
    """Run terraform plan. Returns True on success."""
    write_tfvars(config)
    cmd = (
        f"terraform -chdir={TERRAFORM_DIR} plan"
        f" -var-file={TFVARS_FILE}"
        f" -out=tfplan"
    )
    print_step("Running: terraform plan")
    result = run_cmd(cmd, capture=True, timeout=600)
    if result.returncode != 0:
        print_error(f"terraform plan failed:\n{result.stderr}")
        return False
    if result.stdout:
        console.print(result.stdout)
    print_success("Terraform plan generated")
    return True


def terraform_apply(config: InstallerConfig, auto_approve: bool = False) -> bool:
    """Run terraform apply. Returns True on success."""
    write_tfvars(config)
    approve_flag = "-auto-approve" if auto_approve else ""
    cmd = (
        f"terraform -chdir={TERRAFORM_DIR} apply"
        f" {approve_flag}"
        f" -var-file={TFVARS_FILE}"
    )
    print_step("Running: terraform apply")
    result = run_cmd(cmd, capture=False, timeout=1800)
    if result.returncode != 0:
        print_error("terraform apply failed")
        return False
    print_success("Terraform apply complete")
    return True


def terraform_destroy(config: InstallerConfig, auto_approve: bool = False) -> bool:
    """Run terraform destroy. Returns True on success."""
    write_tfvars(config)
    approve_flag = "-auto-approve" if auto_approve else ""
    cmd = (
        f"terraform -chdir={TERRAFORM_DIR} destroy"
        f" {approve_flag}"
        f" -var-file={TFVARS_FILE}"
    )
    print_step("Running: terraform destroy")
    result = run_cmd(cmd, capture=False, timeout=1800)
    if result.returncode != 0:
        print_error("terraform destroy failed")
        return False
    print_success("Terraform destroy complete")
    return True


def terraform_output(config: InstallerConfig) -> dict[str, Any]:
    """Parse Terraform outputs as a dict. Returns empty dict on failure."""
    cmd = f"terraform -chdir={TERRAFORM_DIR} output -json"
    result = run_cmd(cmd, capture=True, timeout=60)
    if result.returncode != 0:
        print_error(f"terraform output failed:\n{result.stderr}")
        return {}
    try:
        raw = json.loads(result.stdout)
    except json.JSONDecodeError:
        print_error("Failed to parse terraform output JSON")
        return {}
    # Flatten: terraform output -json wraps each value in {"value": ..., "type": ...}
    return {k: v.get("value", v) for k, v in raw.items()}


def terraform_resource_count(config: InstallerConfig) -> int:
    """Return the number of resources in the Terraform state, or -1 on error."""
    cmd = f"terraform -chdir={TERRAFORM_DIR} state list"
    result = run_cmd(cmd, capture=True, timeout=60)
    if result.returncode != 0:
        return -1
    lines = [line for line in result.stdout.strip().splitlines() if line.strip()]
    return len(lines)
