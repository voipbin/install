"""Tests for PR-K: state bucket bootstrap (GAP-32 hot-fix)."""

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.config import InstallerConfig
from scripts.state_bucket import (
    DEFAULT_ENV,
    ensure_state_bucket,
    state_bucket_name,
)


def _make_config(env: str | None = None, project: str = "voipbin-install-dev",
                 region: str = "us-central1") -> InstallerConfig:
    cfg = InstallerConfig()
    cfg.set("gcp_project_id", project)
    cfg.set("region", region)
    if env is not None:
        cfg.set("env", env)
    return cfg


class TestStateBucketName:
    def test_default_env_resolves_to_voipbin(self):
        cfg = _make_config()
        assert state_bucket_name(cfg) == "voipbin-install-dev-voipbin-tf-state"

    def test_default_env_constant_is_voipbin(self):
        assert DEFAULT_ENV == "voipbin"

    def test_explicit_non_default_env(self):
        cfg = _make_config(env="staging")
        assert state_bucket_name(cfg) == "voipbin-install-dev-staging-tf-state"

    def test_empty_env_falls_back_to_default(self):
        cfg = _make_config(env="")
        assert state_bucket_name(cfg) == "voipbin-install-dev-voipbin-tf-state"


class TestEnsureStateBucket:
    def test_idempotent_when_bucket_exists(self):
        cfg = _make_config()
        describe = MagicMock(returncode=0, stdout="", stderr="")
        update = MagicMock(returncode=0, stdout="", stderr="")
        with patch("scripts.state_bucket.run_cmd",
                   side_effect=[describe, update]) as mock_run:
            assert ensure_state_bucket(cfg) is True
        # describe + idempotent versioning update; no create.
        assert mock_run.call_count == 2
        args = mock_run.call_args_list[0].args[0]
        assert args[:4] == ["gcloud", "storage", "buckets", "describe"]
        update_args = mock_run.call_args_list[1].args[0]
        assert update_args[:4] == ["gcloud", "storage", "buckets", "update"]
        assert "--versioning" in update_args
        # no create call ever
        for call in mock_run.call_args_list:
            assert call.args[0][:4] != ["gcloud", "storage", "buckets", "create"]

    def test_creates_bucket_with_correct_flags(self):
        cfg = _make_config()
        describe = MagicMock(returncode=1, stdout="", stderr="not found")
        create = MagicMock(returncode=0, stdout="", stderr="")
        update = MagicMock(returncode=0, stdout="", stderr="")
        with patch("scripts.state_bucket.run_cmd",
                   side_effect=[describe, create, update]) as mock_run:
            assert ensure_state_bucket(cfg) is True
        assert mock_run.call_count == 3
        create_args = mock_run.call_args_list[1].args[0]
        assert create_args[:4] == ["gcloud", "storage", "buckets", "create"]
        assert "gs://voipbin-install-dev-voipbin-tf-state" in create_args
        assert "--project=voipbin-install-dev" in create_args
        assert "--location=us-central1" in create_args
        assert "--uniform-bucket-level-access" in create_args
        assert "--public-access-prevention" in create_args
        # Boolean flag in gcloud 472+ — must not be paired with a value.
        assert "--public-access-prevention=enforced" not in create_args

    def test_enables_versioning_after_create(self):
        cfg = _make_config()
        describe = MagicMock(returncode=1, stdout="", stderr="not found")
        create = MagicMock(returncode=0)
        update = MagicMock(returncode=0)
        with patch("scripts.state_bucket.run_cmd",
                   side_effect=[describe, create, update]) as mock_run:
            assert ensure_state_bucket(cfg) is True
        update_args = mock_run.call_args_list[2].args[0]
        assert update_args[:4] == ["gcloud", "storage", "buckets", "update"]
        assert "--versioning" in update_args

    def test_returns_false_when_create_fails(self):
        cfg = _make_config()
        describe = MagicMock(returncode=1, stdout="", stderr="not found")
        create = MagicMock(returncode=1, stdout="", stderr="permission denied")
        with patch("scripts.state_bucket.run_cmd",
                   side_effect=[describe, create]):
            assert ensure_state_bucket(cfg) is False

    def test_returns_false_when_versioning_fails(self):
        cfg = _make_config()
        describe = MagicMock(returncode=1)
        create = MagicMock(returncode=0)
        update = MagicMock(returncode=1, stderr="boom")
        with patch("scripts.state_bucket.run_cmd",
                   side_effect=[describe, create, update]):
            assert ensure_state_bucket(cfg) is False

    # ---- R2 review fixes ----

    def test_create_race_recovered_when_409(self):
        cfg = _make_config()
        describe = MagicMock(returncode=1, stdout="", stderr="not found")
        create = MagicMock(
            returncode=1, stdout="",
            stderr="HTTPError 409: AlreadyOwnedByYou",
        )
        recheck = MagicMock(returncode=0, stdout="", stderr="")
        update = MagicMock(returncode=0, stdout="", stderr="")
        with patch("scripts.state_bucket.run_cmd",
                   side_effect=[describe, create, recheck, update]) as mock_run:
            assert ensure_state_bucket(cfg) is True
        # describe, create, re-describe, versioning update
        assert mock_run.call_count == 4
        update_args = mock_run.call_args_list[3].args[0]
        assert update_args[:4] == ["gcloud", "storage", "buckets", "update"]
        assert "--versioning" in update_args

    def test_create_race_recovered_when_already_exists_substring(self):
        cfg = _make_config()
        describe = MagicMock(returncode=1, stderr="not found")
        create = MagicMock(returncode=1, stderr="bucket already exists")
        recheck = MagicMock(returncode=0)
        update = MagicMock(returncode=0)
        with patch("scripts.state_bucket.run_cmd",
                   side_effect=[describe, create, recheck, update]) as mock_run:
            assert ensure_state_bucket(cfg) is True
        assert mock_run.call_count == 4
        # versioning still applied
        update_args = mock_run.call_args_list[3].args[0]
        assert "--versioning" in update_args

    def test_create_failure_other_than_race_still_returns_false(self):
        cfg = _make_config()
        describe = MagicMock(returncode=1, stderr="not found")
        create = MagicMock(returncode=1, stderr="quota exceeded")
        with patch("scripts.state_bucket.run_cmd",
                   side_effect=[describe, create]) as mock_run:
            assert ensure_state_bucket(cfg) is False
        # describe + create only; no recheck, no update
        assert mock_run.call_count == 2

    def test_versioning_runs_on_existing_bucket_path(self):
        cfg = _make_config()
        describe = MagicMock(returncode=0, stderr="")
        update = MagicMock(returncode=0)
        with patch("scripts.state_bucket.run_cmd",
                   side_effect=[describe, update]) as mock_run:
            assert ensure_state_bucket(cfg) is True
        assert mock_run.call_count == 2
        update_args = mock_run.call_args_list[1].args[0]
        assert update_args[:4] == ["gcloud", "storage", "buckets", "update"]
        assert "--versioning" in update_args

    def test_versioning_runs_on_created_bucket_path(self):
        cfg = _make_config()
        describe = MagicMock(returncode=1, stderr="not found")
        create = MagicMock(returncode=0)
        update = MagicMock(returncode=0)
        with patch("scripts.state_bucket.run_cmd",
                   side_effect=[describe, create, update]) as mock_run:
            assert ensure_state_bucket(cfg) is True
        assert mock_run.call_count == 3
        update_args = mock_run.call_args_list[2].args[0]
        assert update_args[:4] == ["gcloud", "storage", "buckets", "update"]
        assert "--versioning" in update_args

    def test_iam_403_error_emits_hint_on_create(self):
        cfg = _make_config()
        describe = MagicMock(returncode=1, stderr="not found")
        create = MagicMock(
            returncode=1,
            stderr="HTTPError 403: PERMISSION_DENIED storage.buckets.create",
        )
        with patch("scripts.state_bucket.run_cmd",
                   side_effect=[describe, create]), \
             patch("scripts.state_bucket.print_error") as mock_err:
            assert ensure_state_bucket(cfg) is False
        messages = " | ".join(c.args[0] for c in mock_err.call_args_list)
        assert "roles/storage.admin" in messages

    def test_iam_403_error_emits_hint_on_describe(self):
        cfg = _make_config()
        # describe fails with 403 (not a 404), then create also fails with 403
        describe = MagicMock(
            returncode=1,
            stderr="HTTPError 403: does not have storage.buckets.get",
        )
        create = MagicMock(
            returncode=1,
            stderr="HTTPError 403: PERMISSION_DENIED",
        )
        with patch("scripts.state_bucket.run_cmd",
                   side_effect=[describe, create]), \
             patch("scripts.state_bucket.print_warning") as mock_warn, \
             patch("scripts.state_bucket.print_error") as mock_err:
            assert ensure_state_bucket(cfg) is False
        warn_msgs = " | ".join(c.args[0] for c in mock_warn.call_args_list)
        err_msgs = " | ".join(c.args[0] for c in mock_err.call_args_list)
        assert "roles/storage.admin" in (warn_msgs + " | " + err_msgs)


