"""Tests for PR-G: GCS storage Terraform module + k8s.py fallback fix.

Covers:
  - terraform/storage.tf structure (recordings + tmp buckets, security flags).
  - terraform/outputs.tf exposes bucket-name outputs.
  - scripts/terraform_reconcile.build_registry includes both buckets.
  - scripts/terraform_reconcile.FIELD_MAP populates config.yaml from outputs.
  - scripts/k8s._build_substitution_map uses new TF output keys with no
    silent literal fallback.
  - config/schema.py accepts the new optional fields.
"""

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from scripts import terraform_reconcile
from scripts.k8s import _build_substitution_map


INSTALLER_DIR = Path(__file__).resolve().parents[1]
STORAGE_TF = (INSTALLER_DIR / "terraform" / "storage.tf").read_text()
OUTPUTS_TF = (INSTALLER_DIR / "terraform" / "outputs.tf").read_text()


# ---------------------------------------------------------------------------
# 1. storage.tf structure
# ---------------------------------------------------------------------------

class TestStorageTf:
    def test_recordings_bucket_defined(self):
        assert 'resource "google_storage_bucket" "recordings"' in STORAGE_TF
        assert '"${var.env}-voipbin-recordings"' in STORAGE_TF

    def test_tmp_bucket_defined(self):
        assert 'resource "google_storage_bucket" "tmp"' in STORAGE_TF
        assert '"${var.env}-voipbin-tmp"' in STORAGE_TF

    def test_recordings_security_defaults(self):
        # Slice out the recordings block specifically.
        idx = STORAGE_TF.index('resource "google_storage_bucket" "recordings"')
        block = STORAGE_TF[idx:STORAGE_TF.index("\n}\n", idx)]
        assert "uniform_bucket_level_access = true" in block
        assert 'public_access_prevention    = "enforced"' in block
        assert "force_destroy               = false" in block
        # Versioning enabled on recordings.
        assert "versioning" in block
        assert "enabled = true" in block

    def test_tmp_security_defaults_and_lifecycle(self):
        idx = STORAGE_TF.index('resource "google_storage_bucket" "tmp"')
        block = STORAGE_TF[idx:]
        # uniform IAM + public access prevention
        assert "uniform_bucket_level_access = true" in block
        assert 'public_access_prevention    = "enforced"' in block
        # versioning explicitly disabled
        assert "enabled = false" in block
        # 7-day lifecycle delete
        assert "age = 7" in block
        assert 'type = "Delete"' in block

    def test_recordings_has_no_lifecycle_rule(self):
        """Defensive: recordings bucket must NEVER auto-delete objects."""
        idx = STORAGE_TF.index('resource "google_storage_bucket" "recordings"')
        block = STORAGE_TF[idx:STORAGE_TF.index("\n}\n", idx)]
        assert "lifecycle_rule" not in block

    def test_force_destroy_flags(self):
        """recordings=false (prevents data loss); tmp=true (allows teardown)."""
        rec_idx = STORAGE_TF.index('resource "google_storage_bucket" "recordings"')
        rec_block = STORAGE_TF[rec_idx:STORAGE_TF.index("\n}\n", rec_idx)]
        assert "force_destroy               = false" in rec_block
        tmp_idx = STORAGE_TF.index('resource "google_storage_bucket" "tmp"')
        tmp_block = STORAGE_TF[tmp_idx:STORAGE_TF.index("\n}\n", tmp_idx) if "\n}\n" in STORAGE_TF[tmp_idx:] else len(STORAGE_TF)]
        assert "force_destroy               = true" in tmp_block


# ---------------------------------------------------------------------------
# 2. outputs.tf exposes bucket names
# ---------------------------------------------------------------------------

class TestOutputsTf:
    def test_recordings_bucket_output(self):
        assert 'output "recordings_bucket_name"' in OUTPUTS_TF
        assert "google_storage_bucket.recordings.name" in OUTPUTS_TF

    def test_tmp_bucket_output(self):
        assert 'output "tmp_bucket_name"' in OUTPUTS_TF
        assert "google_storage_bucket.tmp.name" in OUTPUTS_TF


# ---------------------------------------------------------------------------
# 3. reconcile registry includes recordings + tmp buckets
# ---------------------------------------------------------------------------

class _FakeCfg:
    def __init__(self, data):
        self._d = data

    def get(self, key, default=None):
        return self._d.get(key, default)


class TestBuildRegistry:
    def _make_cfg(self):
        return _FakeCfg({
            "gcp_project_id": "my-project",
            "region": "us-central1",
            "zone": "us-central1-a",
            "env": "dev",
            "kamailio_count": 1,
            "rtpengine_count": 1,
        })

    def test_recordings_entry(self):
        entries = terraform_reconcile.build_registry(self._make_cfg())
        rec = next(e for e in entries if e["tf_address"] == "google_storage_bucket.recordings")
        assert rec["import_id"] == "dev-voipbin-recordings"
        assert "gs://dev-voipbin-recordings" in rec["gcloud_check"]

    def test_tmp_entry(self):
        entries = terraform_reconcile.build_registry(self._make_cfg())
        tmp = next(e for e in entries if e["tf_address"] == "google_storage_bucket.tmp")
        assert tmp["import_id"] == "dev-voipbin-tmp"
        assert "gs://dev-voipbin-tmp" in tmp["gcloud_check"]


