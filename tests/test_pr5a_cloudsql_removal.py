"""PR #5a manifest invariants — cloudsql-proxy removal.

Five assertions (per design §6.1):

1. No Deployment/Service/ServiceAccount named ``cloudsql-proxy``.
2. No literal substring ``cloudsql-proxy`` anywhere in rendered output.
3. Rendered ``DATABASE_DSN_BIN`` contains the operator-supplied IP and
   no ``PLACEHOLDER_*`` remnants.
4. NetworkPolicies in ns ``bin-manager`` and ``voip`` carry a Cloud SQL
   CIDR egress rule with the substituted CIDR; rag-manager carries an
   additional rule for port 5432.
5. ``check_cloudsql_private_ip`` raises :class:`PreflightError` when the
   sentinel ``cloudsql-private.invalid`` value is supplied.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
K8S_DIR = REPO_ROOT / "k8s"

# The CIDR / IP we substitute into the rendered manifests for tests.
TEST_PRIVATE_IP = "10.42.0.7"
TEST_PRIVATE_IP_CIDR = "10.42.0.7/32"


def _have_kubectl() -> bool:
    return shutil.which("kubectl") is not None


def _walk_strings(obj):
    """Yield every leaf string in a nested dict/list structure."""
    if isinstance(obj, str):
        yield obj
    elif isinstance(obj, dict):
        for v in obj.values():
            yield from _walk_strings(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _walk_strings(v)


@pytest.fixture(scope="module")
def rendered_yaml() -> str:
    """Render manifests with a non-sentinel Cloud SQL private IP substituted."""
    if not _have_kubectl():
        pytest.skip("kubectl not available in test environment")
    proc = subprocess.run(
        ["kubectl", "kustomize", str(K8S_DIR)],
        check=True,
        capture_output=True,
        text=True,
        timeout=60,
    )
    rendered = proc.stdout
    # Apply the two placeholders this PR introduces.
    rendered = rendered.replace(
        "PLACEHOLDER_CLOUDSQL_PRIVATE_IP_CIDR", TEST_PRIVATE_IP_CIDR
    )
    rendered = rendered.replace(
        "PLACEHOLDER_CLOUDSQL_PRIVATE_IP", TEST_PRIVATE_IP
    )
    return rendered


@pytest.fixture(scope="module")
def rendered_docs(rendered_yaml) -> list[dict]:
    return [d for d in yaml.safe_load_all(rendered_yaml) if d]


def test_no_cloudsql_proxy_resources(rendered_docs):
    """No Deployment, Service, or ServiceAccount named cloudsql-proxy."""
    offenders: list[str] = []
    for doc in rendered_docs:
        if doc.get("kind") not in {"Deployment", "Service", "ServiceAccount"}:
            continue
        if doc.get("metadata", {}).get("name") == "cloudsql-proxy":
            offenders.append(
                f"{doc['kind']}/{doc['metadata']['name']} "
                f"in ns {doc['metadata'].get('namespace', '?')}"
            )
    assert offenders == [], (
        f"cloudsql-proxy resources must not be rendered: {offenders}"
    )


def test_no_cloudsql_proxy_string_in_rendered_manifests(rendered_docs):
    """No literal 'cloudsql-proxy' substring anywhere in rendered output."""
    hits: list[str] = []
    for doc in rendered_docs:
        for s in _walk_strings(doc):
            if "cloudsql-proxy" in s:
                kind = doc.get("kind", "?")
                name = doc.get("metadata", {}).get("name", "?")
                hits.append(f"{kind}/{name}: {s[:80]}")
    assert hits == [], f"'cloudsql-proxy' string leaked into manifests: {hits[:5]}"


def test_dsn_secret_uses_private_ip_placeholder(rendered_docs):
    """Rendered DATABASE_DSN_BIN contains the operator-supplied IP."""
    secret = next(
        d
        for d in rendered_docs
        if d.get("kind") == "Secret"
        and d.get("metadata", {}).get("name") == "voipbin"
        and d.get("metadata", {}).get("namespace") == "bin-manager"
    )
    dsn = secret["stringData"]["DATABASE_DSN_BIN"]
    assert TEST_PRIVATE_IP in dsn, f"expected {TEST_PRIVATE_IP} in DSN, got {dsn}"
    assert "PLACEHOLDER_" not in dsn, f"PLACEHOLDER remnant in DSN: {dsn}"
    assert "cloudsql-proxy" not in dsn


def test_network_policy_allows_cloudsql_cidr_egress(rendered_docs):
    """NetworkPolicies expose Cloud SQL CIDR egress on the right ports.

    - bin-manager: broad rule on port 3306 + narrow rule for rag-manager
      on port 5432.
    - voip: narrow rule for asterisk-registrar on port 3306.
    """
    nps = [d for d in rendered_docs if d.get("kind") == "NetworkPolicy"]

    def _egress_to_cidr(np: dict, cidr: str) -> list[dict]:
        out: list[dict] = []
        for rule in (np.get("spec", {}) or {}).get("egress", []) or []:
            for peer in rule.get("to", []) or []:
                if (peer.get("ipBlock", {}) or {}).get("cidr") == cidr:
                    out.append(rule)
        return out

    # bin-manager broad MySQL rule (port 3306).
    bm_broad = next(
        np for np in nps
        if np["metadata"]["namespace"] == "bin-manager"
        and np["metadata"]["name"] == "allow-to-cloudsql-private-ip"
    )
    rules = _egress_to_cidr(bm_broad, TEST_PRIVATE_IP_CIDR)
    assert len(rules) == 1
    ports = {(p["protocol"], p["port"]) for p in rules[0]["ports"]}
    assert ("TCP", 3306) in ports
    # Defense-in-depth: broad rule must NOT contain 5432.
    assert ("TCP", 5432) not in ports

    # bin-manager narrow Postgres rule for rag-manager.
    bm_pg = next(
        np for np in nps
        if np["metadata"]["namespace"] == "bin-manager"
        and np["metadata"]["name"] == "allow-rag-manager-to-cloudsql-postgres"
    )
    assert (bm_pg["spec"]["podSelector"]["matchLabels"]
            == {"app": "rag-manager"})
    rules = _egress_to_cidr(bm_pg, TEST_PRIVATE_IP_CIDR)
    assert len(rules) == 1
    ports = {(p["protocol"], p["port"]) for p in rules[0]["ports"]}
    assert ports == {("TCP", 5432)}

    # voip narrow rule for asterisk-registrar.
    voip_np = next(
        np for np in nps
        if np["metadata"]["namespace"] == "voip"
        and np["metadata"]["name"] == "allow-asterisk-registrar-to-cloudsql"
    )
    assert (voip_np["spec"]["podSelector"]["matchLabels"]
            == {"app": "asterisk-registrar"})
    rules = _egress_to_cidr(voip_np, TEST_PRIVATE_IP_CIDR)
    assert len(rules) == 1
    ports = {(p["protocol"], p["port"]) for p in rules[0]["ports"]}
    assert ports == {("TCP", 3306)}


def test_preflight_rejects_sentinel_cloudsql_ip():
    """Sentinel value triggers PreflightError naming the field."""
    from scripts.preflight import (
        CLOUDSQL_PRIVATE_IP_SENTINEL,
        PreflightError,
        check_cloudsql_private_ip,
    )

    class FakeConfig:
        def __init__(self, data):
            self._data = data

        def get(self, key, default=None):
            return self._data.get(key, default)

    # Sentinel
    with pytest.raises(PreflightError) as exc:
        check_cloudsql_private_ip(FakeConfig(
            {"cloudsql_private_ip": CLOUDSQL_PRIVATE_IP_SENTINEL}
        ))
    assert "cloudsql_private_ip" in str(exc.value)

    # Empty string also rejected.
    with pytest.raises(PreflightError):
        check_cloudsql_private_ip(FakeConfig({"cloudsql_private_ip": ""}))

    # A real-looking IP passes.
    check_cloudsql_private_ip(FakeConfig({"cloudsql_private_ip": "10.42.0.7"}))
