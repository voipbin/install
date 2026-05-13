"""Ansible operations for VoIPBin installer."""

import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any
from urllib.parse import quote as _urlquote

from scripts.config import InstallerConfig
from scripts.display import print_error, print_step, print_success
from scripts.utils import INSTALLER_DIR, run_cmd


ANSIBLE_DIR = INSTALLER_DIR / "ansible"
PLAYBOOK_SITE = ANSIBLE_DIR / "playbooks" / "site.yml"
INVENTORY_SCRIPT = ANSIBLE_DIR / "inventory" / "gcp_inventory.py"

# Ansible config-related env vars that operators may have exported from their
# shell profile, CI runner, or direnv. Each one can silently override our
# repo-shipped ansible/ansible.cfg and re-introduce role/inventory/roles_path
# resolution bugs (see GAP-39). We pin ANSIBLE_CONFIG and strip the rest so
# the playbook always loads OUR config.
_ANSIBLE_OVERRIDE_VARS = (
    "ANSIBLE_ROLES_PATH",
    "ANSIBLE_INVENTORY",
    "ANSIBLE_COLLECTIONS_PATH",
    "ANSIBLE_COLLECTIONS_PATHS",
    "ANSIBLE_LIBRARY",
    "ANSIBLE_ACTION_PLUGINS",
    "ANSIBLE_CALLBACK_PLUGINS",
    "ANSIBLE_FILTER_PLUGINS",
)


def _build_ansible_env() -> dict[str, str]:
    """Return an environment dict that forces our ansible.cfg to win.

    Ansible config precedence is:
        ANSIBLE_CONFIG (env) > ./ansible.cfg > ~/.ansible.cfg > /etc/ansible/ansible.cfg

    To guarantee deterministic behavior across operator environments, we:
      1. Pin ANSIBLE_CONFIG to the repo's ansible/ansible.cfg.
      2. Strip ANSIBLE_ROLES_PATH and similar overrides so the values from
         our config are not preempted by env vars.
    """
    env = os.environ.copy()
    env["ANSIBLE_CONFIG"] = str(ANSIBLE_DIR / "ansible.cfg")
    for var in _ANSIBLE_OVERRIDE_VARS:
        env.pop(var, None)
    return env


# Locked password alphabet from PR-D2a terraform override_special.
# See docs/operations/cloudsql-credentials.md for rotation notes and
# what to update when widening the alphabet.
_KAMAILIORO_URL_ALPHABET_RE = re.compile(r"^[A-Za-z0-9!*+\-._~]+$")


def _build_kamailio_auth_db_url(
    config: InstallerConfig,
    terraform_outputs: dict[str, Any],
) -> str:
    """Return the kamailio_auth_db_url for the env.j2 template.

    Sources the kamailioro password from terraform outputs and the MySQL host
    from config (populated by terraform_reconcile.py from the
    cloudsql_mysql_private_ip terraform output). Returns "" when either side
    is missing so dev / early-apply flows do not crash. Raises RuntimeError
    if the password contains characters outside the locked URL-safe alphabet
    so a future alphabet widening is caught loudly. Percent-encodes the
    password via urllib.parse.quote with safe="!*-._~" so locked-alphabet
    specials round-trip literally and only "+" is encoded to "%2B"; this
    avoids ambiguity with MySQL URL parsers that treat "+" as form-encoded
    space.
    """
    raw_password = terraform_outputs.get("cloudsql_mysql_password_kamailioro", "")
    if raw_password is None:
        raw_password = ""
    mysql_host = str(config.get("cloudsql_private_ip", "") or "").strip()
    if not raw_password or not mysql_host:
        return ""
    if not _KAMAILIORO_URL_ALPHABET_RE.match(raw_password):
        raise RuntimeError(
            "kamailioro password contains characters outside the locked "
            "URL-safe alphabet (A-Za-z0-9 + '!*+-._~'). Update terraform "
            "override_special and the URL escape logic together. "
            "See docs/operations/cloudsql-credentials.md."
        )
    encoded = _urlquote(raw_password, safe="!*-._~")
    return f"mysql://kamailioro:{encoded}@{mysql_host}:3306/asterisk"


def _build_rtpengine_socks(terraform_outputs: dict[str, Any]) -> str:
    """Return the RTPENGINE_SOCKS string for env.j2 template.

    Format. space-separated ``udp:<ip>:22222`` per ng-protocol endpoint.
    Sourced from terraform output ``rtpengine_external_ips`` (list of
    strings). Returns ``""`` if the list is missing, empty, or not a list so
    dev / early-apply flows do not crash; group_vars then keeps Kamailio's
    existing empty-string fallback. Port 22222 is the rtpengine ng control
    protocol UDP port (confirmed via the existing kamailio.yml group_vars
    comment and the voip-rtpengine Helm chart).
    """
    ips = terraform_outputs.get("rtpengine_external_ips", []) or []
    if not isinstance(ips, list):
        return ""
    parts = [
        f"udp:{ip}:22222"
        for ip in ips
        if isinstance(ip, str) and ip.strip()
    ]
    return " ".join(parts)


