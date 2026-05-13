#!/usr/bin/env python3
"""PR-U-3 mutant-injection harness.

15 mutants; gate ≥12 catches.
Skill-mandated: file-backup based revert (NEVER git checkout).
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent.parent
TARGETS = [
    REPO / "ansible" / "roles" / "kamailio" / "templates" / "docker-compose.yml.j2",
    REPO / "ansible" / "inventory" / "group_vars" / "kamailio.yml",
    REPO / "scripts" / "preflight.py",
    REPO / "scripts" / "ansible_runner.py",
    REPO / "config" / "schema.py",
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
         "tests/test_pr_u_3_kamailio_heplify_sidecar.py",
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


@mutant("rename heplify-client: → heplify-clientX:")
def m1():
    mutate_file(TARGETS[0], "heplify-client:", "heplify-clientX:")


@mutant("bump sipcapture/heplify:1.56 → 1.57")
def m2():
    mutate_file(TARGETS[0], "sipcapture/heplify:1.56", "sipcapture/heplify:1.57")


@mutant("remove network_mode: host from sidecar")
def m3():
    # Use a sidecar-block-specific marker: the line after `container_name: kamailio-heplify`
    mutate_file(TARGETS[0],
                "container_name: kamailio-heplify\n    restart: unless-stopped\n    network_mode: host",
                "container_name: kamailio-heplify\n    restart: unless-stopped")


@mutant("drop depends_on: kamailio from sidecar")
def m4():
    mutate_file(TARGETS[0],
                "depends_on:\n      - kamailio\n    logging:\n      driver: json-file",
                "logging:\n      driver: json-file")


@mutant("replace -i any with -i eth0")
def m5():
    mutate_file(TARGETS[0], '"-i"\n      - "any"', '"-i"\n      - "eth0"')


@mutant("swap -m SIP → -m RTCP")
def m6():
    mutate_file(TARGETS[0], '"-m"\n      - "SIP"', '"-m"\n      - "RTCP"')


@mutant("drop -dim REGISTER")
def m7():
    mutate_file(TARGETS[0],
                '"-dim"\n      - "REGISTER"\n    depends_on:',
                'depends_on:')


@mutant("change group_vars homer_uri to 127.0.0.1:9060")
def m8():
    mutate_file(TARGETS[1],
                'homer_uri: "{% if heplify_lb_ip %}{{ heplify_lb_ip }}:9060{% endif %}"',
                'homer_uri: "127.0.0.1:9060"')


@mutant("flip group_vars homer_enabled true → false")
def m9():
    mutate_file(TARGETS[1], 'homer_enabled: "true"', 'homer_enabled: "false"')


@mutant("invert preflight raise → swallow empty IP")
def m10():
    mutate_file(TARGETS[2],
                "    if not lb_ip:\n        raise PreflightError(",
                "    if lb_ip and False:\n        raise PreflightError(")


@mutant("remove homer_enabled gate (always check)")
def m11():
    mutate_file(TARGETS[2],
                "    if not bool(config.get(\"homer_enabled\", True)):\n        return\n",
                "    if False:\n        return\n")


@mutant("drop preflight invocation from ansible_run")
def m12():
    mutate_file(TARGETS[3],
                "    try:\n        check_kamailio_homer_uri_present(terraform_outputs, config)\n    except PreflightError as exc:\n        print_error(str(exc))\n        return False\n",
                "    # preflight removed by mutant\n")


@mutant("drop homer_enabled flat-var emission")
def m13():
    mutate_file(TARGETS[3],
                '    ansible_vars["homer_enabled"] = (\n        "true" if bool(config.get("homer_enabled", True)) else "false"\n    )\n',
                "    # flat-var removed by mutant\n")


@mutant("remove Jinja gate {% if homer_enabled ... %} (sidecar always rendered)")
def m14():
    mutate_file(TARGETS[0],
                "{% if homer_enabled | bool and heplify_lb_ip %}",
                "")


@mutant("drop restart: unless-stopped from sidecar")
def m15():
    mutate_file(TARGETS[0],
                "container_name: kamailio-heplify\n    restart: unless-stopped\n",
                "container_name: kamailio-heplify\n")


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
    sys.exit(0 if caught >= 12 else 1)


if __name__ == "__main__":
    main()
