"""PR-AF-1: RabbitMQ .erlang.cookie permission fix via fsGroupChangePolicy.

Root cause: fsGroup=999 with default fsGroupChangePolicy=Always triggers
recursive chown/chmod on PVC mount, adding group bits to .erlang.cookie.
RabbitMQ requires 0400 (owner-only).

Fix: fsGroupChangePolicy: OnRootMismatch — skips recursive chmod when root
dir ownership already matches, preserving .erlang.cookie permissions.
"""

from __future__ import annotations

import pathlib

import pytest
import yaml

MANIFEST_PATH = (
    pathlib.Path(__file__).parent.parent
    / "k8s" / "infrastructure" / "rabbitmq" / "deployment.yaml"
)


def _load_pod_security_context() -> dict:
    docs = list(yaml.safe_load_all(MANIFEST_PATH.read_text()))
    for doc in docs:
        if doc and doc.get("kind") == "Deployment":
            return doc["spec"]["template"]["spec"]["securityContext"]
    raise ValueError("No Deployment found")


@pytest.fixture(scope="module")
def sc():
    return _load_pod_security_context()


class TestFsGroupChangePolicy:
    def test_fsgroupchangepolicy_set(self, sc):
        """fsGroupChangePolicy must be OnRootMismatch to preserve .erlang.cookie 0400."""
        assert sc.get("fsGroupChangePolicy") == "OnRootMismatch", (
            f"fsGroupChangePolicy is {sc.get('fsGroupChangePolicy', 'UNSET')}; "
            "expected 'OnRootMismatch'. Default 'Always' corrupts .erlang.cookie permissions."
        )

    def test_fsgroup_preserved(self, sc):
        """fsGroup must remain 999 — required for PVC write access."""
        assert sc.get("fsGroup") == 999

    def test_runasuser_preserved(self, sc):
        """runAsUser must remain 999."""
        assert sc.get("runAsUser") == 999

    def test_runasnonroot_preserved(self, sc):
        """runAsNonRoot must remain True."""
        assert sc.get("runAsNonRoot") is True

    def test_seccompprofile_preserved(self, sc):
        """seccompProfile.type must remain RuntimeDefault."""
        assert sc.get("seccompProfile", {}).get("type") == "RuntimeDefault"


class TestMutantHarness:
    def _check(self, sc: dict) -> list[str]:
        failures = []
        if sc.get("fsGroupChangePolicy") != "OnRootMismatch":
            failures.append(f"fsGroupChangePolicy={sc.get('fsGroupChangePolicy')!r}")
        if sc.get("fsGroup") != 999:
            failures.append("fsGroup")
        if sc.get("runAsUser") != 999:
            failures.append("runAsUser")
        if sc.get("runAsNonRoot") is not True:
            failures.append("runAsNonRoot")
        return failures

    def test_mutant_field_removed(self):
        """Mutant: fsGroupChangePolicy removed → must fail."""
        sc = _load_pod_security_context().copy()
        sc.pop("fsGroupChangePolicy", None)
        assert self._check(sc), "Mutant (field removed) passed — regression!"

    def test_mutant_set_to_always(self):
        """Mutant: fsGroupChangePolicy=Always (broken default) → must fail."""
        sc = _load_pod_security_context().copy()
        sc["fsGroupChangePolicy"] = "Always"
        assert self._check(sc), "Mutant (Always) passed — regression!"

    def test_mutant_typo_value(self):
        """Mutant: typo in value → must fail."""
        sc = _load_pod_security_context().copy()
        sc["fsGroupChangePolicy"] = "onRootMismatch"  # wrong case
        assert self._check(sc), "Mutant (typo) passed — regression!"

    def test_mutant_fsgroup_removed(self):
        """Mutant: fsGroup removed → must fail (PVC write broken)."""
        sc = _load_pod_security_context().copy()
        sc.pop("fsGroup", None)
        assert self._check(sc), "Mutant (fsGroup removed) passed!"
