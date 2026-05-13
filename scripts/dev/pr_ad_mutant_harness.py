#!/usr/bin/env python3
"""PR-AD mutant-injection harness.

11 mutations from design v2 §Mutant matrix. Target 11/11 kills.

Defensive pattern (skill cli-actual-execution-smoke §Subprocess-timeout):
  * Backup-and-restore by file contents in Python dict, not git checkout.
  * subprocess.TimeoutExpired → restore before re-raising.
  * Final integrity check: every target file must match its backup byte-for-byte.
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent.parent
KUST = REPO / "k8s" / "kustomization.yaml"
# Pick deterministic manifest targets for mutations
AGENT_MANIFEST = REPO / "k8s" / "backend" / "services" / "agent-manager.yaml"
AI_MANIFEST = REPO / "k8s" / "backend" / "services" / "ai-manager.yaml"
ADMIN_MANIFEST = REPO / "k8s" / "frontend" / "admin.yaml"
# Deep-tree target for M9
DEEP_TARGET = REPO / "k8s" / "voip" / "asterisk-call" / "deployment.yaml"

TARGETS = [KUST, AGENT_MANIFEST, AI_MANIFEST, ADMIN_MANIFEST, DEEP_TARGET]


def backup() -> dict[Path, bytes]:
    return {p: p.read_bytes() for p in TARGETS if p.exists()}


def restore(bak: dict[Path, bytes]) -> None:
    for p, data in bak.items():
        p.write_bytes(data)
    for p, data in bak.items():
        assert p.read_bytes() == data, f"restore failed for {p}"


def run_pytest(timeout: int = 60) -> bool:
    """Return True iff static test catches the mutation (=any test failed)."""
    result = subprocess.run(
        ["python", "-m", "pytest",
         "tests/test_pr_ad_k8s_image_rendering.py",
         "-q", "--tb=no", "-x"],
        capture_output=True, text=True, cwd=REPO, timeout=timeout,
    )
    return result.returncode != 0


def mutate(p: Path, old: str, new: str) -> None:
    text = p.read_text()
    assert old in text, f"precondition: {old!r} not in {p.name}"
    p.write_text(text.replace(old, new, 1))


def mutate_re(p: Path, pattern: str, replacement: str, flags: int = 0) -> None:
    text = p.read_text()
    new = re.sub(pattern, replacement, text, count=1, flags=flags)
    assert new != text, f"regex precondition: {pattern!r} did not match in {p.name}"
    p.write_text(new)


MUTANTS: list[tuple[str, callable]] = []


def mutant(label: str):
    def deco(fn):
        MUTANTS.append((label, fn))
        return fn
    return deco


# M1: drop one images: entry (the agent-manager-image block)
@mutant("M1 drop images entry")
def m1() -> None:
    text = KUST.read_text()
    block_re = re.compile(
        r"  - name: agent-manager-image\n    newName:[^\n]+\n    newTag: [^\n]+\n",
        re.MULTILINE,
    )
    new = block_re.sub("", text, count=1)
    assert new != text, "M1 precondition: agent-manager-image block not found"
    KUST.write_text(new)


# M2: wrong newName prefix (forget bin-)
@mutant("M2 wrong newName (no bin- prefix)")
def m2() -> None:
    mutate(
        KUST,
        "newName: docker.io/voipbin/bin-agent-manager",
        "newName: docker.io/voipbin/agent-manager",
    )


# M3: wrong newTag
@mutant("M3 wrong newTag (stable not latest)")
def m3() -> None:
    mutate_re(
        KUST,
        r"(- name: agent-manager-image\n    newName:[^\n]+\n    newTag: )latest",
        r"\1stable",
    )


# M4: typo placeholder in manifest
@mutant("M4 typo placeholder in manifest")
def m4() -> None:
    mutate(
        AGENT_MANIFEST,
        "image: agent-manager-image",
        "image: agent-mgr-image",
    )


# M5: stray literal in manifest
@mutant("M5 stray literal image: voipbin/foo")
def m5() -> None:
    text = AI_MANIFEST.read_text()
    new = text.replace(
        "image: ai-manager-image",
        "image: voipbin/ai-manager",
        1,
    )
    assert new != text, "M5 precondition: ai-manager-image placeholder not found"
    AI_MANIFEST.write_text(new)


# M6: drop docker.io/ prefix in newName
@mutant("M6 newName missing docker.io/")
def m6() -> None:
    mutate(
        KUST,
        "newName: docker.io/voipbin/bin-ai-manager",
        "newName: voipbin/bin-ai-manager",
    )


# M7: empty newTag
@mutant("M7 empty newTag")
def m7() -> None:
    mutate_re(
        KUST,
        r"(- name: ai-manager-image\n    newName:[^\n]+\n    newTag: )latest",
        r"\1",
    )


# M8: orphan images: entry (no matching placeholder in any manifest)
@mutant("M8 orphan images entry")
def m8() -> None:
    text = KUST.read_text()
    addition = (
        "  - name: nonexistent-orphan-image\n"
        "    newName: docker.io/voipbin/bin-agent-manager\n"
        "    newTag: latest\n"
    )
    new = text.rstrip() + "\n" + addition
    KUST.write_text(new)


# M9: stray literal in deep subtree (catches walker narrowing)
@mutant("M9 deep-tree stray literal")
def m9() -> None:
    text = DEEP_TARGET.read_text()
    new = text.replace(
        "image: voip-asterisk-call-image",
        "image: voipbin/voip-asterisk-call",
        1,
    )
    assert new != text, "M9 precondition: voip-asterisk-call-image not found"
    DEEP_TARGET.write_text(new)


# M10: kustomization.yaml semantically broken (newName mis-indented as sibling of images)
@mutant("M10 kustomize semantic break (indent)")
def m10() -> None:
    text = KUST.read_text()
    # Replace the agent-manager entry's newName indent so it becomes a top-level
    # key with no value (yaml stays valid but kustomize errors).
    new = text.replace(
        "  - name: agent-manager-image\n    newName: docker.io/voipbin/bin-agent-manager\n    newTag: latest\n",
        "  - name: agent-manager-image\nnewName: docker.io/voipbin/bin-agent-manager\n    newTag: latest\n",
        1,
    )
    assert new != text, "M10 precondition: agent-manager-image block not found"
    KUST.write_text(new)


# M11: placeholder collision (two manifests use the same placeholder accidentally)
@mutant("M11 placeholder collision (swap)")
def m11() -> None:
    # Swap ai-manager-image to use agent-manager-image so both agent-manager.yaml
    # AND ai-manager.yaml point at agent-manager-image. The orphan check then
    # catches the now-unreferenced ai-manager-image entry.
    text = AI_MANIFEST.read_text()
    new = text.replace(
        "image: ai-manager-image",
        "image: agent-manager-image",
        1,
    )
    assert new != text, "M11 precondition: ai-manager-image not found"
    AI_MANIFEST.write_text(new)


def main() -> int:
    bak = backup()
    killed = 0
    survived: list[str] = []
    for label, fn in MUTANTS:
        try:
            fn()
        except AssertionError as exc:
            print(f"  SETUP-ERR {label}: {exc}")
            restore(bak)
            continue
        try:
            caught = run_pytest(timeout=60)
        except subprocess.TimeoutExpired:
            restore(bak)
            print(f"  KILLED   {label}  (timeout — mutation altered behavior)")
            killed += 1
            continue
        restore(bak)
        if caught:
            killed += 1
            print(f"  KILLED   {label}")
        else:
            survived.append(label)
            print(f"  SURVIVED {label}")
    # Final integrity check (cli-actual-execution-smoke skill defense #2)
    for p, data in bak.items():
        assert p.read_bytes() == data, f"LEAK: {p} drifted from baseline"
    print(f"\n{killed}/{len(MUTANTS)} mutants killed")
    if survived:
        print("survivors:")
        for s in survived:
            print(f"  - {s}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
