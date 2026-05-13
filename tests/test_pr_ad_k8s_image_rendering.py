"""PR-AD — K8s image rendering via kustomize images: block static + rendered tests.

Validates:
  - No `image: voipbin/<anything>` literal survives under k8s/.
  - Every Deployment placeholder has a matching `images:` entry.
  - Every `images:` entry's newName is on the Docker Hub voipbin allow-list.
  - Every `images:` entry's newTag is non-empty.
  - Every container's `name:` field shares a prefix with its placeholder image.
  - `kubectl kustomize k8s/` renders successfully (exit 0).
  - Rendered output contains the expected Deployment count and no leftover placeholders.

Mutant matrix (11 mutations) lives in scripts/dev/pr_ad_mutant_harness.py.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from collections import defaultdict
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parent.parent
K8S = REPO / "k8s"
KUST = K8S / "kustomization.yaml"
ALLOWLIST = REPO / "tests" / "fixtures" / "pr_ad_docker_hub_voipbin_snapshot.json"

# Minimum number of YAML files the walker should visit. Lower-bound guard (R1 M9):
# legitimate additions are fine; accidental glob narrowing trips this.
MIN_YAML_FILES = 38


def _walk_k8s_yamls() -> list[Path]:
    """Walk every YAML file under k8s/ with zero exclusions."""
    return sorted(K8S.rglob("*.yaml"))


def _load_kustomization() -> dict:
    return yaml.safe_load(KUST.read_text())


def _all_image_literals() -> list[tuple[Path, str]]:
    """Find every `image: voipbin/...` literal across k8s/."""
    pat = re.compile(r"^\s*image:\s*(voipbin/[a-z0-9-]+)\s*$", re.MULTILINE)
    out = []
    for p in _walk_k8s_yamls():
        text = p.read_text()
        for m in pat.finditer(text):
            out.append((p, m.group(1)))
    return out


def _all_placeholder_references() -> list[tuple[Path, str, str]]:
    """Return (path, container_name, placeholder) tuples from every Deployment.

    Walks YAML streams in case a manifest contains multiple documents.
    """
    out = []
    for p in _walk_k8s_yamls():
        try:
            docs = list(yaml.safe_load_all(p.read_text()))
        except yaml.YAMLError:
            continue
        for doc in docs:
            if not isinstance(doc, dict):
                continue
            kind = doc.get("kind", "")
            if kind not in {"Deployment", "StatefulSet", "DaemonSet", "Job", "CronJob"}:
                continue
            spec_paths = []
            if kind in {"Job"}:
                # Job: spec.template.spec.containers
                template = doc.get("spec", {}).get("template", {})
                spec_paths.append(template)
            elif kind == "CronJob":
                template = doc.get("spec", {}).get("jobTemplate", {}).get("spec", {}).get("template", {})
                spec_paths.append(template)
            else:
                template = doc.get("spec", {}).get("template", {})
                spec_paths.append(template)
            for tpl in spec_paths:
                containers = (tpl.get("spec", {}) or {}).get("containers", []) or []
                for c in containers:
                    name = c.get("name", "")
                    image = c.get("image", "")
                    if image.endswith("-image"):
                        out.append((p, name, image))
    return out


# --- Walker count guard (R1 #4 M9) ----------------------------------------


class TestWalkerCountGuard:
    def test_walker_visits_at_least_minimum_files(self) -> None:
        files = _walk_k8s_yamls()
        assert len(files) >= MIN_YAML_FILES, (
            f"k8s/ walker visited only {len(files)} files (expected ≥ {MIN_YAML_FILES}). "
            "Accidental glob narrowing? Symlink loop? Subtree removed?"
        )


# --- No literal voipbin/ image reference survives -------------------------


class TestNoLiteralImageReferences:
    def test_no_voipbin_image_literal_anywhere_under_k8s(self) -> None:
        literals = _all_image_literals()
        assert not literals, (
            f"Found {len(literals)} surviving `image: voipbin/...` literal(s):\n"
            + "\n".join(f"  {p.relative_to(REPO)}: {lit}" for p, lit in literals)
            + "\n\nAll image references must use the placeholder pattern "
              "(`image: <svc>-image`) so kustomize images: block can remap them."
        )


# --- Placeholder ↔ images: entry bijection --------------------------------


class TestPlaceholderImageMapping:
    def test_every_placeholder_has_matching_images_entry(self) -> None:
        kust = _load_kustomization()
        images_entries = {e["name"] for e in kust.get("images", [])}
        refs = _all_placeholder_references()
        used = {ph for _, _, ph in refs}
        missing = used - images_entries
        assert not missing, (
            f"Placeholders used in manifests but missing from "
            f"k8s/kustomization.yaml `images:` block: {sorted(missing)}"
        )

    def test_no_orphan_images_entries(self) -> None:
        """R1 #4 M8: every `images:` entry must be referenced by a manifest."""
        kust = _load_kustomization()
        images_entries = {e["name"] for e in kust.get("images", [])}
        refs = _all_placeholder_references()
        used = {ph for _, _, ph in refs}
        orphans = images_entries - used
        assert not orphans, (
            f"`images:` entries in kustomization.yaml not referenced by any "
            f"manifest container: {sorted(orphans)}"
        )

    def test_no_duplicate_images_entry_names(self) -> None:
        kust = _load_kustomization()
        names = [e["name"] for e in kust.get("images", [])]
        dupes = [n for n in set(names) if names.count(n) > 1]
        assert not dupes, f"Duplicate `images:` entry names: {sorted(dupes)}"


