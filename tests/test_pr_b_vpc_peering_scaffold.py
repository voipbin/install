"""PR-B tests — VPC peering scaffold + reconcile registry additions.

Covers design §2.6:
1. network.tf has google_compute_global_address.cloudsql_peering with VPC_PEERING
2. network.tf has google_service_networking_connection.voipbin with ABANDON
3. variables.tf declares cloudsql_peering_prefix_length (default 20 + validation)
4. outputs.tf declares cloudsql_peering_range_cidr
5. build_registry includes the global address entry
6. build_registry does NOT include google_service_networking_connection.voipbin
7. build_registry includes all 5 external_service for_each static IPs
"""

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.config import InstallerConfig  # noqa: E402
from scripts.terraform_reconcile import build_registry  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
NETWORK_TF = REPO_ROOT / "terraform" / "network.tf"
VARIABLES_TF = REPO_ROOT / "terraform" / "variables.tf"
OUTPUTS_TF = REPO_ROOT / "terraform" / "outputs.tf"


def _extract_block(text: str, header_re: str) -> str:
    """Return the body of the first top-level `{...}` block following header_re.

    Brace-balanced so HCL interpolations like `${var.foo}` inside the body
    don't terminate the match early.

    Limitation: does NOT handle braces inside string literals or heredocs.
    Adequate for current PR-B targets (no heredoc/quoted-brace usage in the
    targeted HCL blocks).
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


def _make_config():
    cfg = InstallerConfig()
    cfg.set_many({
        "gcp_project_id": "my-project",
        "region": "us-central1",
        "zone": "us-central1-a",
        "kamailio_count": 1,
        "rtpengine_count": 1,
    })
    return cfg


def test_network_tf_has_global_address():
    text = NETWORK_TF.read_text()
    body = _extract_block(
        text,
        r'resource\s+"google_compute_global_address"\s+"cloudsql_peering"',
    )
    assert 'purpose' in body and '"VPC_PEERING"' in body
    assert 'address_type' in body and '"INTERNAL"' in body
    assert 'prefix_length' in body
    assert 'var.cloudsql_peering_prefix_length' in body


def test_network_tf_has_service_networking_connection():
    text = NETWORK_TF.read_text()
    body = _extract_block(
        text,
        r'resource\s+"google_service_networking_connection"\s+"voipbin"',
    )
    assert 'servicenetworking.googleapis.com' in body
    assert 'deletion_policy' in body and '"ABANDON"' in body
    assert 'google_compute_global_address.cloudsql_peering' in body


def test_peering_prefix_variable():
    text = VARIABLES_TF.read_text()
    body = _extract_block(text, r'variable\s+"cloudsql_peering_prefix_length"')
    assert re.search(r'type\s*=\s*number', body)
    assert re.search(r'default\s*=\s*20', body)
    assert 'validation' in body
    assert '>= 8' in body and '<= 29' in body


def test_peering_range_cidr_output():
    text = OUTPUTS_TF.read_text()
    body = _extract_block(text, r'output\s+"cloudsql_peering_range_cidr"')
    assert 'google_compute_global_address.cloudsql_peering' in body
    assert 'address' in body and 'prefix_length' in body


def test_registry_includes_peering_global_address():
    cfg = _make_config()
    entries = build_registry(cfg)
    matches = [e for e in entries if e["tf_address"] == "google_compute_global_address.cloudsql_peering"]
    assert len(matches) == 1, "Expected exactly one peering global_address entry"
    entry = matches[0]
    assert "--global" in entry["gcloud_check"]
    assert "voipbin-cloudsql-peering" in entry["gcloud_check"]
    assert entry["import_id"] == "projects/my-project/global/addresses/voipbin-cloudsql-peering"


def test_registry_excludes_service_networking_connection():
    cfg = _make_config()
    addresses = {e["tf_address"] for e in build_registry(cfg)}
    # Intentional exclusion — see scripts/terraform_reconcile.py rationale
    # and design §2.4 / §4.
    assert "google_service_networking_connection.voipbin" not in addresses


def test_registry_includes_static_ips():
    cfg = _make_config()
    entries = build_registry(cfg)
    addresses = {e["tf_address"] for e in entries}
    for key in ("api-manager", "hook-manager", "admin", "talk", "meet"):
        addr = f'google_compute_address.external_service["{key}"]'
        assert addr in addresses, f"Missing registry entry for {addr}"
        entry = next(e for e in entries if e["tf_address"] == addr)
        assert f"{key}-static-ip" in entry["gcloud_check"]
        assert entry["import_id"] == f"projects/my-project/regions/us-central1/addresses/{key}-static-ip"