# ---------------------------------------------------------------------------
# 4. FIELD_MAP entries populate config.yaml from outputs
# ---------------------------------------------------------------------------

class TestFieldMap:
    def test_field_map_has_both_buckets(self):
        keys = {(m.tf_key, m.cfg_key) for m in terraform_reconcile.FIELD_MAP}
        assert ("recordings_bucket_name", "recordings_bucket") in keys
        assert ("tmp_bucket_name", "tmp_bucket") in keys

    def test_outputs_populates_config(self, monkeypatch):
        config = MagicMock()
        # Both slots empty in config.yaml.
        config.get.return_value = None
        ok = terraform_reconcile.outputs(config, {
            "recordings_bucket_name": "r-bkt",
            "tmp_bucket_name": "t-bkt",
        })
        assert ok is True
        config.set.assert_any_call("recordings_bucket", "r-bkt")
        config.set.assert_any_call("tmp_bucket", "t-bkt")
        config.save.assert_called_once()

    def test_outputs_overwrites_stale_operator_set(self, monkeypatch):
        config = MagicMock()
        # Already populated with a different value → outputs() MUST overwrite (TF is authoritative).
        config.get.return_value = "operator-bucket"
        ok = terraform_reconcile.outputs(config, {
            "recordings_bucket_name": "r-bkt",
            "tmp_bucket_name": "t-bkt",
        })
        assert ok is True
        config.set.assert_called()

    def test_invalid_bucket_name_rejected(self):
        """Validator rejects malformed bucket names — operator slot stays untouched."""
        config = MagicMock()
        config.get.return_value = None
        ok = terraform_reconcile.outputs(config, {
            "recordings_bucket_name": "Bad_Name!",   # uppercase + bang
            "tmp_bucket_name": "x",                  # too short (<3 chars)
        })
        assert ok is True
        config.set.assert_not_called()

    def test_bucket_name_validator(self):
        valid = terraform_reconcile._is_valid_bucket_name
        assert valid("dev-voipbin-recordings")
        assert valid("a1b")
        assert not valid("AB-upper")
        assert not valid("ab")        # too short
        assert not valid("-leading")
        assert not valid("trailing-")
        assert not valid(123)
        assert not valid(None)


# ---------------------------------------------------------------------------
# 5. k8s.py substitution map — TF outputs authoritative, no silent literal
# ---------------------------------------------------------------------------

class TestK8sSubstitution:
    def _cfg(self, **overrides):
        base = {
            "domain": "voipbin.example.com",
            "gcp_project_id": "my-project",
            "region": "us-central1",
        }
        base.update(overrides)
        return _FakeCfg(base)

    def test_tf_outputs_win(self):
        subs = _build_substitution_map(
            self._cfg(),
            {"recordings_bucket_name": "abc", "tmp_bucket_name": "xyz"},
            {},
        )
        assert subs["PLACEHOLDER_RECORDINGS_BUCKET"] == "abc"
        assert subs["PLACEHOLDER_TMP_BUCKET"] == "xyz"

    def test_config_fallback(self):
        # No TF outputs, but config.yaml has values (FIELD_MAP populated).
        subs = _build_substitution_map(
            self._cfg(recordings_bucket="cfg-rec", tmp_bucket="cfg-tmp"),
            {},
            {},
        )
        assert subs["PLACEHOLDER_RECORDINGS_BUCKET"] == "cfg-rec"
        assert subs["PLACEHOLDER_TMP_BUCKET"] == "cfg-tmp"

    def test_no_silent_literal_fallback(self):
        # Both empty → empty string (NOT a derived literal like "{project}-voipbin-recordings").
        subs = _build_substitution_map(self._cfg(), {}, {})
        assert subs["PLACEHOLDER_RECORDINGS_BUCKET"] == ""
        assert subs["PLACEHOLDER_TMP_BUCKET"] == ""
        # Legacy token must be gone.
        assert "PLACEHOLDER_RECORDING_BUCKET_NAME" not in subs


# ---------------------------------------------------------------------------
# 6. config/schema.py adds the new optional fields
# ---------------------------------------------------------------------------

class TestConfigSchema:
    def test_schema_declares_buckets(self):
        from config.schema import CONFIG_SCHEMA
        props = CONFIG_SCHEMA["properties"]
        assert "recordings_bucket" in props
        assert "tmp_bucket" in props
        assert props["recordings_bucket"]["type"] == "string"
        assert props["tmp_bucket"]["type"] == "string"

    def test_schema_still_strict(self):
        from config.schema import CONFIG_SCHEMA
        # additionalProperties must remain False to keep config.yaml strict.
        assert CONFIG_SCHEMA["additionalProperties"] is False
