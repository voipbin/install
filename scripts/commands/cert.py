"""PR-Z Phase B: `voipbin-install cert` subcommands.

Three subcommands:
  - ``cert status``          show per-SAN expiry / mode / CA fingerprint.
  - ``cert renew [--force]`` re-run cert_provision; --force clears state so
                              the short-circuit doesn't kick in.
  - ``cert clean-staging``    remove ``<workdir>/.cert-staging/`` if present.
"""

from __future__ import annotations

import json as _json
import sys
from pathlib import Path
from typing import Any

from scripts.config import InstallerConfig
from scripts.display import (
    console,
    print_error,
    print_header,
    print_step,
    print_success,
    print_warning,
)
from scripts.pipeline import (
    CERT_STAGING_DIRNAME,
    cleanup_cert_staging,
    load_state,
    save_state,
)
from scripts.utils import INSTALLER_DIR


def cmd_cert_status(as_json: bool = False) -> int:
    """Print certificate state for all pinned SANs."""
    state = load_state()
    cert_state = state.get("cert_state") or {}
    config = InstallerConfig()
    if config.exists():
        config.load()

    payload: dict[str, Any] = {
        "config_mode": cert_state.get("config_mode")
        or config.get("cert_mode", "self_signed"),
        "actual_mode": cert_state.get("actual_mode"),
        "san_list": list(cert_state.get("san_list") or []),
        "leaf_certs": dict(cert_state.get("leaf_certs") or {}),
    }
    if (cert_state.get("actual_mode") or "") == "self_signed":
        payload["ca_fingerprint_sha256"] = cert_state.get("ca_fingerprint_sha256")
        payload["ca_not_after"] = cert_state.get("ca_not_after")

    if as_json:
        console.print(_json.dumps(payload, indent=2, default=str))
        return 0

    print_header("Kamailio TLS Certificate Status")
    print_step(f"Config mode:  {payload['config_mode']}")
    actual_mode = payload["actual_mode"] or "(not provisioned yet)"
    print_step(f"Actual mode:  {actual_mode}")
    if payload.get("ca_fingerprint_sha256"):
        print_step(f"CA fingerprint (SHA256): {payload['ca_fingerprint_sha256']}")
        print_step(f"CA not_after: {payload.get('ca_not_after', '?')}")
    if not payload["san_list"]:
        print_warning("No SAN entries yet — run `voipbin-install apply` first.")
        return 0
    print_step("Leaf certificates:")
    for san in payload["san_list"]:
        entry = payload["leaf_certs"].get(san) or {}
        not_after = entry.get("not_after", "?")
        fp = entry.get("fingerprint_sha256", "?")
        print_step(f"  {san}: not_after={not_after}  fp={fp}")
    return 0


def cmd_cert_renew(force: bool = False) -> int:
    """Re-run the cert_provision stage.

    With ``--force``, clear ``state.yaml.cert_state.leaf_certs`` first so the
    short-circuit cannot fire on still-valid leaves.
    """
    from scripts.pipeline import run_pipeline

    config = InstallerConfig()
    if not config.exists():
        print_error("No configuration found. Run `voipbin-install init` first.")
        return 1
    config.load()

    if force:
        state = load_state()
        cert_state = dict(state.get("cert_state") or {})
        cert_state.pop("leaf_certs", None)
        state["cert_state"] = cert_state
        save_state(state)
        print_warning("--force: cleared cert_state.leaf_certs in state.yaml")

    ok = run_pipeline(
        config=config,
        dry_run=False,
        auto_approve=True,
        only_stage="cert_provision",
    )
    if not ok:
        print_error("cert_provision stage failed.")
        return 1
    print_success("cert_provision complete.")
    return 0


def cmd_cert_clean_staging() -> int:
    staging = INSTALLER_DIR / CERT_STAGING_DIRNAME
    if not staging.exists():
        print_step("No cert-staging directory present — nothing to remove.")
        return 0
    cleanup_cert_staging(INSTALLER_DIR)
    if staging.exists():  # pragma: no cover - cleanup helper swallows errors
        print_error("cert-staging directory still present after cleanup.")
        return 1
    print_success("cert-staging removed.")
    return 0
