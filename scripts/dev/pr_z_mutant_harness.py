#!/usr/bin/env python3
"""PR-Z mutant-injection harness.

16 mutations from design §11 (some adapted to actual code structure).
Target ≥14 kills.
Skill-mandated: file-backup based revert (NEVER git checkout).
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent.parent
TARGETS = [
    REPO / "scripts" / "tls_bootstrap.py",
    REPO / "scripts" / "cert_lifecycle.py",
    REPO / "scripts" / "pipeline.py",
    REPO / "scripts" / "config.py",
    REPO / "config" / "schema.py",
    REPO / "ansible" / "roles" / "kamailio" / "tasks" / "main.yml",
]


def backup() -> dict[Path, bytes]:
    return {p: p.read_bytes() for p in TARGETS if p.exists()}


def restore(bak: dict[Path, bytes]) -> None:
    for p, data in bak.items():
        p.write_bytes(data)
    for p, data in bak.items():
        assert p.read_bytes() == data, f"restore failed for {p}"


def run_pytest() -> bool:
    """Return True if at least ONE PR-Z test failed (= mutant caught)."""
    result = subprocess.run(
        [
            "python", "-m", "pytest",
            "tests/test_pr_z_tls_bootstrap_kamailio.py",
            "tests/test_pr_z_cert_lifecycle.py",
            "tests/test_pr_z_pipeline_cert_stage.py",
            "tests/test_pr_z_cli_cert.py",
            "tests/test_pr_z_config_schema.py",
            "tests/test_pr_z_wizard.py",
            "tests/test_pr_z_secret_schema.py",
            "tests/test_pr_z_cert_deploy_path_guard.py",
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


# M1: CA validity reduced
@mutant("CA validity reduced to 365 days")
def m1():
    mutate(REPO / "scripts" / "tls_bootstrap.py",
           "DEFAULT_CA_VALID_DAYS = 3650",
           "DEFAULT_CA_VALID_DAYS = 365")


# M2: KAMAILIO_PAIRS truncated to sip only
@mutant("KAMAILIO_PAIRS registrar entry dropped")
def m2():
    mutate(REPO / "scripts" / "tls_bootstrap.py",
           '    ("registrar", "KAMAILIO_CERT_REGISTRAR_BASE64",\n'
           '     "KAMAILIO_PRIVKEY_REGISTRAR_BASE64"),\n',
           '')


# M3: Leaf signed with leaf's own key (chain broken)
@mutant("Leaf signed with leaf's own private key instead of CA")
def m3():
    mutate(REPO / "scripts" / "tls_bootstrap.py",
           ".sign(private_key=ca_key, algorithm=hashes.SHA256())",
           ".sign(private_key=key, algorithm=hashes.SHA256())")


# M4: schema enum dropped
@mutant("config schema accepts any cert_mode (enum dropped)")
def m4():
    mutate_re(REPO / "config" / "schema.py",
              r'"enum":\s*\[\s*"self_signed"\s*,\s*"manual"\s*\]',
              '"type": "string"')


# M5: manual mode validation skipped
@mutant("manual mode skips dir validation")
def m5():
    p = REPO / "scripts" / "cert_lifecycle.py"
    text = p.read_text()
    sig_idx = text.find("def _validate_manual_cert_dir(")
    if sig_idx < 0:
        raise AssertionError("M5: _validate_manual_cert_dir not found")
    # Walk from sig to the colon ending the signature (handles multi-line sig).
    end = text.find("\n) ->", sig_idx)
    if end < 0:
        end = text.find("):", sig_idx)
        colon_end = end + 2
    else:
        # multi-line sig ends with ") -> ReturnType:"
        colon_end = text.find(":", end) + 1
    new = text[:colon_end] + "\n    return {}  # MUTATED" + text[colon_end:]
    assert new != text
    p.write_text(new)


# M6: Short-circuit disabled (always reissue)
@mutant("_state_short_circuit_ok always returns False")
def m6():
    p = REPO / "scripts" / "cert_lifecycle.py"
    text = p.read_text()
    sig = "def _state_short_circuit_ok("
    idx = text.find(sig)
    if idx < 0:
        raise AssertionError("M6: _state_short_circuit_ok not found")
    end = text.find(":\n", idx)
    new = text[:end] + ":\n    return False  # MUTATED\n" + text[end + 2:]
    p.write_text(new)


# M7: audit always passes (treats expired as valid)
@mutant("_audit_secret_completeness always returns OK")
def m7():
    p = REPO / "scripts" / "cert_lifecycle.py"
    text = p.read_text()
    sig = "def _audit_secret_completeness("
    idx = text.find(sig)
    if idx < 0:
        raise AssertionError("M7: _audit_secret_completeness not found")
    end = text.find(":\n", idx)
    new = text[:end] + ":\n    return True, []  # MUTATED\n" + text[end + 2:]
    p.write_text(new)


# M8: leaf validity 1 day
@mutant("leaf default validity reduced to 1 day")
def m8():
    mutate(REPO / "scripts" / "tls_bootstrap.py",
           "DEFAULT_LEAF_VALID_DAYS = 365",
           "DEFAULT_LEAF_VALID_DAYS = 1")


# M9: renewal threshold flipped
@mutant("renewal threshold flipped (RENEWAL_THRESHOLD_DAYS = -30)")
def m9():
    mutate(REPO / "scripts" / "cert_lifecycle.py",
           "RENEWAL_THRESHOLD_DAYS = 30",
           "RENEWAL_THRESHOLD_DAYS = -30")


# M10: acme mode silently accepted
@mutant("acme mode error path neutralized")
def m10():
    p = REPO / "scripts" / "config.py"
    text = p.read_text()
    if 'cert_mode=acme is not yet supported' not in text:
        raise AssertionError("M10: acme error string not found")
    # Find the errors.append (or raise) carrying the acme message; neutralize.
    new = re.sub(
        r'(errors\.append\([^)]*cert_mode=acme[^)]*\))',
        r'pass  # MUTATED \1',
        text, count=1, flags=re.DOTALL,
    )
    if new == text:
        # Try raise form
        new = re.sub(
            r'(raise [A-Za-z_]+\([^)]*cert_mode=acme[^)]*\))',
            r'pass  # MUTATED',
            text, count=1, flags=re.DOTALL,
        )
    assert new != text, "M10: no neutralization point found"
    p.write_text(new)


# M11: APPLY_STAGES order flipped
@mutant("APPLY_STAGES: cert_provision after ansible_run")
def m11():
    mutate(REPO / "scripts" / "pipeline.py",
           '"reconcile_k8s_outputs",\n    "cert_provision",\n    "ansible_run",',
           '"reconcile_k8s_outputs",\n    "ansible_run",\n    "cert_provision",')


# M12: ansible path-guard removed
@mutant("ansible Assert cert deploy path is safe block removed")
def m12():
    mutate_re(REPO / "ansible" / "roles" / "kamailio" / "tasks" / "main.yml",
              r"- name: Assert cert deploy path is safe.*?(?=- name:)",
              "",
              flags=re.DOTALL)


# M13: synchronize delete flag flipped
@mutant("synchronize delete: true -> delete: false")
def m13():
    mutate(REPO / "ansible" / "roles" / "kamailio" / "tasks" / "main.yml",
           "delete: true",
           "delete: false")


# M14: STAGE_LABELS entry removed
@mutant("STAGE_LABELS['cert_provision'] dropped")
def m14():
    mutate_re(REPO / "scripts" / "pipeline.py",
              r'\n\s*"cert_provision":\s*"[^"]+",',
              "")


# M15: cert_manual_dir field not required (schema conditional dropped)
@mutant("schema allOf conditional requiring cert_manual_dir dropped")
def m15():
    p = REPO / "config" / "schema.py"
    text = p.read_text()
    if 'cert_manual_dir' not in text:
        raise AssertionError("M15: cert_manual_dir not in schema.py")
    # Strip the whole allOf block that references cert_manual_dir
    new = re.sub(
        r'"allOf":\s*\[[^\]]*cert_manual_dir[^\]]*\],?\s*',
        '',
        text, count=1, flags=re.DOTALL,
    )
    if new == text:
        # Fallback: strip any "required": [..., "cert_manual_dir"] pattern
        new = re.sub(r'"cert_manual_dir"\s*,?\s*', '', text, count=2)
    assert new != text, "M15: no schema conditional removable"
    p.write_text(new)


# M16: KAMAILIO_PAIRS order swap
@mutant("KAMAILIO_PAIRS order swapped")
def m16():
    p = REPO / "scripts" / "tls_bootstrap.py"
    text = p.read_text()
    sip = '("sip", "KAMAILIO_CERT_SIP_BASE64", "KAMAILIO_PRIVKEY_SIP_BASE64"),'
    reg = '("registrar", "KAMAILIO_CERT_REGISTRAR_BASE64",\n     "KAMAILIO_PRIVKEY_REGISTRAR_BASE64"),'
    if sip not in text or reg not in text:
        raise AssertionError("M16: pair lines not found")
    new = text.replace(sip, "__SIP__").replace(reg, "__REG__")
    new = new.replace("__SIP__", reg).replace("__REG__", sip)
    p.write_text(new)


def main():
    bak = backup()
    caught = 0
    print()
    print("| # | Mutation | Killed |")
    print("|---|---|---|")
    for i, (label, fn) in enumerate(MUTANTS, 1):
        try:
            fn()
        except Exception as exc:
            print(f"| {i} | {label} | SETUP-ERROR: {str(exc)[:80]} |")
            restore(bak)
            continue
        ok_failed = run_pytest()
        restore(bak)
        if ok_failed:
            print(f"| {i} | {label} | ✓ |")
            caught += 1
        else:
            print(f"| {i} | {label} | ✗ SURVIVED |")
    total = len(MUTANTS)
    print(f"| | **Total** | **{caught}/{total}** |")
    print()
    for p, data in bak.items():
        assert p.read_bytes() == data, f"FINAL RESTORE FAILED: {p}"
    print("(Production files restored OK.)")
    sys.exit(0 if caught >= 14 else 1)


if __name__ == "__main__":
    main()