def _write_extra_vars(
    config: InstallerConfig,
    terraform_outputs: dict[str, Any],
) -> Path:
    """Write a temporary extra-vars JSON file combining config + Terraform outputs.

    The file is created with restricted permissions (0o600) since it may
    contain sensitive data like database passwords and API keys.
    """
    ansible_vars = config.to_ansible_vars()
    ansible_vars["terraform_outputs"] = terraform_outputs
    # Flatten common Terraform outputs into top-level vars for Ansible roles
    ansible_vars["kamailio_internal_ips"] = terraform_outputs.get(
        "kamailio_internal_ips", []
    )
    ansible_vars["rtpengine_external_ips"] = terraform_outputs.get(
        "rtpengine_external_ips", []
    )
    ansible_vars["kamailio_external_lb_ip"] = terraform_outputs.get(
        "kamailio_external_lb_ip", ""
    )
    ansible_vars["kamailio_internal_lb_ip"] = terraform_outputs.get(
        "kamailio_internal_lb_ip", ""
    )
    # PR-T: Flatten k8s LoadBalancer Service externalIPs (harvested by
    # reconcile_k8s_outputs stage and merged/rehydrated into terraform_outputs
    # by run_pipeline) so the Kamailio env.j2 template can consume them as
    # top-level Ansible vars. Each `.get(..., "")` default keeps dev / early-
    # apply / dry-run flows from crashing when the harvest stage has not
    # populated a given key yet; group_vars/kamailio.yml then supplies any
    # role-level fallback. Keys must match scripts.k8s._LB_SERVICES tuple-3
    # output-key column exactly. drift will silently empty env.j2 slots and
    # CrashLoop Kamailio. tests/test_pr_t_ansible_k8s_lb_flat_vars.py pins
    # the contract.
    #
    # Note on `or ""` coercion: `.get(key, "")` only fires on MISSING key.
    # An explicit `None` in terraform_outputs (e.g. a YAML `~` from a
    # hand-edited state.yaml) would slip through and Jinja2 in env.j2 has no
    # `default('')` filter on these LB IP slots — `REDIS_URL=...@None:6379`
    # would render literally. The trailing `or ""` collapses both missing-
    # and None-valued cases to empty string before they reach Ansible.
    ansible_vars["redis_lb_ip"] = terraform_outputs.get("redis_lb_ip", "") or ""
    ansible_vars["rabbitmq_lb_ip"] = (
        terraform_outputs.get("rabbitmq_lb_ip", "") or ""
    )
    ansible_vars["asterisk_call_lb_ip"] = (
        terraform_outputs.get("asterisk_call_lb_ip", "") or ""
    )
    ansible_vars["asterisk_registrar_lb_ip"] = (
        terraform_outputs.get("asterisk_registrar_lb_ip", "") or ""
    )
    ansible_vars["asterisk_conference_lb_ip"] = (
        terraform_outputs.get("asterisk_conference_lb_ip", "") or ""
    )
    # PR-U-1: heplify-server LoadBalancer IP for Kamailio HOMER_URI wiring
    # (consumed by env.j2 in PR-U-3, currently no-op until that PR lands).
    ansible_vars["heplify_lb_ip"] = (
        terraform_outputs.get("heplify_lb_ip", "") or ""
    )
    ansible_vars["rtpengine_socks"] = _build_rtpengine_socks(terraform_outputs)
    ansible_vars["kamailio_auth_db_url"] = _build_kamailio_auth_db_url(
        config, terraform_outputs
    )
    # Create temp file with restricted permissions (owner-only read/write)
    fd = tempfile.mkstemp(suffix=".json", prefix="voipbin_extra_vars_")
    os.fchmod(fd[0], 0o600)
    with os.fdopen(fd[0], "w") as f:
        json.dump(ansible_vars, f, indent=2)
    return Path(fd[1])


def ansible_run(
    config: InstallerConfig,
    terraform_outputs: dict[str, Any],
) -> bool:
    """Run site.yml with inventory and extra vars. Returns True on success."""
    extra_vars_path = _write_extra_vars(config, terraform_outputs)
    try:
        project_id = config.get("gcp_project_id", "")
        zone = config.get("zone", "")
        cmd = [
            "ansible-playbook", str(PLAYBOOK_SITE),
            "--inventory", str(INVENTORY_SCRIPT),
            "--extra-vars", f"@{extra_vars_path}",
            "-e", f"gcp_project={project_id}",
            "-e", f"gcp_zone={zone}",
        ]
        print_step("Running: ansible-playbook site.yml")
        # cwd=ANSIBLE_DIR loads ansible/ansible.cfg via the ./ansible.cfg
        # precedence rule, and env pins ANSIBLE_CONFIG + strips operator
        # overrides so role/inventory resolution is deterministic regardless
        # of caller environment (GAP-39 hardening).
        result = run_cmd(
            cmd, capture=False, timeout=1800,
            cwd=ANSIBLE_DIR, env=_build_ansible_env(),
        )
        if result.returncode != 0:
            print_error("Ansible playbook failed")
            return False
        print_success("Ansible playbook complete")
        return True
    finally:
        extra_vars_path.unlink(missing_ok=True)


def ansible_check(
    config: InstallerConfig,
    terraform_outputs: dict[str, Any],
) -> bool:
    """Dry-run Ansible with --check. Returns True on success."""
    extra_vars_path = _write_extra_vars(config, terraform_outputs)
    try:
        project_id = config.get("gcp_project_id", "")
        zone = config.get("zone", "")
        cmd = [
            "ansible-playbook", str(PLAYBOOK_SITE),
            "--inventory", str(INVENTORY_SCRIPT),
            "--extra-vars", f"@{extra_vars_path}",
            "-e", f"gcp_project={project_id}",
            "-e", f"gcp_zone={zone}",
            "--check", "--diff",
        ]
        print_step("Running: ansible-playbook --check (dry run)")
        result = run_cmd(
            cmd, capture=False, timeout=600,
            cwd=ANSIBLE_DIR, env=_build_ansible_env(),
        )
        if result.returncode != 0:
            print_error("Ansible check failed")
            return False
        print_success("Ansible check passed")
        return True
    finally:
        extra_vars_path.unlink(missing_ok=True)