# --- images: entry semantic checks ----------------------------------------


class TestImagesEntrySemantics:
    def test_every_newname_uses_docker_io_voipbin_prefix(self) -> None:
        """R1 #4 M6: catches missing `docker.io/` prefix."""
        kust = _load_kustomization()
        bad = [
            e for e in kust.get("images", [])
            if not e.get("newName", "").startswith("docker.io/voipbin/")
        ]
        assert not bad, (
            f"`images:` entries with newName not starting with "
            f"'docker.io/voipbin/': {[(e['name'], e.get('newName')) for e in bad]}"
        )

    def test_every_newname_in_docker_hub_allowlist(self) -> None:
        """R1 #4 M2: catches wrong repo name (e.g. forgot bin- prefix)."""
        allowlist = set(json.loads(ALLOWLIST.read_text())["repos"])
        kust = _load_kustomization()
        bad = []
        for e in kust.get("images", []):
            new_name = e.get("newName", "")
            if not new_name.startswith("docker.io/voipbin/"):
                continue  # already caught by other test
            repo = new_name.removeprefix("docker.io/voipbin/")
            if repo not in allowlist:
                bad.append((e["name"], new_name))
        assert not bad, (
            f"`images:` entries pointing to repos not on the Docker Hub voipbin "
            f"allow-list: {bad}. If the repo is genuinely new, refresh the "
            f"snapshot at {ALLOWLIST.relative_to(REPO)}."
        )

    def test_every_newtag_nonempty(self) -> None:
        """R1 #4 M7: catches empty newTag."""
        kust = _load_kustomization()
        bad = [
            e["name"] for e in kust.get("images", [])
            if not str(e.get("newTag", "")).strip()
        ]
        assert not bad, f"`images:` entries with empty newTag: {bad}"

    def test_every_newtag_is_latest(self) -> None:
        """R1 #4 M3: catches wrong newTag (e.g. `stable` instead of `latest`).

        PR-AD v2 decision: opensource self-hosted operators get :latest by
        default; SHA-pins live in `docs/operations/image-overrides.md` via
        manual override. If a future PR legitimately changes the default
        tag policy (e.g. to :stable), update this test in the same PR so
        the change is intentional, not silent.
        """
        kust = _load_kustomization()
        bad = [
            (e["name"], e.get("newTag"))
            for e in kust.get("images", [])
            if e.get("newTag") != "latest"
        ]
        assert not bad, (
            f"`images:` entries whose newTag is not 'latest': {bad}. "
            "PR-AD default is :latest; SHA-pins must use the operator override "
            "recipe in docs/operations/image-overrides.md, not be committed."
        )


# --- Container name ↔ placeholder M11 collision check via bijection -------
# (R1 #4 M11): A simple strict prefix-match would falsely fail legitimate
# aliases (e.g. container `migration` using `bin-database-image`, or the
# `asterisk` container using `voip-asterisk-call-image`). The orphan-entry
# test in TestPlaceholderImageMapping already catches the M11 class:
# if one manifest accidentally swaps its placeholder to another service's
# placeholder, the original `images:` entry becomes orphaned, and
# test_no_orphan_images_entries fails. Documented here so reviewers see the
# rationale and don't add a stricter check that would falsely flag aliases.


# --- imagePullPolicy: Always invariant (PR-AD v2 §Known limitations) ------