class TestTerraformInitWiring:
    def test_init_passes_both_bucket_and_prefix_backend_configs(self, tmp_path,
                                                                monkeypatch):
        from scripts import terraform as tf_mod

        cfg = _make_config()
        monkeypatch.setattr(tf_mod, "TFVARS_FILE", tmp_path / "terraform.tfvars.json")
        monkeypatch.setattr(tf_mod, "write_tfvars", lambda c: tmp_path / "x")
        monkeypatch.setattr(tf_mod, "ensure_state_bucket", lambda c: True)

        captured: dict = {}

        def fake_run_cmd(cmd, capture=False, timeout=0):
            captured["cmd"] = cmd
            return MagicMock(returncode=0)

        monkeypatch.setattr(tf_mod, "run_cmd", fake_run_cmd)
        assert tf_mod.terraform_init(cfg) is True
        cmd = captured["cmd"]
        assert any(a.startswith("-backend-config=bucket=") for a in cmd)
        assert any(a.startswith("-backend-config=prefix=") for a in cmd)
        bucket_arg = [a for a in cmd if a.startswith("-backend-config=bucket=")][0]
        assert bucket_arg == "-backend-config=bucket=voipbin-install-dev-voipbin-tf-state"

    def test_init_returns_false_when_bootstrap_fails(self, tmp_path, monkeypatch):
        from scripts import terraform as tf_mod

        cfg = _make_config()
        monkeypatch.setattr(tf_mod, "write_tfvars", lambda c: tmp_path / "x")
        monkeypatch.setattr(tf_mod, "ensure_state_bucket", lambda c: False)
        # run_cmd must not be called when bootstrap fails
        called = {"n": 0}

        def fake_run_cmd(*a, **kw):
            called["n"] += 1
            return MagicMock(returncode=0)

        monkeypatch.setattr(tf_mod, "run_cmd", fake_run_cmd)
        assert tf_mod.terraform_init(cfg) is False
        assert called["n"] == 0


class TestReconcileUsesHelper:
    def test_reconcile_state_bucket_entry_uses_state_bucket_name(self):
        from scripts import terraform_reconcile as tr

        cfg = _make_config(env="staging")
        # build_import_registry signature varies; locate via search of entries
        entries = tr.build_registry(cfg)
        state_entries = [e for e in entries
                         if e["tf_address"] == "google_storage_bucket.terraform_state"]
        assert len(state_entries) == 1
        e = state_entries[0]
        assert e["import_id"] == "voipbin-install-dev-staging-tf-state"
        assert "gs://voipbin-install-dev-staging-tf-state" in e["gcloud_check"]
