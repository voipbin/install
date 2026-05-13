"""PR-Z Phase B tests: wizard cert_mode prompt."""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.gcp import ProjectListing


def _run_wizard_captured(monkeypatch, prompt_text_seq, prompt_choice_seq):
    """Run run_wizard with patched prompts; capture the resulting config dict.

    Returns the in-progress config dict observed at the moment Q7 (Cloud DNS)
    finishes — by then cert_mode/cert_manual_dir are populated.
    """
    from scripts import wizard as wiz_mod

    text_iter = iter(prompt_text_seq)
    choice_iter = iter(prompt_choice_seq)

    def fake_text(*a, **kw):
        try:
            return next(text_iter)
        except StopIteration:
            raise SystemExit("text exhausted")

    def fake_choice(*a, **kw):
        try:
            return next(choice_iter)
        except StopIteration:
            raise SystemExit("choice exhausted")

    monkeypatch.setattr(wiz_mod, "prompt_text", fake_text)
    monkeypatch.setattr(wiz_mod, "prompt_choice", fake_choice)
    monkeypatch.setattr(wiz_mod, "get_project_id", lambda: None)
    monkeypatch.setattr(wiz_mod, "list_active_projects", lambda: [])
    monkeypatch.setattr(wiz_mod, "console", _NullConsole())

    try:
        return wiz_mod.run_wizard(existing_config={})
    except SystemExit:
        return None


class _NullConsole:
    def print(self, *a, **kw):
        pass


# Without a project picker, the wizard prompts via text for Q1.
# Order of prompt_text:  proj_id, [region if custom], domain, [cert_manual_dir if manual]
# Order of prompt_choice: region, gke_type, tls, image_tag, cert_mode, dns_mode


class TestWizardCertModePrompt:
    def test_default_self_signed(self, monkeypatch):
        # cert_mode default index = 1 → self_signed
        cfg = _run_wizard_captured(
            monkeypatch,
            prompt_text_seq=["voipbin-test-1", "voipbin.example.com"],
            prompt_choice_seq=[1, 1, 1, 1, 1, 1],
        )
        assert cfg is not None
        assert cfg["cert_mode"] == "self_signed"
        assert cfg.get("cert_manual_dir") in (None, "")

    def test_manual_prompts_for_dir(self, monkeypatch):
        with tempfile.TemporaryDirectory() as td:
            cfg = _run_wizard_captured(
                monkeypatch,
                # 3 text prompts: project, domain, cert_manual_dir
                prompt_text_seq=["voipbin-test-1", "voipbin.example.com", td],
                # cert_mode = 2 = manual
                prompt_choice_seq=[1, 1, 1, 1, 2, 1],
            )
            assert cfg is not None
            assert cfg["cert_mode"] == "manual"
            assert cfg["cert_manual_dir"] == td

    def test_manual_validates_dir_exists(self, monkeypatch):
        # _validate_cert_manual_dir returns non-None for non-existent
        from scripts.wizard import _validate_cert_manual_dir
        err = _validate_cert_manual_dir("/nonexistent/path/zzz")
        assert err is not None
        assert "does not exist" in err.lower() or "Directory does not exist" in err

    def test_self_signed_written_to_config(self, monkeypatch, tmp_path):
        # Run wizard with self_signed, persist via InstallerConfig, re-load
        cfg = _run_wizard_captured(
            monkeypatch,
            prompt_text_seq=["voipbin-test-1", "voipbin.example.com"],
            prompt_choice_seq=[1, 1, 1, 1, 1, 1],
        )
        assert cfg is not None
        from scripts.config import InstallerConfig
        ic = InstallerConfig(config_dir=tmp_path)
        ic._data = dict(cfg)
        ic.save()
        # Re-load and verify cert_mode persisted
        ic2 = InstallerConfig(config_dir=tmp_path)
        ic2.load()
        assert ic2.get("cert_mode") == "self_signed"