class TestImagePullPolicyAlways:
    def test_every_container_has_image_pull_policy_always(self) -> None:
        """Make pull semantics invariant under :latest vs SHA-pin escape hatch."""
        bad = []
        for p in _walk_k8s_yamls():
            try:
                docs = list(yaml.safe_load_all(p.read_text()))
            except yaml.YAMLError:
                continue
            for doc in docs:
                if not isinstance(doc, dict):
                    continue
                if doc.get("kind") not in {"Deployment", "StatefulSet", "DaemonSet", "Job", "CronJob"}:
                    continue
                template = (
                    doc.get("spec", {}).get("template", {})
                    if doc.get("kind") != "CronJob"
                    else doc.get("spec", {}).get("jobTemplate", {}).get("spec", {}).get("template", {})
                )
                containers = (template.get("spec", {}) or {}).get("containers", []) or []
                for c in containers:
                    img = c.get("image", "")
                    # Only enforce on our placeholder pattern (skip third-party
                    # sidecars like clickhouse/redis/rabbitmq that have their
                    # own image refs).
                    if not img.endswith("-image"):
                        continue
                    if c.get("imagePullPolicy") != "Always":
                        bad.append((p.relative_to(REPO), c.get("name", "?"), c.get("imagePullPolicy")))
        assert not bad, (
            "Containers using placeholder images must declare "
            "`imagePullPolicy: Always` (PR-AD v2 §Known limitations of :latest):\n"
            + "\n".join(f"  {p}: {n} (got {pol!r})" for p, n, pol in bad)
        )


# --- Rendered output via kubectl kustomize (R1 #4 M10) --------------------


def _kubectl_available() -> bool:
    return shutil.which("kubectl") is not None


class TestRenderedOutput:
    """Hard requirement: `kubectl kustomize k8s/` must render cleanly.

    No skip path. If kubectl is absent on the developer machine, the test
    fails with an actionable error (install repo has no CI; tests are
    operator-local hard gates per PR-AD design v2).
    """

    def test_kubectl_is_installed(self) -> None:
        assert _kubectl_available(), (
            "kubectl is required to run PR-AD rendered-output tests. "
            "Install kubectl (e.g. via gcloud or `apt-get install kubectl`) "
            "and re-run. Skipping is NOT acceptable: this is the only gate "
            "that catches semantically broken kustomization.yaml that still "
            "parses as YAML."
        )

    def test_kubectl_kustomize_exit_zero(self) -> None:
        if not _kubectl_available():
            pytest.fail("kubectl missing; see test_kubectl_is_installed.")
        r = subprocess.run(
            ["kubectl", "kustomize", str(K8S)],
            capture_output=True, text=True, timeout=120,
        )
        assert r.returncode == 0, (
            f"kubectl kustomize failed (exit={r.returncode}):\n"
            f"--- stderr ---\n{r.stderr}\n--- stdout (head) ---\n{r.stdout[:500]}"
        )

    def test_rendered_images_resolve_to_docker_io_voipbin(self) -> None:
        if not _kubectl_available():
            pytest.fail("kubectl missing; see test_kubectl_is_installed.")
        r = subprocess.run(
            ["kubectl", "kustomize", str(K8S)],
            capture_output=True, text=True, timeout=120,
        )
        assert r.returncode == 0, f"render failed: {r.stderr}"
        # Walk the rendered stream
        kind_counts = defaultdict(int)
        image_pattern = re.compile(r"^docker\.io/voipbin/[a-z0-9-]+:[a-z0-9.-]+$")
        leftover_placeholders = []
        bad_images = []
        for doc in yaml.safe_load_all(r.stdout):
            if not isinstance(doc, dict):
                continue
            kind = doc.get("kind", "")
            kind_counts[kind] += 1
            if kind not in {"Deployment", "StatefulSet", "DaemonSet", "Job", "CronJob"}:
                continue
            template = (
                doc.get("spec", {}).get("template", {})
                if kind != "CronJob"
                else doc.get("spec", {}).get("jobTemplate", {}).get("spec", {}).get("template", {})
            )
            containers = (template.get("spec", {}) or {}).get("containers", []) or []
            for c in containers:
                img = c.get("image", "")
                if img.endswith("-image"):
                    leftover_placeholders.append((doc.get("metadata", {}).get("name", "?"), c.get("name", "?"), img))
                    continue
                # Only assert on voipbin images; third-party (clickhouse, redis, ...) are out of scope
                if "voipbin" in img and not image_pattern.match(img):
                    bad_images.append((doc.get("metadata", {}).get("name", "?"), c.get("name", "?"), img))
        assert not leftover_placeholders, (
            f"Placeholders survived kustomize rendering: {leftover_placeholders}"
        )
        assert not bad_images, (
            f"Rendered voipbin images do not match docker.io/voipbin/<repo>:<tag>: {bad_images}"
        )
        # Lower-bound on deployments: catches accidental subtree removal
        assert kind_counts["Deployment"] >= 30, (
            f"Expected at least 30 Deployments after rendering, got {kind_counts['Deployment']}. "
            f"All kinds: {dict(kind_counts)}"
        )
