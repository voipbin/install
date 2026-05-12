"""Tests for PR-D2c: kamailioro ansible wiring + URL-safety guard.

Covers scripts/ansible_runner.py::_build_kamailio_auth_db_url and its
integration into _write_extra_vars, plus operator-doc linkage.

Design doc. docs/plans/2026-05-12-pr-d2c-kamailioro-ansible-wiring-design.md
"""

import json
import os
import stat
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from scripts.ansible_runner import (
    _build_kamailio_auth_db_url,
    _write_extra_vars,
)


# Fixture password (24 chars, locked alphabet, includes + ! * . - _).
# Matches PR-D2a random_password.mysql_kamailioro length=24.
FIXTURE_PW = "Sample-pw_1.2*3!a+x9.AaZ"
FIXTURE_ENCODED = "Sample-pw_1.2*3!a%2Bx9.AaZ"
FIXTURE_HOST = "10.99.0.3"
EXPECTED_URL = f"mysql://kamailioro:{FIXTURE_ENCODED}@{FIXTURE_HOST}:3306/asterisk"


def _make_config(data: dict) -> MagicMock:
    """Mock InstallerConfig with to_ansible_vars + get."""
    cfg = MagicMock()
    cfg.to_ansible_vars.return_value = dict(data)
    cfg.get.side_effect = lambda key, default="": data.get(key, default)
    return cfg


class TestBuildKamailioAuthDbUrlHappyPath:
    """The happy-path URL is split into six distinct sub-assertions so each
    URL-structure mutation in the design's §7 mutant table trips a unique
    test case."""

    def _build(self):
        cfg = _make_config({"cloudsql_private_ip": FIXTURE_HOST})
        outputs = {"cloudsql_mysql_password_kamailioro": FIXTURE_PW}
        return _build_kamailio_auth_db_url(cfg, outputs)

    def test_url_prefix_exactly_mysql(self):
        url = self._build()
        assert url.startswith("mysql://"), url

    def test_username_exactly_kamailioro(self):
        url = self._build()
        # userinfo is between "mysql://" and the next "@".
        userinfo = url[len("mysql://") : url.index("@")]
        username = userinfo.split(":", 1)[0]
        assert username == "kamailioro", url

    def test_encoded_password_equals_expected(self):
        url = self._build()
        userinfo = url[len("mysql://") : url.index("@")]
        encoded_password = userinfo.split(":", 1)[1]
        assert encoded_password == FIXTURE_ENCODED, url

    def test_host_appears_verbatim(self):
        url = self._build()
        host_port_path = url[url.index("@") + 1 :]
        host = host_port_path.split(":", 1)[0]
        assert host == FIXTURE_HOST, url

    def test_port_exactly_3306(self):
        url = self._build()
        host_port_path = url[url.index("@") + 1 :]
        port = host_port_path.split(":", 1)[1].split("/", 1)[0]
        assert port == "3306", url

    def test_path_exactly_asterisk(self):
        url = self._build()
        path = url.rsplit("/", 1)[-1]
        assert path == "asterisk", url


class TestBuildKamailioAuthDbUrlEmptyInputs:
    """Empty / missing inputs return "" without raising so dev / early-apply
    flows do not crash. None-valued password is tolerated explicitly because
    terraform outputs may surface null for sensitive values in some corners.
    """

    def test_empty_password_returns_empty_string(self):
        cfg = _make_config({"cloudsql_private_ip": FIXTURE_HOST})
        outputs = {"cloudsql_mysql_password_kamailioro": ""}
        assert _build_kamailio_auth_db_url(cfg, outputs) == ""

    def test_empty_host_returns_empty_string(self):
        cfg = _make_config({"cloudsql_private_ip": ""})
        outputs = {"cloudsql_mysql_password_kamailioro": FIXTURE_PW}
        assert _build_kamailio_auth_db_url(cfg, outputs) == ""

    def test_whitespace_only_host_returns_empty_string(self):
        cfg = _make_config({"cloudsql_private_ip": "   "})
        outputs = {"cloudsql_mysql_password_kamailioro": FIXTURE_PW}
        assert _build_kamailio_auth_db_url(cfg, outputs) == ""

    def test_none_password_returns_empty_string(self):
        cfg = _make_config({"cloudsql_private_ip": FIXTURE_HOST})
        outputs = {"cloudsql_mysql_password_kamailioro": None}
        assert _build_kamailio_auth_db_url(cfg, outputs) == ""


