#!/usr/bin/env python3
"""PR-V mutant-injection harness.

12 mutants; gate ≥10 catches.
Skill-mandated: file-backup based revert (NEVER git checkout).
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent.parent
TARGETS = [
    REPO / "scripts" / "gcp.py",
    REPO / "scripts" / "wizard.py",
]


def backup() -> dict[Path, bytes]:
    return {p: p.read_bytes() for p in TARGETS}


def restore(bak: dict[Path, bytes]) -> None:
    for p, data in bak.items():
        p.write_bytes(data)
    for p, data in bak.items():
        assert p.read_bytes() == data, f"restore failed for {p}"


def run_pytest() -> bool:
    """Return True if at least ONE test failed (= mutant caught)."""
    result = subprocess.run(
        ["python", "-m", "pytest",
         "tests/test_pr_v_project_picker.py",
         "-q", "--tb=no", "-x"],
        capture_output=True, text=True, cwd=REPO,
    )
    return result.returncode != 0


def mutate_file(p: Path, old: str, new: str) -> None:
    text = p.read_text()
    assert old in text, f"mutation precondition failed: {old!r} not in {p}"
    p.write_text(text.replace(old, new, 1))


MUTANTS = []


def mutant(label: str):
    def deco(fn):
        MUTANTS.append((label, fn))
        return fn
    return deco


# M1: lifecycleState filter
@mutant("swap lifecycleState:ACTIVE → lifecycleState:DELETED")
def m1():
    mutate_file(TARGETS[0],
                '"--filter=lifecycleState:ACTIVE"',
                '"--filter=lifecycleState:DELETED"')


# M2: case-sensitive sort
@mutant("sort key case-sensitive (drop .lower())")
def m2():
    mutate_file(TARGETS[0],
                "listings.sort(key=lambda lp: lp.project_id.lower())",
                "listings.sort(key=lambda lp: lp.project_id)")


# M3: flip billing default
@mutant("flip billing_enabled default to False on missing entry")
def m3():
    mutate_file(TARGETS[0],
                "            billing_enabled=None,\n        )",
                "            billing_enabled=False,\n        )")


# M4: drop empty projectId filter
@mutant("drop `if p.get(\"projectId\")` filter")
def m4():
    mutate_file(TARGETS[0],
                'if p.get("projectId")  # Filter empty projectIds defensively',
                'if True  # MUTATED: filter removed')


# M5: billingEnabled default flip
@mutant("billingEnabled default True instead of False")
def m5():
    mutate_file(TARGETS[0],
                'bool(entry.get("billingEnabled", False))',
                'bool(entry.get("billingEnabled", True))')


# M6: sentinel rename
@mutant("rename __manual__ sentinel to __manual")
def m6():
    # Rename ONLY the comparator (selected_id != "__manual__"), not the
    # producer. Behavioral divergence: option says __manual__ but comparator
    # checks for __manual → never matches → manual selection becomes
    # project_id="__manual__" rather than falling through to prompt_text.
    mutate_file(TARGETS[1],
                'if selected_id != "__manual__":',
                'if selected_id != "__manual":')


# M7: drop empty-listings fallback
@mutant("drop `if listings:` fallback (force picker even on empty)")
def m7():
    mutate_file(TARGETS[1],
                "        listings = list_active_projects()\n        project_id = \"\"\n        if listings:",
                "        listings = list_active_projects()\n        project_id = \"\"\n        if True:")


# M8: default kwarg
@mutant("change picker default to len(listings)+1 (always manual)")
def m8():
    mutate_file(TARGETS[1],
                "                default=default_idx,\n            )",
                "                default=len(listings)+1,\n            )")


# M9: billing None render as no
@mutant("render billing_enabled None as 'billing: no' (security regression)")
def m9():
    mutate_file(TARGETS[1],
                'else:\n                    billing_str = "billing: unknown"',
                'else:\n                    billing_str = "billing: no"')


# M10: billingAccounts prefix strip wrong
@mutant("strip billingAccounts/ prefix incorrectly (off-by-one)")
def m10():
    mutate_file(TARGETS[0],
                'account_id = full_name[len("billingAccounts/"):]',
                'account_id = full_name[len("billingAccounts/")+1:]')


# M11: drop open=true filter
@mutant("drop --filter=open=true from accounts list")
def m11():
    mutate_file(TARGETS[0],
                '"--format=json", "--filter=open=true"',
                '"--format=json"')


# M12: drop displayName fallback
@mutant("drop displayName fallback (v3 schema breaks)")
def m12():
    mutate_file(TARGETS[0],
                "    if nm.startswith(\"projects/\"):\n        return dn or \"\"\n    return nm or dn or \"\"",
                "    return nm or \"\"")


def main():
    bak = backup()
    caught, missed = 0, []
    for i, (label, fn) in enumerate(MUTANTS, 1):
        try:
            fn()
        except Exception as exc:
            print(f"M{i:02d} SETUP-ERROR: {label}: {exc}")
            restore(bak)
            missed.append((i, label, "setup-error"))
            continue
        ok_failed = run_pytest()
        restore(bak)
        if ok_failed:
            print(f"M{i:02d} CAUGHT:     {label}")
            caught += 1
        else:
            print(f"M{i:02d} SURVIVED:   {label}")
            missed.append((i, label, "survived"))
    print(f"\n=== {caught}/{len(MUTANTS)} mutants caught ===")
    for p, data in bak.items():
        assert p.read_bytes() == data, f"FINAL RESTORE FAILED: {p}"
    print("Production files restored OK.")
    sys.exit(0 if caught >= 10 else 1)


if __name__ == "__main__":
    main()
