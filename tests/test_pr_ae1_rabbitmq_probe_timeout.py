"""PR-AE-1: RabbitMQ liveness/readiness probe timeout regression tests.

Root cause: Kubernetes defaults timeoutSeconds=1 for exec probes.
On GKE, rabbitmq-diagnostics CLI occasionally exceeds 1s, causing
false-positive liveness kills (191 restarts observed in 14h).

Fix: explicit timeoutSeconds=10 (liveness) / timeoutSeconds=5 (readiness)
     failureThreshold=6 (liveness) / failureThreshold=3 (readiness)
"""

from __future__ import annotations

import copy
import pathlib
import textwrap

import pytest
import yaml

MANIFEST_PATH = (
    pathlib.Path(__file__).parent.parent
    / "k8s"
    / "infrastructure"
    / "rabbitmq"
    / "deployment.yaml"
)


def _load_deployment() -> dict:
    """Load the RabbitMQ Deployment document from the multi-doc YAML."""
    docs = list(yaml.safe_load_all(MANIFEST_PATH.read_text()))
    for doc in docs:
        if doc and doc.get("kind") == "Deployment":
            return doc
    raise ValueError("No Deployment found in rabbitmq/deployment.yaml")


def _get_container(deployment: dict) -> dict:
    containers = deployment["spec"]["template"]["spec"]["containers"]
    for c in containers:
        if c["name"] == "rabbitmq":
            return c
    raise ValueError("No 'rabbitmq' container found")


@pytest.fixture(scope="module")
def deployment():
    return _load_deployment()


@pytest.fixture(scope="module")
def container(deployment):
    return _get_container(deployment)


# ---------------------------------------------------------------------------
# Liveness probe
# ---------------------------------------------------------------------------

class TestLivenessProbe:
    def test_liveness_timeout_seconds(self, container):
        """timeoutSeconds must be exactly 10 — provides ~3-5x headroom over p99 CLI latency."""
        probe = container["livenessProbe"]
        assert probe["timeoutSeconds"] == 10, (
            f"livenessProbe.timeoutSeconds is {probe.get('timeoutSeconds', 'UNSET')}; "
            "expected 10. Default of 1s causes false-positive kills on GKE."
        )

    def test_liveness_failure_threshold(self, container):
        """failureThreshold must be exactly 6 — tolerates ~3 min of transient overload."""
        probe = container["livenessProbe"]
        assert probe["failureThreshold"] == 6, (
            f"livenessProbe.failureThreshold is {probe.get('failureThreshold', 'UNSET')}; "
            "expected 6."
        )

    def test_liveness_period_preserved(self, container):
        """periodSeconds must remain 30 — do not accidentally shorten the probe interval."""
        probe = container["livenessProbe"]
        assert probe["periodSeconds"] == 30, (
            f"livenessProbe.periodSeconds is {probe.get('periodSeconds', 'UNSET')}; "
            "expected 30."
        )

    def test_liveness_timeout_lt_period(self, container):
        """timeoutSeconds must be strictly less than periodSeconds to avoid probe overlap."""
        probe = container["livenessProbe"]
        t = probe["timeoutSeconds"]
        p = probe["periodSeconds"]
        assert t < p, (
            f"livenessProbe.timeoutSeconds ({t}) >= periodSeconds ({p}); "
            "probes would overlap, causing undefined behavior."
        )

    def test_liveness_initial_delay_preserved(self, container):
        """initialDelaySeconds must remain 30."""
        probe = container["livenessProbe"]
        assert probe["initialDelaySeconds"] == 30


# ---------------------------------------------------------------------------
# Readiness probe
# ---------------------------------------------------------------------------

class TestReadinessProbe:
    def test_readiness_timeout_seconds(self, container):
        """timeoutSeconds must be exactly 5."""
        probe = container["readinessProbe"]
        assert probe["timeoutSeconds"] == 5, (
            f"readinessProbe.timeoutSeconds is {probe.get('timeoutSeconds', 'UNSET')}; "
            "expected 5."
        )

    def test_readiness_failure_threshold(self, container):
        """failureThreshold must be exactly 3."""
        probe = container["readinessProbe"]
        assert probe["failureThreshold"] == 3, (
            f"readinessProbe.failureThreshold is {probe.get('failureThreshold', 'UNSET')}; "
            "expected 3."
        )

    def test_readiness_period_preserved(self, container):
        """periodSeconds must remain 10."""
        probe = container["readinessProbe"]
        assert probe["periodSeconds"] == 10, (
            f"readinessProbe.periodSeconds is {probe.get('periodSeconds', 'UNSET')}; "
            "expected 10."
        )

    def test_readiness_timeout_lt_period(self, container):
        """timeoutSeconds must be strictly less than periodSeconds."""
        probe = container["readinessProbe"]
        t = probe["timeoutSeconds"]
        p = probe["periodSeconds"]
        assert t < p, (
            f"readinessProbe.timeoutSeconds ({t}) >= periodSeconds ({p})."
        )

    def test_readiness_initial_delay_preserved(self, container):
        """initialDelaySeconds must remain 20."""
        probe = container["readinessProbe"]
        assert probe["initialDelaySeconds"] == 20


# ---------------------------------------------------------------------------
# Mutant harness
# ---------------------------------------------------------------------------

