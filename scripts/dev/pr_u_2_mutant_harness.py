#!/usr/bin/env python3
"""PR-U-2 mutant-injection harness.

15 mutants; gate ≥12 catches.
Skill-mandated: file-backup based revert (NEVER git checkout).
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent.parent
TARGETS = [
    REPO / "terraform" / "cloudsql.tf",
    REPO / "terraform" / "outputs.tf",
    REPO / "scripts" / "k8s.py",
    REPO / "scripts" / "preflight.py",
    REPO / "tests" / "test_pr_d2a_cloudsql_resources.py",
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
        ["python", "-m", "pytest", "tests/test_pr_u_2_homer_db_provisioning.py",
         "tests/test_pr_d2a_cloudsql_resources.py",
         "tests/test_pr4_manifest_invariants.py",
         "-q", "--tb=no", "-x"],
        capture_output=True, text=True, cwd=REPO,
    )
    return result.returncode != 0


def mutate(text: str, old: str, new: str) -> str:
    assert old in text, f"mutation precondition failed: {old!r} not in text"
    return text.replace(old, new, 1)


def mutate_file(p: Path, old: str, new: str) -> None:
    p.write_text(mutate(p.read_text(), old, new))


MUTANTS = []


def mutant(label: str):
    def deco(fn):
        MUTANTS.append((label, fn))
        return fn
    return deco


# 1: rename TF resource voipbin_postgres_homer
@mutant("rename random_password.postgres_homer → postgres_homerX")
def m1():
    mutate_file(REPO / "terraform" / "cloudsql.tf",
                'resource "random_password" "postgres_homer"',
                'resource "random_password" "postgres_homerX"')

# 2: drop sensitive=true from homer output
@mutant("drop sensitive=true from homer password output")
def m2():
    p = REPO / "terraform" / "outputs.tf"
    text = p.read_text()
    # Patch only the homer block: replace its sensitive=true with sensitive=false
    head, _, tail = text.partition('output "cloudsql_postgres_password_homer"')
    body, brace, rest = tail.partition("}")
    body = body.replace("sensitive   = true", "sensitive   = false", 1)
    p.write_text(head + 'output "cloudsql_postgres_password_homer"' + body + brace + rest)

# 3: remove homer_data database resource
@mutant("remove google_sql_database.voipbin_postgres_homer_data")
def m3():
    p = REPO / "terraform" / "cloudsql.tf"
    text = p.read_text()
    start = text.index('resource "google_sql_database" "voipbin_postgres_homer_data"')
    end = text.index("}", start) + 1
    p.write_text(text[:start] + text[end:])

# 4: remove homer_config database resource
@mutant("remove google_sql_database.voipbin_postgres_homer_config")
def m4():
    p = REPO / "terraform" / "cloudsql.tf"
    text = p.read_text()
    start = text.index('resource "google_sql_database" "voipbin_postgres_homer_config"')
    end = text.index("}", start) + 1
    p.write_text(text[:start] + text[end:])

# 5: typo homer_data → homer_dta in TF
@mutant("typo homer_data → homer_dta in DB name field")
def m5():
    mutate_file(REPO / "terraform" / "cloudsql.tf",
                'name      = "homer_data"', 'name      = "homer_dta"')

# 6: rename random_password.postgres_homer → postgres_homerXX (second test of #1 variant)
@mutant("rename random_password reference in user resource")
def m6():
    mutate_file(REPO / "terraform" / "cloudsql.tf",
                "random_password.postgres_homer.result",
                "random_password.postgres_homerXX.result")

# 7: hard-code DBUSER to "voipbin" in k8s.py
@mutant("hard-code PLACEHOLDER_HOMER_DB_USER to voipbin")
def m7():
    mutate_file(REPO / "scripts" / "k8s.py",
                '"PLACEHOLDER_HOMER_DB_USER": "homer"',
                '"PLACEHOLDER_HOMER_DB_USER": "voipbin"')

# 8: resolve DBPASS from wrong TF key
@mutant("resolve DBPASS from wrong terraform output key")
def m8():
    mutate_file(REPO / "scripts" / "k8s.py",
                '"cloudsql_postgres_password_homer", ""',
                '"cloudsql_postgres_password_homerWRONG", ""')

# 9: invert preflight raise → swallow
@mutant("invert preflight raise (swallow empty password)")
def m9():
    mutate_file(REPO / "scripts" / "preflight.py",
                "    if not pw:\n        raise PreflightError(",
                "    if pw and False:\n        raise PreflightError(")

# 10: remove HOMER-dir gate (always raise)
@mutant("remove HOMER-dir gate in preflight (always check)")
def m10():
    mutate_file(REPO / "scripts" / "preflight.py",
                "    homer_dir = _K8S_DIR / \"infrastructure\" / \"homer\"\n    if not homer_dir.exists():\n        return\n",
                "    homer_dir = _K8S_DIR / \"infrastructure\" / \"homer\"\n    if False:\n        return\n")

# 11: drop homer entry from TestSensitiveOutputs.EXPECTED
@mutant("drop cloudsql_postgres_password_homer from TestSensitiveOutputs.EXPECTED")
def m11():
    p = REPO / "tests" / "test_pr_d2a_cloudsql_resources.py"
    text = p.read_text()
    target = '        "cloudsql_postgres_password_homer":\n            "random_password.postgres_homer.result",\n'
    p.write_text(text.replace(target, ""))

# 12: replace terraform_outputs.get with literal ""
@mutant("replace terraform_outputs.get with empty literal in k8s.py homer wiring")
def m12():
    mutate_file(REPO / "scripts" / "k8s.py",
                '"PLACEHOLDER_HOMER_DB_PASS": terraform_outputs.get(\n            "cloudsql_postgres_password_homer", ""\n        ),',
                '"PLACEHOLDER_HOMER_DB_PASS": "",')

# 13: rename TF user name homer → heplify
@mutant("rename TF user name homer → heplify (k8s.py still says homer)")
def m13():
    mutate_file(REPO / "terraform" / "cloudsql.tf",
                'resource "google_sql_user" "voipbin_postgres_homer" {\n  name     = "homer"',
                'resource "google_sql_user" "voipbin_postgres_homer" {\n  name     = "heplify"')

# 14: rename TF output cloudsql_postgres_password_homer → ..._homerX
@mutant("rename TF output cloudsql_postgres_password_homer")
def m14():
    mutate_file(REPO / "terraform" / "outputs.tf",
                'output "cloudsql_postgres_password_homer"',
                'output "cloudsql_postgres_password_homerX"')

# 15: drop the check_homer_credentials_present(...) call from k8s_apply
@mutant("drop check_homer_credentials_present invocation in k8s.py")
def m15():
    mutate_file(REPO / "scripts" / "k8s.py",
                "    try:\n        check_homer_credentials_present(terraform_outputs)\n    except PreflightError as exc:\n        print_error(str(exc))\n        return False\n",
                "    # invocation removed by mutant\n")


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
    # Final integrity check
    for p, data in bak.items():
        assert p.read_bytes() == data, f"FINAL RESTORE FAILED: {p}"
    print("Production files restored OK.")
    sys.exit(0 if caught >= 12 else 1)


if __name__ == "__main__":
    main()
