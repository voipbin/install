"""PR-U-1 invariant tests. Homer/heplify-server k8s manifests.

Design doc. docs/plans/2026-05-13-pr-u-1-homer-k8s-manifests-design.md

The HOMER stack (heplify-server + homer-webapp) is deployed as a single
Deployment with two containers in the `infrastructure` namespace. Two
internal LoadBalancer Services expose it:

  - heplify-tcp: ports 9060/TCP, 9061/TCP, 9090/TCP, 9096/TCP, 80/TCP
  - heplify-udp: port 9060/UDP

Selector matches Deployment Pod labels (`app: heplify-server`). LB
annotation `cloud.google.com/load-balancer-type: "Internal"` keeps the
LB off the public internet (lockable decision 2026-05-13).

These invariants must hold so that PR-R's harvest_loadbalancer_ips()
sees the heplify-udp Service externalIP, PR-T's ansible_runner flat-vars
include heplify_lb_ip, and PR-U-3 can wire `HOMER_URI=udp:{ip}:9060`
into Kamailio's env.j2.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
HOMER_DIR = REPO_ROOT / "k8s" / "infrastructure" / "homer"
INFRA_KUSTOMIZATION = REPO_ROOT / "k8s" / "infrastructure" / "kustomization.yaml"


def _load_yaml(path: Path):
    with path.open() as f:
        return yaml.safe_load(f)


def _load_yaml_all(path: Path):
    with path.open() as f:
        return list(yaml.safe_load_all(f))


# ---------------------------------------------------------------------------
# TestHomerKustomizationStructure
# ---------------------------------------------------------------------------

class TestHomerKustomizationStructure:
    """The homer Kustomize module is correctly shaped."""

    def test_kustomization_file_exists(self):
        assert (HOMER_DIR / "kustomization.yaml").is_file(), (
            f"PR-U-1 missing {HOMER_DIR / 'kustomization.yaml'}"
        )

    def test_resources_list_deployment_and_service(self):
        kust = _load_yaml(HOMER_DIR / "kustomization.yaml")
        assert set(kust["resources"]) == {"deployment.yaml", "service.yaml"}, (
            f"Unexpected resources in homer kustomization.yaml: {kust['resources']}"
        )

    def test_no_kustomize_namespace_directive(self):
        """Mirrors the sibling pattern (redis/rabbitmq/clickhouse): per-resource
        `metadata.namespace` only, no kustomize-level `namespace:` field."""
        kust = _load_yaml(HOMER_DIR / "kustomization.yaml")
        assert "namespace" not in kust, (
            "PR-U-1 design §6.3 forbids a kustomize-level `namespace:` field. "
            "Use per-resource metadata.namespace only."
        )

    def test_images_pinned_to_canonical_tags(self):
        kust = _load_yaml(HOMER_DIR / "kustomization.yaml")
        images = {img["name"]: img for img in kust.get("images", [])}
        assert "heplify-server" in images
        assert images["heplify-server"]["newName"] == "sipcapture/heplify-server"
        assert str(images["heplify-server"]["newTag"]) == "1.30"
        assert "homer-webapp" in images
        assert images["homer-webapp"]["newName"] == "pchero/homer-app"
        assert str(images["homer-webapp"]["newTag"]) == "0.0.4"


# ---------------------------------------------------------------------------
# TestHomerRegisteredInInfrastructureKustomization
# ---------------------------------------------------------------------------

class TestHomerRegisteredInInfrastructureKustomization:
    """Without registration in the parent kustomization.yaml the entire
    homer module is silently skipped at `kubectl apply -k` time."""

    def test_homer_in_infrastructure_resources(self):
        kust = _load_yaml(INFRA_KUSTOMIZATION)
        assert "homer" in kust["resources"], (
            f"PR-U-1 regression: `homer` not in {INFRA_KUSTOMIZATION} "
            f"resources list: {kust['resources']}"
        )


# ---------------------------------------------------------------------------
# TestHomerNamespaceIsInfrastructure
# ---------------------------------------------------------------------------

class TestHomerNamespaceIsInfrastructure:
    """All resources live in the `infrastructure` namespace (shared with
    redis/rabbitmq/clickhouse)."""

    def test_deployment_namespace(self):
        dep = _load_yaml(HOMER_DIR / "deployment.yaml")
        assert dep["metadata"]["namespace"] == "infrastructure"

    def test_both_services_namespace(self):
        services = _load_yaml_all(HOMER_DIR / "service.yaml")
        assert len(services) == 2, (
            f"Expected exactly 2 Services in service.yaml, got {len(services)}"
        )
        for svc in services:
            assert svc["metadata"]["namespace"] == "infrastructure", (
                f"Service {svc['metadata']['name']} has wrong namespace: "
                f"{svc['metadata']['namespace']}"
            )


# ---------------------------------------------------------------------------
# TestHeplifyServicesInternalLB
# ---------------------------------------------------------------------------

class TestHeplifyServicesInternalLB:
    """Both Services are LoadBalancer type with the GCP internal-LB
    annotation; without the annotation GCP would allocate a PUBLIC IP
    which violates locked decision 2026-05-13 #4 (no external exposure)."""

    def _services_by_name(self) -> dict:
        services = _load_yaml_all(HOMER_DIR / "service.yaml")
        return {s["metadata"]["name"]: s for s in services}

    def test_heplify_tcp_internal_lb_annotation(self):
        svcs = self._services_by_name()
        ann = svcs["heplify-tcp"]["metadata"].get("annotations", {})
        assert (
            ann.get("cloud.google.com/load-balancer-type") == "Internal"
        ), (
            "PR-U-1 design §6.2: heplify-tcp must be Internal LB. "
            f"Got annotations: {ann}"
        )

    def test_heplify_udp_internal_lb_annotation(self):
        svcs = self._services_by_name()
        ann = svcs["heplify-udp"]["metadata"].get("annotations", {})
        assert (
            ann.get("cloud.google.com/load-balancer-type") == "Internal"
        ), (
            "PR-U-1 design §6.2: heplify-udp must be Internal LB. "
            f"Got annotations: {ann}"
        )

    def test_heplify_tcp_port_set(self):
        svcs = self._services_by_name()
        ports = svcs["heplify-tcp"]["spec"]["ports"]
        # (port, protocol) tuples
        port_set = {(p["port"], p.get("protocol", "TCP")) for p in ports}
        assert port_set == {
            (9060, "TCP"),
            (9061, "TCP"),
            (9090, "TCP"),
            (9096, "TCP"),
            (80, "TCP"),
        }, f"heplify-tcp port set drift: {port_set}"

    def test_heplify_udp_port_set(self):
        svcs = self._services_by_name()
        ports = svcs["heplify-udp"]["spec"]["ports"]
        port_set = {(p["port"], p.get("protocol", "TCP")) for p in ports}
        # PR-T1 + PR-U-1 convention: the -udp Service exposes ONLY UDP/9060.
        # TCP/9060 goes on heplify-tcp (shared with homer-webapp + config/TLS).
        assert port_set == {(9060, "UDP")}, (
            f"heplify-udp port set drift: {port_set}"
        )


