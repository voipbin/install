"""Tests for PR-E: config schema cleanup + preflight reposition.

Covers:
- check_cloudsql_private_ip no longer called from run_pre_apply_checks
- _run_k8s_apply now gates on the sentinel preflight (sentinel→False, real→passes)
- config/schema.py marks the field DEPRECATED
- reconcile_outputs respects operator override and overwrites sentinel
- preflight.warn_if_cloudsql_proxy_deployed removed
- init.py dry-run text uses dynamic count helper
- diagnosis.py no longer calls the obsolete warn helper
"""

from pathlib import Path
from unittest.mock import MagicMock, patch


REPO = Path(__file__).resolve().parent.parent


def _read(rel: str) -> str:
    return (REPO / rel).read_text()


def test_diagnosis_no_early_cloudsql_check():
    src = _read("scripts/diagnosis.py")
    # The early call site is gone; only references should be in comments (or none).
    # The actual function invocation (call form) must not appear.
    assert "check_cloudsql_private_ip(config)" not in src


def test_pipeline_k8s_apply_has_sentinel_guard():
    cfg = MagicMock()
    cfg.get.side_effect = lambda k, *a: {
        "cloudsql_private_ip": "cloudsql-private.invalid",
    }.get(k, a[0] if a else None)

    from scripts.pipeline import _run_k8s_apply
    with patch("scripts.pipeline.k8s_apply") as mock_apply, \
         patch("scripts.pipeline.k8s_dry_run") as mock_dry:
        result = _run_k8s_apply(cfg, {}, dry_run=False, auto_approve=True)
    assert result is False
    mock_apply.assert_not_called()
    mock_dry.assert_not_called()


def test_pipeline_k8s_apply_passes_with_real_ip():
    cfg = MagicMock()
    cfg.get.side_effect = lambda k, *a: {
        "cloudsql_private_ip": "10.42.0.7",
    }.get(k, a[0] if a else None)

    from scripts.pipeline import _run_k8s_apply
    with patch("scripts.pipeline.k8s_apply", return_value=True) as mock_apply:
        result = _run_k8s_apply(cfg, {}, dry_run=False, auto_approve=True)
    assert result is True
    mock_apply.assert_called_once()


def test_schema_cloudsql_private_ip_marked_deprecated():
    from config.schema import CONFIG_SCHEMA
    entry = CONFIG_SCHEMA["properties"]["cloudsql_private_ip"]
    assert "DEPRECATED" in entry["description"]
    assert "cloudsql_private_ip" not in CONFIG_SCHEMA.get("required", [])


def test_reconcile_outputs_overwrites_stale_operator_value():
    """Terraform value is authoritative — any pre-existing value is overwritten."""
    from scripts.terraform_reconcile import outputs

    cfg = MagicMock()
    store = {"cloudsql_private_ip": "10.99.99.99"}
    cfg.get.side_effect = lambda k, *a: store.get(k, a[0] if a else None)
    def _set(k, v):
        store[k] = v
    cfg.set.side_effect = _set

    tf = {
        "cloudsql_mysql_private_ip": "10.0.0.5",
        "cloudsql_mysql_private_ip_cidr": "10.0.0.5/32",
    }
    outputs(cfg, tf)
    assert store["cloudsql_private_ip"] == "10.0.0.5"


def test_reconcile_outputs_overwrites_sentinel():
    """Stale sentinel must be replaced by Terraform output."""
    from scripts.preflight import CLOUDSQL_PRIVATE_IP_SENTINEL
    from scripts.terraform_reconcile import outputs

    cfg = MagicMock()
    store = {"cloudsql_private_ip": CLOUDSQL_PRIVATE_IP_SENTINEL}
    cfg.get.side_effect = lambda k, *a: store.get(k, a[0] if a else None)
    def _set(k, v):
        store[k] = v
    cfg.set.side_effect = _set

    tf = {
        "cloudsql_mysql_private_ip": "10.0.0.5",
        "cloudsql_mysql_private_ip_cidr": "10.0.0.5/32",
    }
    outputs(cfg, tf)
    assert store["cloudsql_private_ip"] == "10.0.0.5"


def test_warn_if_cloudsql_proxy_deployed_removed():
    import scripts.preflight as p
    assert not hasattr(p, "warn_if_cloudsql_proxy_deployed")


def test_init_dry_run_text_truthful():
    src = _read("scripts/commands/init.py")
    assert "Generate 6 secrets" not in src
    assert "_count_secrets()" in src


def test_diagnosis_no_warn_call():
    src = _read("scripts/diagnosis.py")
    assert "warn_if_cloudsql_proxy_deployed" not in src
