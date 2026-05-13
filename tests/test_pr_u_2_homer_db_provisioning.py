"""PR-U-2: HOMER Postgres database + user provisioning + substitution wiring tests.

19 cases across 7 classes mirroring design §7.2.

Skill-mandated: file-backup based revert pattern for mutant harness (NEVER git checkout).
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from scripts import k8s as k8s_mod
from scripts import preflight as preflight_mod

REPO_ROOT = Path(__file__).resolve().parent.parent
CLOUDSQL_TF = REPO_ROOT / "terraform" / "cloudsql.tf"
OUTPUTS_TF = REPO_ROOT / "terraform" / "outputs.tf"
K8S_PY = REPO_ROOT / "scripts" / "k8s.py"
PREFLIGHT_PY = REPO_ROOT / "scripts" / "preflight.py"
DEPLOY_YAML = REPO_ROOT / "k8s" / "infrastructure" / "homer" / "deployment.yaml"


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


def _extract_resource(text: str, kind: str, name: str) -> str:
    """Return the body of an HCL resource block (between outermost braces)."""
    pattern = rf'resource\s+"{re.escape(kind)}"\s+"{re.escape(name)}"\s*{{'
    m = re.search(pattern, text)
    if not m:
        raise AssertionError(f"resource {kind}.{name} not found in tf")
    start = m.end()
    depth = 1
    i = start
    while i < len(text) and depth > 0:
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
        i += 1
    return text[start : i - 1]


def _extract_output(text: str, name: str) -> str:
    pattern = rf'output\s+"{re.escape(name)}"\s*{{'
    m = re.search(pattern, text)
    if not m:
        raise AssertionError(f"output {name} not found in tf")
    start = m.end()
    depth = 1
    i = start
    while i < len(text) and depth > 0:
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
        i += 1
    return text[start : i - 1]


# ============================================================================
# §7.2 TestTerraformShape — 4 cases. 4 new TF resources by exact name + address.
# ============================================================================

class TestTerraformShape:
    """4 cases. cloudsql.tf must contain the 4 new HOMER resources."""

    def test_random_password_postgres_homer_exists(self):
        body = _extract_resource(_read(CLOUDSQL_TF), "random_password", "postgres_homer")
        assert re.search(r"length\s*=\s*24", body), "homer pw length must be 24"
        assert re.search(r"special\s*=\s*true", body), "homer pw must use special chars"

    def test_homer_data_database_exists(self):
        body = _extract_resource(
            _read(CLOUDSQL_TF), "google_sql_database", "voipbin_postgres_homer_data"
        )
        assert re.search(r'name\s*=\s*"homer_data"', body), "database name must be exact 'homer_data'"
        assert "google_sql_database_instance.voipbin_postgres.name" in body, (
            "homer_data must live on the voipbin_postgres instance"
        )

    def test_homer_config_database_exists(self):
        body = _extract_resource(
            _read(CLOUDSQL_TF), "google_sql_database", "voipbin_postgres_homer_config"
        )
        assert re.search(r'name\s*=\s*"homer_config"', body)
        assert "google_sql_database_instance.voipbin_postgres.name" in body

    def test_homer_user_exists(self):
        body = _extract_resource(
            _read(CLOUDSQL_TF), "google_sql_user", "voipbin_postgres_homer"
        )
        assert re.search(r'name\s*=\s*"homer"', body), (
            "homer user must be named exactly 'homer' (PR-U-2 locked decision)"
        )
        assert "random_password.postgres_homer.result" in body, (
            "homer user password must reference the dedicated random_password"
        )


# ============================================================================
# §7.2 TestTerraformOutputs — 2 cases. Sensitive output present and correct.
# ============================================================================

class TestTerraformOutputs:
    """2 cases. cloudsql_postgres_password_homer output declared sensitive=true."""

    def test_output_exists(self):
        body = _extract_output(_read(OUTPUTS_TF), "cloudsql_postgres_password_homer")
        assert "random_password.postgres_homer.result" in body, (
            "output must reference random_password.postgres_homer.result"
        )

    def test_output_is_sensitive(self):
        body = _extract_output(_read(OUTPUTS_TF), "cloudsql_postgres_password_homer")
        assert re.search(r"sensitive\s*=\s*true", body), (
            "homer password output MUST be sensitive=true to prevent shell-history leak"
        )


# ============================================================================
# §7.2 TestSubstitutionMapWiring — 3 cases.
# ============================================================================

class TestSubstitutionMapWiring:
    """3 cases. _build_substitution_map wires real HOMER credentials."""

    def _build(self, terraform_outputs: dict) -> dict:
        # Minimal config + secrets fixtures — substitute_map needs them but
        # PR-U-2 HOMER entries no longer consult config/secrets for fallback.
        config = {"env": "dev"}
        secrets = {}
        return k8s_mod._build_substitution_map(config, terraform_outputs, secrets)

    def test_homer_db_user_is_literal_homer(self):
        sub = self._build({"cloudsql_postgres_password_homer": "ignored"})
        assert sub["PLACEHOLDER_HOMER_DB_USER"] == "homer", (
            "PR-U-2 locked: user name is hard-coded literal 'homer'"
        )

    def test_homer_db_pass_resolves_from_terraform_output(self):
        sub = self._build({"cloudsql_postgres_password_homer": "Sup3r!Secret-xyz"})
        assert sub["PLACEHOLDER_HOMER_DB_PASS"] == "Sup3r!Secret-xyz"

    def test_homer_db_pass_empty_when_output_absent(self):
        sub = self._build({})  # no terraform output yet
        assert sub["PLACEHOLDER_HOMER_DB_PASS"] == "", (
            "empty fallback so preflight can fail-fast with operator-friendly message"
        )


# ============================================================================
# §7.2 TestPreflightGate — 3 cases.
# ============================================================================

class TestPreflightGate:
    """3 cases. check_homer_credentials_present asserts password presence."""

    def test_empty_password_with_homer_dir_raises(self):
        # HOMER dir exists in the repo (PR-U-1 shipped it), so the gate is active.
        assert DEPLOY_YAML.exists(), "PR-U-1 should have shipped the homer manifest"
        with pytest.raises(preflight_mod.PreflightError) as excinfo:
            preflight_mod.check_homer_credentials_present(
                {"cloudsql_postgres_password_homer": ""}
            )
        msg = str(excinfo.value).lower()
        assert "homer" in msg and "password" in msg, (
            "error must name HOMER + password for operator clarity"
        )

    def test_nonempty_password_passes(self):
        # No raise — function returns None.
        result = preflight_mod.check_homer_credentials_present(
            {"cloudsql_postgres_password_homer": "actual-password-string"}
        )
        assert result is None

    def test_missing_homer_dir_is_noop(self, monkeypatch, tmp_path):
        # Swap _K8S_DIR to an empty tmp_path → homer/ dir absent → no-op.
        monkeypatch.setattr(preflight_mod, "_K8S_DIR", tmp_path)
        # Even with empty password, should NOT raise because dir is absent.
        preflight_mod.check_homer_credentials_present(
            {"cloudsql_postgres_password_homer": ""}
        )


# ============================================================================
# §7.2 TestPreflightRegistration — 1 case. Mutant #15 catcher.
# ============================================================================

class TestPreflightRegistration:
    """1 case. k8s_apply must import and invoke check_homer_credentials_present."""

    def test_k8s_apply_invokes_homer_credentials_check(self):
        src = _read(K8S_PY)
        # Import (multi-line import block tolerated)
        assert re.search(
            r"from\s+scripts\.preflight\s+import\s*\([^)]*check_homer_credentials_present",
            src,
            re.DOTALL,
        ), "k8s_apply must import check_homer_credentials_present from preflight"
        # Invocation (within k8s_apply, after LB check)
        assert "check_homer_credentials_present(terraform_outputs)" in src, (
            "k8s_apply must invoke check_homer_credentials_present(terraform_outputs)"
        )


# ============================================================================
# §7.2 TestSensitiveOutputsGuard — 1 case. FIELD_MAP password discipline.
# ============================================================================

class TestSensitiveOutputsGuard:
    """1 case. cloudsql_postgres_password_homer is NOT in FIELD_MAP (password discipline)."""

    def test_homer_password_not_in_field_map(self):
        from scripts.terraform_reconcile import FIELD_MAP

        # FIELD_MAP_PASSWORD_GUARD pattern: assert NO mapping writes the homer password
        # into config.yaml. Same discipline as TestPasswordNeverInConfig in test_pr_d2a.
        for mapping in FIELD_MAP:
            tf_key = getattr(mapping, "tf_key", None) or (
                mapping.get("tf_key") if isinstance(mapping, dict) else None
            )
            cfg_key = getattr(mapping, "cfg_key", None) or (
                mapping.get("cfg_key") if isinstance(mapping, dict) else None
            )
            assert tf_key != "cloudsql_postgres_password_homer", (
                f"FIELD_MAP must NEVER persist homer password to config.yaml; "
                f"found tf_key={tf_key} cfg_key={cfg_key}"
            )
            # Defense in depth: also reject any cfg_key that obviously holds a password.
            if cfg_key:
                assert "homer" not in cfg_key.lower() or "password" not in cfg_key.lower(), (
                    f"FIELD_MAP must not write homer*password keys; got {cfg_key}"
                )


# ============================================================================
# §7.2 TestPlaceholderInvariantHolds — 1 case.
# ============================================================================

class TestPlaceholderInvariantHolds:
    """1 case. All PLACEHOLDER_* tokens in k8s/ are keyed in _build_substitution_map."""

    def test_all_homer_placeholders_resolved(self):
        # Build sub-map with realistic-shaped inputs.
        config = {"env": "dev"}
        secrets = {}
        terraform_outputs = {"cloudsql_postgres_password_homer": "test-pw"}
        sub = k8s_mod._build_substitution_map(config, terraform_outputs, secrets)

        # Scan the HOMER manifest for PLACEHOLDER_HOMER_DB_* tokens.
        homer_text = _read(DEPLOY_YAML)
        tokens = set(re.findall(r"PLACEHOLDER_HOMER_DB_[A-Z0-9_]+", homer_text))
        assert tokens, "expected PR-U-1 to have shipped PLACEHOLDER_HOMER_DB_* tokens"
        for token in tokens:
            assert token in sub, (
                f"unresolved placeholder {token} in HOMER manifest — "
                "_build_substitution_map must key every PLACEHOLDER_* token"
            )


# ============================================================================
# §7.2 TestEndToEndRender — 4 cases. Real on-disk fixture.
# ============================================================================

class TestEndToEndRender:
    """4 cases. After substitution, rendered deployment.yaml is fully wired."""

    @pytest.fixture
    def rendered(self):
        config = {"env": "dev"}
        secrets = {}
        terraform_outputs = {
            "cloudsql_postgres_password_homer": "Test-Pass!Word_42",
            "cloudsql_postgres_private_ip": "192.0.2.42",  # RFC 5737 doc range
        }
        sub = k8s_mod._build_substitution_map(config, terraform_outputs, secrets)
        text = _read(DEPLOY_YAML)
        # Apply substitution by longest-key-first to avoid prefix collisions
        # (matches the production substitution semantics in _render_manifests).
        for key in sorted(sub.keys(), key=len, reverse=True):
            text = text.replace(key, str(sub[key]))
        return text

    def test_heplify_dbuser_substituted_to_homer(self, rendered):
        # heplify container env: HEPLIFYSERVER_DBUSER → "homer"
        # Match the env block YAML shape: `- name: HEPLIFYSERVER_DBUSER\n  value: homer`
        m = re.search(
            r"name:\s*HEPLIFYSERVER_DBUSER\s*\n\s*value:\s*(\S+)", rendered
        )
        assert m, "HEPLIFYSERVER_DBUSER env entry must be present in rendered YAML"
        assert m.group(1) == "homer", f"DBUSER must render to 'homer', got {m.group(1)!r}"

    def test_homerwebapp_dbuser_substituted_to_homer(self, rendered):
        # homer-webapp container env: DB_USER → "homer"
        m = re.search(r"name:\s*DB_USER\s*\n\s*value:\s*(\S+)", rendered)
        assert m, "DB_USER env entry must be present in rendered YAML"
        assert m.group(1) == "homer", f"DB_USER must render to 'homer', got {m.group(1)!r}"

    def test_dbpass_substituted_to_fixture_password(self, rendered):
        # Both DBPASS entries must contain the fixture password.
        assert "Test-Pass!Word_42" in rendered, (
            "fixture password must appear in rendered YAML at DBPASS substitutions"
        )

    def test_no_placeholder_homer_remains(self, rendered):
        # After substitution, no PLACEHOLDER_HOMER_* tokens may survive.
        residuals = re.findall(r"PLACEHOLDER_HOMER_[A-Z0-9_]+", rendered)
        assert not residuals, (
            f"unresolved HOMER placeholders after render: {residuals}"
        )