class TestMutantHarness:
    """Synthetic mutation tests — each mutant must cause at least one test to fail."""

    def _apply_mutant(self, mutant_fn) -> tuple[dict, dict]:
        dep = _load_deployment()
        c = _get_container(dep)
        mutant_fn(c)
        return dep, c

    def _run_probe_tests(self, container):
        """Return (failures: list[str])"""
        failures = []
        # liveness
        lp = container.get("livenessProbe", {})
        if lp.get("timeoutSeconds") != 10:
            failures.append("liveness timeoutSeconds")
        if lp.get("failureThreshold") != 6:
            failures.append("liveness failureThreshold")
        if lp.get("periodSeconds") != 30:
            failures.append("liveness periodSeconds")
        t = lp.get("timeoutSeconds", 0)
        p = lp.get("periodSeconds", 0)
        if t >= p:
            failures.append("liveness timeout_lt_period")
        # readiness
        rp = container.get("readinessProbe", {})
        if rp.get("timeoutSeconds") != 5:
            failures.append("readiness timeoutSeconds")
        if rp.get("failureThreshold") != 3:
            failures.append("readiness failureThreshold")
        if rp.get("periodSeconds") != 10:
            failures.append("readiness periodSeconds")
        t2 = rp.get("timeoutSeconds", 0)
        p2 = rp.get("periodSeconds", 0)
        if t2 >= p2:
            failures.append("readiness timeout_lt_period")
        return failures

    def test_mutant_liveness_timeout_default(self):
        """Mutant: timeoutSeconds removed (reverts to k8s default 1s) → must fail."""
        _, c = self._apply_mutant(lambda c: c["livenessProbe"].pop("timeoutSeconds"))
        failures = self._run_probe_tests(c)
        assert failures, "Mutant (liveness timeoutSeconds removed) passed all checks — regression!"

    def test_mutant_liveness_timeout_too_small(self):
        """Mutant: timeoutSeconds=1 (original broken value) → must fail."""
        def m(c):
            c["livenessProbe"]["timeoutSeconds"] = 1
        _, c = self._apply_mutant(m)
        failures = self._run_probe_tests(c)
        assert failures, "Mutant (liveness timeoutSeconds=1) passed all checks — regression!"

    def test_mutant_liveness_failure_threshold_removed(self):
        """Mutant: failureThreshold removed → must fail."""
        _, c = self._apply_mutant(lambda c: c["livenessProbe"].pop("failureThreshold"))
        failures = self._run_probe_tests(c)
        assert failures, "Mutant (liveness failureThreshold removed) passed all checks!"

    def test_mutant_liveness_failure_threshold_wrong(self):
        """Mutant: failureThreshold=1 → must fail."""
        def m(c):
            c["livenessProbe"]["failureThreshold"] = 1
        _, c = self._apply_mutant(m)
        failures = self._run_probe_tests(c)
        assert failures, "Mutant (liveness failureThreshold=1) passed all checks!"

    def test_mutant_readiness_timeout_default(self):
        """Mutant: readiness timeoutSeconds removed → must fail."""
        _, c = self._apply_mutant(lambda c: c["readinessProbe"].pop("timeoutSeconds"))
        failures = self._run_probe_tests(c)
        assert failures, "Mutant (readiness timeoutSeconds removed) passed all checks!"

    def test_mutant_readiness_timeout_too_small(self):
        """Mutant: readiness timeoutSeconds=1 → must fail."""
        def m(c):
            c["readinessProbe"]["timeoutSeconds"] = 1
        _, c = self._apply_mutant(m)
        failures = self._run_probe_tests(c)
        assert failures, "Mutant (readiness timeoutSeconds=1) passed all checks!"

    def test_mutant_readiness_failure_threshold_removed(self):
        """Mutant: readiness failureThreshold removed → must fail."""
        _, c = self._apply_mutant(lambda c: c["readinessProbe"].pop("failureThreshold"))
        failures = self._run_probe_tests(c)
        assert failures, "Mutant (readiness failureThreshold removed) passed all checks!"

    def test_mutant_readiness_failure_threshold_wrong(self):
        """Mutant: readiness failureThreshold=1 → must fail."""
        def m(c):
            c["readinessProbe"]["failureThreshold"] = 1
        _, c = self._apply_mutant(m)
        failures = self._run_probe_tests(c)
        assert failures, "Mutant (readiness failureThreshold=1) passed all checks!"

    def test_mutant_liveness_timeout_exceeds_period(self):
        """Mutant: timeoutSeconds=35 > periodSeconds=30 → timeout_lt_period must fail."""
        def m(c):
            c["livenessProbe"]["timeoutSeconds"] = 35
        _, c = self._apply_mutant(m)
        failures = self._run_probe_tests(c)
        assert failures, "Mutant (liveness timeoutSeconds=35 > period=30) passed all checks!"

    def test_mutant_readiness_timeout_exceeds_period(self):
        """Mutant: readiness timeoutSeconds=15 > periodSeconds=10 → timeout_lt_period must fail."""
        def m(c):
            c["readinessProbe"]["timeoutSeconds"] = 15
        _, c = self._apply_mutant(m)
        failures = self._run_probe_tests(c)
        assert failures, "Mutant (readiness timeoutSeconds=15 > period=10) passed all checks!"
