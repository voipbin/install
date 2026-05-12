"""PR-C tests — Cloud SQL MySQL private-IP flip + dead SA removal.

Covers design §2.7:
1. cloudsql.tf: MySQL instance is private-IP-only with allocated_ip_range
2. cloudsql.tf: depends_on includes google_service_networking_connection.voipbin
3. cloudsql.tf: no sa_cloudsql_proxy SA or IAM binding remains
4. terraform/service_accounts.tf no longer exists
5. outputs.tf: cloudsql_ip output removed
6. outputs.tf: cloudsql_mysql_private_ip output added
7. FIELD_MAP includes cloudsql_mysql_private_ip -> cloudsql_private_ip
8. FIELD_MAP includes cloudsql_peering_range_cidr -> cloudsql_private_ip_cidr
9. _is_valid_ipv4_address rejects sentinel/empty/None/IPv6/non-string
10. _is_valid_ipv4_cidr accepts CIDR, rejects bare addr, IPv6, garbage
11. ansible_runner.py no longer references the dead flatten keys
12. outputs() populates cloudsql_private_ip and cloudsql_private_ip_cidr
"""

from __future__ import annotations

import re
import sys
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.terraform_reconcile import (  # noqa: E402
    FIELD_MAP,
    _is_valid_ipv4_address,
    _is_valid_ipv4_cidr,
    outputs,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
CLOUDSQL_TF = REPO_ROOT / "terraform" / "cloudsql.tf"
OUTPUTS_TF = REPO_ROOT / "terraform" / "outputs.tf"
SERVICE_ACCOUNTS_TF = REPO_ROOT / "terraform" / "service_accounts.tf"
ANSIBLE_RUNNER_PY = REPO_ROOT / "scripts" / "ansible_runner.py"


def _extract_block(text: str, header_re: str) -> str:
    """Return the body of the first top-level `{...}` block following header_re.

    Brace-balanced — adapted from tests/test_pr_b_vpc_peering_scaffold.py.
    """
    m = re.search(header_re + r"\s*\{", text)
    assert m, f"Header not found: {header_re}"
    i = m.end()
    depth = 1
    start = i
    while i < len(text) and depth > 0:
        c = text[i]
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return text[start:i]
        i += 1
    raise AssertionError(f"Unbalanced braces for header {header_re}")


# ---------------------------------------------------------------------------
# 1-2. Cloud SQL MySQL instance private-IP-only
# ---------------------------------------------------------------------------

def test_cloudsql_mysql_private_only():
    text = CLOUDSQL_TF.read_text()
    instance_body = _extract_block(
        text, r'resource\s+"google_sql_database_instance"\s+"voipbin"'
    )
    settings_body = _extract_block(instance_body, r"settings")
    ip_cfg_body = _extract_block(settings_body, r"ip_configuration")
    assert re.search(r"ipv4_enabled\s*=\s*false", ip_cfg_body), \
        "ipv4_enabled must be false"
    assert "google_compute_network.voipbin.id" in ip_cfg_body, \
        "private_network must reference voipbin VPC"
    assert re.search(r'ssl_mode\s*=\s*"ENCRYPTED_ONLY"', ip_cfg_body)
    assert "google_compute_global_address.cloudsql_peering.name" in ip_cfg_body, \
        "allocated_ip_range must reference PR-B peering range"


def test_cloudsql_depends_on_peering():
    text = CLOUDSQL_TF.read_text()
    instance_body = _extract_block(
        text, r'resource\s+"google_sql_database_instance"\s+"voipbin"'
    )
    # depends_on = [ ... ]
    m = re.search(r"depends_on\s*=\s*\[(.*?)\]", instance_body, re.DOTALL)
    assert m, "depends_on block not found"
    deps = m.group(1)
    assert "time_sleep.api_propagation" in deps
    assert "google_service_networking_connection.voipbin" in deps


# ---------------------------------------------------------------------------
# 3. Dead service account + IAM binding removed
# ---------------------------------------------------------------------------

def test_no_cloudsql_proxy_sa():
    text = CLOUDSQL_TF.read_text()
    assert "sa_cloudsql_proxy" not in text, \
        "sa_cloudsql_proxy resource must be deleted"
    assert "sa_cloudsql_proxy_client" not in text, \
        "sa_cloudsql_proxy_client IAM binding must be deleted"
    assert "sa-${var.env}-cloudsql-proxy" not in text


# ---------------------------------------------------------------------------
# 4. service_accounts.tf comment-only stub deleted
# ---------------------------------------------------------------------------

def test_service_accounts_tf_removed():
    assert not SERVICE_ACCOUNTS_TF.exists(), \
        f"{SERVICE_ACCOUNTS_TF} must be deleted (A-1 cleanup)"


# ---------------------------------------------------------------------------
# 5-6. Outputs flipped
# ---------------------------------------------------------------------------

def test_cloudsql_ip_output_deleted():
    text = OUTPUTS_TF.read_text()
    assert not re.search(r'output\s+"cloudsql_ip"\s*\{', text), \
        "cloudsql_ip output (public IP) must be deleted"
    assert "public_ip_address" not in text, \
        "no remaining reference to public_ip_address"


def test_cloudsql_mysql_private_ip_output_present():
    text = OUTPUTS_TF.read_text()
    body = _extract_block(text, r'output\s+"cloudsql_mysql_private_ip"')
    assert "google_sql_database_instance.voipbin.private_ip_address" in body
    assert "reconcile_outputs" in body  # description references consumer


# ---------------------------------------------------------------------------
# 7-8. FIELD_MAP additions
# ---------------------------------------------------------------------------

def test_field_map_includes_mysql_private_ip():
    matches = [m for m in FIELD_MAP if m.tf_key == "cloudsql_mysql_private_ip"]
    assert len(matches) == 1
    assert matches[0].cfg_key == "cloudsql_private_ip"
    assert matches[0].validator is _is_valid_ipv4_address


def test_field_map_includes_peering_cidr():
    matches = [m for m in FIELD_MAP if m.tf_key == "cloudsql_peering_range_cidr"]
    assert len(matches) == 1
    assert matches[0].cfg_key == "cloudsql_private_ip_cidr"
    assert matches[0].validator is _is_valid_ipv4_cidr


# ---------------------------------------------------------------------------
# 9-10. Validators
# ---------------------------------------------------------------------------

def test_ipv4_validator_rejects_sentinel_and_garbage():
    # Rejections
    assert _is_valid_ipv4_address("cloudsql-private.invalid") is False
    assert _is_valid_ipv4_address("") is False
    assert _is_valid_ipv4_address(None) is False
    assert _is_valid_ipv4_address("::1") is False  # IPv6
    assert _is_valid_ipv4_address(123) is False
    assert _is_valid_ipv4_address("10.0.0.0/24") is False  # CIDR, not addr
    # Acceptances
    assert _is_valid_ipv4_address("10.1.2.3") is True
    assert _is_valid_ipv4_address("192.168.1.1") is True


def test_ipv4_cidr_validator():
    # Acceptances
    assert _is_valid_ipv4_cidr("10.126.80.0/20") is True
    assert _is_valid_ipv4_cidr("10.0.0.0/8") is True
    # Rejections
    assert _is_valid_ipv4_cidr("10.126.80.0") is False  # no prefix
    assert _is_valid_ipv4_cidr("not-cidr") is False
    assert _is_valid_ipv4_cidr("::/0") is False  # IPv6
    assert _is_valid_ipv4_cidr(None) is False
    assert _is_valid_ipv4_cidr("") is False
    assert _is_valid_ipv4_cidr(42) is False


# ---------------------------------------------------------------------------
# 11. ansible_runner.py — dead output flattens removed
# ---------------------------------------------------------------------------

def test_ansible_runner_no_dead_outputs():
    text = ANSIBLE_RUNNER_PY.read_text()
    # The dead flatten keys must not appear at all in the runner module.
    assert "cloudsql_ip" not in text, \
        "ansible_runner.py must not reference cloudsql_ip"
    assert "cloudsql_connection_name" not in text, \
        "ansible_runner.py must not reference cloudsql_connection_name"


# ---------------------------------------------------------------------------
# 12. outputs() integration — FIELD_MAP plumbs the new fields end-to-end
# ---------------------------------------------------------------------------

def test_reconcile_outputs_populates_cloudsql_private_ip_and_cidr():
    store: dict[str, object] = {}

    cfg = MagicMock()
    cfg.get.side_effect = lambda k, default=None: store.get(k, default)

    def _set(k, v):
        store[k] = v

    cfg.set.side_effect = _set
    cfg.save.return_value = None

    tf_outputs = {
        "cloudsql_mysql_private_ip": "10.126.80.5",
        "cloudsql_peering_range_cidr": "10.126.80.0/20",
    }
    assert outputs(cfg, tf_outputs) is True
    assert store["cloudsql_private_ip"] == "10.126.80.5"
    assert store["cloudsql_private_ip_cidr"] == "10.126.80.0/20"
