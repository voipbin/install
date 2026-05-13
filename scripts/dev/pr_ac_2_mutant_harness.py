#!/usr/bin/env python3
"""PR-AC-2 mutant-injection harness.

11 mutations from design v2 §Scope. Target: 11/11 kills.
Skill-mandated: file-backup based revert (NEVER git checkout).
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent.parent
TASKS = REPO / "ansible" / "roles" / "kamailio" / "tasks" / "main.yml"
SHIM = REPO / "ansible" / "roles" / "kamailio" / "templates" / "voipbin-kamailio-lb-routes.sh.j2"
UNIT = REPO / "ansible" / "roles" / "kamailio" / "templates" / "voipbin-kamailio-lb-routes.service.j2"

TARGETS = [TASKS, SHIM, UNIT]


def backup() -> dict[Path, bytes]:
    return {p: p.read_bytes() for p in TARGETS if p.exists()}


def restore(bak: dict[Path, bytes]) -> None:
    for p, data in bak.items():
        p.write_bytes(data)
    for p, data in bak.items():
        assert p.read_bytes() == data, f"restore failed for {p}"


def run_pytest() -> bool:
    """Return True iff the static test catches the mutation (=any test failed)."""
    result = subprocess.run(
        [
            "python", "-m", "pytest",
            "tests/test_pr_ac_2_kamailio_lb_ip_route.py",
            "-q", "--tb=no", "-x",
        ],
        capture_output=True, text=True, cwd=REPO,
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


# M1: drop external task entry (rename so static test misses it)
@mutant("M1 drop external task")
def m1() -> None:
    mutate(
        TASKS,
        "- name: Install forwarded-LB-IP shim script",
        "- name: Install forwarded-LB-IP shim script DROPPED",
    )


# M2: drop systemd start task
@mutant("M2 drop start task")
def m2() -> None:
    mutate(
        TASKS,
        "- name: Enable and start forwarded-LB-IP shim",
        "- name: Enable and start forwarded-LB-IP shim DROPPED",
    )


# M3: rename external LB var in shim
@mutant("M3 wrong external var name")
def m3() -> None:
    mutate(SHIM, "kamailio_external_lb_ip", "kamailio_external_ip")


# M4: move shim install task block to AFTER docker pull (true ordering mutation)
@mutant("M4 wrong ordering (block moved after pull)")
def m4() -> None:
    text = TASKS.read_text()
    # Extract the entire 3-task shim block (from its leading comment through
    # the "Enable and start" task) and re-insert AFTER the "Pull latest Docker
    # images" task.
    block_start = text.index("# --- Forwarded LB IP local-route shim (PR-AC-2) ---")
    # Block ends right before the next "# --- Docker pull and start ---" section
    block_end = text.index("# --- Docker pull and start ---", block_start)
    shim_block = text[block_start:block_end]
    # Remove block from original position
    text_without = text[:block_start] + text[block_end:]
    # Find the end of the "Pull latest Docker images" task (insert after it)
    pull_marker = "- name: Pull latest Docker images"
    pull_idx = text_without.index(pull_marker)
    # Find next blank-line boundary after pull task
    next_task = text_without.index("\n- name:", pull_idx + len(pull_marker))
    new_text = (
        text_without[: next_task + 1]
        + shim_block.rstrip() + "\n\n"
        + text_without[next_task + 1 :]
    )
    assert new_text != text, "M4 precondition: failed to relocate shim block"
    TASKS.write_text(new_text)


# M5: remove empty-string guard from shim
@mutant("M5 missing empty-string guard")
def m5() -> None:
    text = SHIM.read_text()
    text = re.sub(r"if \[ -z \"\$ip\" \]; then[\s\S]*?fi\n", "", text, count=1)
    SHIM.write_text(text)


# M6: weaken pre-check (drop "match ... type local")
@mutant("M6 weakened pre-check (drop match)")
def m6() -> None:
    mutate(
        SHIM,
        "ip route show table local match",
        "ip route show table local | grep",
    )


# M7: change route type to unicast
@mutant("M7 route type unicast")
def m7() -> None:
    mutate(SHIM, "ip route add local ", "ip route add unicast ")


# M8: change scope from host to link on the ip route add line
@mutant("M8 scope link")
def m8() -> None:
    mutate_re(
        SHIM,
        r"(ip route add local [^\n]*?)scope host",
        r"\1scope link",
    )


# M9: hardcode interface
@mutant("M9 hardcoded ens4")
def m9() -> None:
    mutate(SHIM, "{{ ansible_default_ipv4.interface }}", "ens4")


# M10: weaken pre-check to plain grep substring (no /32 type local)
@mutant("M10 plain grep substring")
def m10() -> None:
    # Different from M6: remove the /32 token specifically
    mutate(SHIM, "/32", "")


# M11: drop become:true from one of the three tasks
@mutant("M11 missing become on start task")
def m11() -> None:
    text = TASKS.read_text()
    # Find the "Enable and start" block and remove its `become: true` line
    pattern = (
        r"(- name: Enable and start forwarded-LB-IP shim\s*\n"
        r"(?:.*\n)*?)  become: true\n"
    )
    new = re.sub(pattern, r"\1", text, count=1)
    assert new != text, "M11 precondition: become:true line not found"
    TASKS.write_text(new)


def main() -> int:
    bak = backup()
    killed = 0
    survived = []
    for label, fn in MUTANTS:
        try:
            fn()
        except AssertionError as exc:
            print(f"  SKIP   {label}: precondition failed ({exc})")
            restore(bak)
            continue
        caught = run_pytest()
        restore(bak)
        status = "KILLED" if caught else "SURVIVED"
        if caught:
            killed += 1
        else:
            survived.append(label)
        print(f"  {status:8s} {label}")
    print(f"\n{killed}/{len(MUTANTS)} mutants killed")
    if survived:
        print("survivors:")
        for s in survived:
            print(f"  - {s}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
