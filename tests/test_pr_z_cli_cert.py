"""PR-Z Phase B tests: CLI `voipbin-install cert ...` subcommands."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts import pipeline as pl
from scripts.commands import cert as cert_cmd
from scripts.commands.cert import (
    cmd_cert_clean_staging,
    cmd_cert_renew,
    cmd_cert_status,
)


SAN_LIST = ["sip.example.com", "registrar.example.com"]


def _seed_state(tmp_path, monkeypatch, **cert_state_overrides):
    monkeypatch.setattr(pl, "INSTALLER_DIR", tmp_path)
    monkeypatch.setattr(pl, "STATE_FILE", tmp_path / ".voipbin-state.yaml")
    monkeypatch.setattr(cert_cmd, "INSTALLER_DIR", tmp_path)
    cs = {
        "config_mode": "self_signed",
        "actual_mode": "self_signed",
        "san_list": SAN_LIST,
        "ca_fingerprint_sha256": "AB:CD",
        "ca_not_after": "2036-01-01T00:00:00+00:00",
        "leaf_certs": {
            "sip.example.com": {
                "not_after": "2099-01-01T00:00:00+00:00",
                "fingerprint_sha256": "11:22",
                "serial": 1,
            },
            "registrar.example.com": {
                "not_after": "2099-01-01T00:00:00+00:00",
                "fingerprint_sha256": "33:44",
                "serial": 2,
            },
        },
    }
    cs.update(cert_state_overrides)
    pl.save_state({"cert_state": cs})


class TestCertCliCommands:
    def test_status_per_san_expiry(self, tmp_path, monkeypatch, capsys):
        _seed_state(tmp_path, monkeypatch)
        rc = cmd_cert_status(as_json=False)
        assert rc == 0
        out = capsys.readouterr().out
        assert "sip.example.com" in out
        assert "registrar.example.com" in out
        assert "2099" in out

    def test_status_json_valid(self, tmp_path, monkeypatch, capsys):
        _seed_state(tmp_path, monkeypatch)
        rc = cmd_cert_status(as_json=True)
        assert rc == 0
        out = capsys.readouterr().out
        payload = json.loads(out)
        assert payload["actual_mode"] == "self_signed"
        assert set(payload["san_list"]) == set(SAN_LIST)
        assert "ca_fingerprint_sha256" in payload

    def test_renew_noop_when_leaves_fresh(self, tmp_path, monkeypatch):
        _seed_state(tmp_path, monkeypatch)
        # Provide a fake config.yaml so InstallerConfig.exists() is True.
        (tmp_path / "config.yaml").write_text(
            "gcp_project_id: voipbin-test-1\n"
            "region: us-central1\n"
            "domain: example.com\n"
            "cert_mode: self_signed\n"
        )
        monkeypatch.chdir(tmp_path)
        called = {}

        def fake_run_pipeline(**kwargs):
            called["stage"] = kwargs.get("only_stage")
            # No-op: state already valid; would short-circuit in real call.
            return True

        monkeypatch.setattr(
            "scripts.pipeline.run_pipeline", fake_run_pipeline
        )
        rc = cmd_cert_renew(force=False)
        assert rc == 0
        assert called["stage"] == "cert_provision"

    def test_renew_force_clears_state_leaf_certs(self, tmp_path, monkeypatch):
        _seed_state(tmp_path, monkeypatch)
        (tmp_path / "config.yaml").write_text(
            "gcp_project_id: voipbin-test-1\n"
            "region: us-central1\n"
            "domain: example.com\n"
            "cert_mode: self_signed\n"
        )
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr(
            "scripts.pipeline.run_pipeline",
            lambda **kw: True,
        )
        rc = cmd_cert_renew(force=True)
        assert rc == 0
        st = pl.load_state()
        cs = st.get("cert_state") or {}
        # leaf_certs cleared by --force
        assert "leaf_certs" not in cs

    def test_clean_staging_removes_dir(self, tmp_path, monkeypatch):
        monkeypatch.setattr(cert_cmd, "INSTALLER_DIR", tmp_path)
        staging = tmp_path / pl.CERT_STAGING_DIRNAME
        staging.mkdir()
        (staging / "marker").write_text("x")
        rc = cmd_cert_clean_staging()
        assert rc == 0
        assert not staging.exists()

    def test_renew_remediation_hint_when_no_config(
        self, tmp_path, monkeypatch, capsys
    ):
        monkeypatch.setattr(pl, "INSTALLER_DIR", tmp_path)
        monkeypatch.setattr(pl, "STATE_FILE", tmp_path / ".voipbin-state.yaml")
        monkeypatch.setattr(cert_cmd, "INSTALLER_DIR", tmp_path)
        monkeypatch.chdir(tmp_path)
        rc = cmd_cert_renew(force=False)
        assert rc == 1
        out = capsys.readouterr().out
        # Remediation hint mentions init
        assert "init" in out.lower()