# ---------------------------------------------------------------------------
# TestHeplifyPodSelectorMatchesDeploymentLabels
# ---------------------------------------------------------------------------

class TestHeplifyPodSelectorMatchesDeploymentLabels:
    """Service selectors must intersect Deployment Pod template labels.
    A drift here means LB has no endpoints and Pods are silently unreachable."""

    def _pod_labels(self) -> dict:
        dep = _load_yaml(HOMER_DIR / "deployment.yaml")
        return dep["spec"]["template"]["metadata"]["labels"]

    def test_heplify_tcp_selector_matches_pod_labels(self):
        services = _load_yaml_all(HOMER_DIR / "service.yaml")
        svc = next(s for s in services if s["metadata"]["name"] == "heplify-tcp")
        pod_labels = self._pod_labels()
        for k, v in svc["spec"]["selector"].items():
            assert pod_labels.get(k) == v, (
                f"heplify-tcp selector {k}={v} does not match Pod label "
                f"{k}={pod_labels.get(k)}"
            )

    def test_heplify_udp_selector_matches_pod_labels(self):
        services = _load_yaml_all(HOMER_DIR / "service.yaml")
        svc = next(s for s in services if s["metadata"]["name"] == "heplify-udp")
        pod_labels = self._pod_labels()
        for k, v in svc["spec"]["selector"].items():
            assert pod_labels.get(k) == v, (
                f"heplify-udp selector {k}={v} does not match Pod label "
                f"{k}={pod_labels.get(k)}"
            )


