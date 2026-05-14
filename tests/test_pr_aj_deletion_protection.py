"""PR-AJ: GKE cluster deletion_protection regression tests.

Ensures deletion_protection=false and lifecycle.ignore_changes are present
so that voipbin-install destroy works without manual intervention, while
production operators can safely set true via GCP Console without terraform
reverting it on subsequent applies.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
GKE_TF = REPO_ROOT / "terraform" / "gke.tf"


def _read(path: Path) -> str:
    return path.read_text()


def _gke_cluster_block(text: str) -> str:
    """Extract the google_container_cluster.voipbin resource block."""
    match = re.search(
        r'resource\s+"google_container_cluster"\s+"voipbin"\s*\{',
        text,
    )
    assert match, "google_container_cluster.voipbin resource not found in gke.tf"
    start = match.start()
    depth = 0
    for i, ch in enumerate(text[start:], start):
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start : i + 1]
    raise ValueError("Unterminated google_container_cluster.voipbin block")


class TestGKEClusterDeletionProtection:
    """3 cases — PR-AJ: deletion_protection=false + lifecycle ignore on GKE cluster."""

    def test_deletion_protection_false(self):
        block = _gke_cluster_block(_read(GKE_TF))
        assert re.search(r'deletion_protection\s*=\s*false', block), (
            "GKE cluster must have deletion_protection=false so that "
            "voipbin-install destroy can remove it without manual intervention."
        )

    def test_lifecycle_ignore_deletion_protection(self):
        block = _gke_cluster_block(_read(GKE_TF))
        assert re.search(
            r'lifecycle\s*\{[^}]*ignore_changes\s*=\s*\[deletion_protection\]',
            block,
            re.DOTALL,
        ), (
            "GKE cluster must have lifecycle { ignore_changes = [deletion_protection] } "
            "so production operators can set true via GCP Console without terraform reverting it."
        )

    def test_deletion_protection_not_true(self):
        block = _gke_cluster_block(_read(GKE_TF))
        assert not re.search(r'deletion_protection\s*=\s*true', block), (
            "GKE cluster must NOT have deletion_protection=true hardcoded — "
            "that blocks voipbin-install destroy."
        )