class TestBuildKamailioAuthDbUrlRejectsUnsafePassword:
    """Future alphabet drift must be caught at apply time with a clear
    RuntimeError that points operators back to the rotation doc."""

    def _run_with_password(self, password: str):
        cfg = _make_config({"cloudsql_private_ip": FIXTURE_HOST})
        outputs = {"cloudsql_mysql_password_kamailioro": password}
        return _build_kamailio_auth_db_url(cfg, outputs)

    def test_at_sign_raises_with_doc_reference(self):
        with pytest.raises(RuntimeError) as exc:
            self._run_with_password("Bad@password")
        assert "cloudsql-credentials.md" in str(exc.value)

    def test_colon_raises_with_doc_reference(self):
        with pytest.raises(RuntimeError) as exc:
            self._run_with_password("Bad:password")
        assert "cloudsql-credentials.md" in str(exc.value)

    def test_slash_raises_with_doc_reference(self):
        with pytest.raises(RuntimeError) as exc:
            self._run_with_password("Bad/password")
        assert "cloudsql-credentials.md" in str(exc.value)


class TestWriteExtraVarsIncludesKamailioAuthDbUrl:
    """Integration between _build_kamailio_auth_db_url and _write_extra_vars."""

    def test_happy_path_writes_expected_url_key(self):
        cfg = _make_config(
            {
                "gcp_project_id": "test-proj",
                "region": "us-central1",
                "zone": "us-central1-a",
                "cloudsql_private_ip": FIXTURE_HOST,
            }
        )
        outputs = {
            "kamailio_internal_ips": [],
            "rtpengine_external_ips": [],
            "kamailio_external_lb_ip": "",
            "cloudsql_mysql_password_kamailioro": FIXTURE_PW,
        }
        path = _write_extra_vars(cfg, outputs)
        try:
            data = json.loads(path.read_text())
            assert data["kamailio_auth_db_url"] == EXPECTED_URL
        finally:
            if path.exists():
                os.unlink(path)

    def test_unsafe_password_propagates_runtime_error(self):
        cfg = _make_config(
            {
                "gcp_project_id": "test-proj",
                "region": "us-central1",
                "zone": "us-central1-a",
                "cloudsql_private_ip": FIXTURE_HOST,
            }
        )
        outputs = {
            "kamailio_internal_ips": [],
            "rtpengine_external_ips": [],
            "kamailio_external_lb_ip": "",
            "cloudsql_mysql_password_kamailioro": "Bad@password",
        }
        with pytest.raises(RuntimeError) as exc:
            _write_extra_vars(cfg, outputs)
        assert "cloudsql-credentials.md" in str(exc.value)


class TestExtraVarsFilePermissions:
    """Defends against a future refactor that swaps tempfile.mkstemp for a
    helper with default 0o644 permissions and forgets to chmod."""

    def test_extra_vars_file_is_0o600(self):
        cfg = _make_config(
            {
                "gcp_project_id": "test-proj",
                "region": "us-central1",
                "zone": "us-central1-a",
                "cloudsql_private_ip": FIXTURE_HOST,
            }
        )
        outputs = {
            "kamailio_internal_ips": [],
            "rtpengine_external_ips": [],
            "kamailio_external_lb_ip": "",
            "cloudsql_mysql_password_kamailioro": FIXTURE_PW,
        }
        path = _write_extra_vars(cfg, outputs)
        try:
            mode = stat.S_IMODE(path.stat().st_mode)
            assert mode == 0o600, oct(mode)
        finally:
            if path.exists():
                os.unlink(path)


class TestOperatorDocLinked:
    """The operator-facing cloudsql-credentials doc must be discoverable
    from a top-level operations index so operators do not have to grep the
    repo to find rotation procedures."""

    def test_cloudsql_credentials_doc_exists(self):
        doc = (
            Path(__file__).resolve().parents[1]
            / "docs"
            / "operations"
            / "cloudsql-credentials.md"
        )
        assert doc.is_file(), doc

    def test_cloudsql_credentials_linked_from_operations_index(self):
        index_candidates = [
            Path(__file__).resolve().parents[1] / "docs" / "operations" / "README.md",
            Path(__file__).resolve().parents[1] / "docs" / "README.md",
        ]
        found = False
        for index in index_candidates:
            if not index.is_file():
                continue
            text = index.read_text()
            if "cloudsql-credentials.md" in text:
                found = True
                break
        assert found, (
            "cloudsql-credentials.md is not linked from any of: "
            + ", ".join(str(p) for p in index_candidates)
        )