# ---------------------------------------------------------------------------
# TestKustomizeBuildCompiles
# ---------------------------------------------------------------------------

class TestKustomizeBuildCompiles:
    """`kubectl kustomize` over the homer module must succeed. Falls back
    to PyYAML parse-only when kubectl is not on PATH (CI image always has
    it; the parse fallback covers developer laptops without kubectl)."""

    def test_kustomize_build_or_yaml_parse(self):
        kubectl = shutil.which("kubectl")
        if kubectl is None:
            # Fallback. Each YAML file must parse cleanly.
            _load_yaml(HOMER_DIR / "deployment.yaml")
            _load_yaml_all(HOMER_DIR / "service.yaml")
            _load_yaml(HOMER_DIR / "kustomization.yaml")
            pytest.skip("kubectl not on PATH; YAML parse fallback succeeded")
        result = subprocess.run(
            [kubectl, "kustomize", str(HOMER_DIR)],
            capture_output=True, text=True, timeout=30,
        )
        assert result.returncode == 0, (
            f"`kubectl kustomize {HOMER_DIR}` failed:\n"
            f"stdout: {result.stdout}\n"
            f"stderr: {result.stderr}"
        )


# ---------------------------------------------------------------------------
# TestPlaceholderTokensPresent
# ---------------------------------------------------------------------------

class TestPlaceholderTokensPresent:
    """Sentinel tokens that PR-U-1 added to deployment.yaml so the
    substitution map fills them at apply time."""

    def test_expected_placeholder_tokens(self):
        text = (HOMER_DIR / "deployment.yaml").read_text()
        # Count only lines that USE the token as a value (not lines that
        # mention it in a comment). Comments contain `#` which yaml `value:`
        # lines do not begin with after the colon.
        def _count_value_lines(token: str) -> int:
            return sum(
                1 for line in text.splitlines()
                if token in line and "value:" in line
            )

        # PR-U-1 reused PLACEHOLDER_CLOUDSQL_POSTGRES_PRIVATE_IP from PR-D1
        # rather than inventing PLACEHOLDER_HOMER_DB_ADDR (iter-1 finding #5).
        assert _count_value_lines("PLACEHOLDER_CLOUDSQL_POSTGRES_PRIVATE_IP") == 2, (
            "Expected 2 value-line occurrences of PLACEHOLDER_CLOUDSQL_POSTGRES_PRIVATE_IP "
            "(heplify DBADDR + homer-webapp DB_HOST)"
        )
        assert _count_value_lines("PLACEHOLDER_HOMER_DB_USER") == 2, (
            "Expected 2 value-line occurrences of PLACEHOLDER_HOMER_DB_USER "
            "(heplify DBUSER + homer-webapp DB_USER)"
        )
        assert _count_value_lines("PLACEHOLDER_HOMER_DB_PASS") == 2, (
            "Expected 2 value-line occurrences of PLACEHOLDER_HOMER_DB_PASS "
            "(heplify DBPASS + homer-webapp DB_PASS)"
        )

    def test_dbshema_typo_intentional_comment_present(self):
        """The misspelled `HEPLIFYSERVER_DBSHEMA` is the upstream binary's
        actual env name. Keep an explicit inline comment so a future
        contributor doesn't "fix" it."""
        text = (HOMER_DIR / "deployment.yaml").read_text()
        assert "HEPLIFYSERVER_DBSHEMA" in text, (
            "HEPLIFYSERVER_DBSHEMA env var is required for heplify-server. "
            "Do NOT 'fix' the typo to DBSCHEMA — the upstream binary literally "
            "reads the misspelled env var name."
        )
        assert "HEPLIFYSERVER_DBSCHEMA" not in text, (
            "Found HEPLIFYSERVER_DBSCHEMA (the 'corrected' spelling) in the "
            "manifest. This is wrong — the upstream binary reads DBSHEMA, "
            "not DBSCHEMA. See deployment.yaml inline comment + iter-1 #9."
        )
        assert "INTENTIONAL" in text, (
            "Expected an inline comment marking HEPLIFYSERVER_DBSHEMA as "
            "intentional. iter-1 finding #9."
        )
