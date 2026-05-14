"""PR-Z Phase A: state-aware certificate lifecycle orchestrator.

This module owns the audit / short-circuit / reissue decision logic for the
Kamailio TLS certs. It calls into ``scripts.tls_bootstrap`` for pure crypto
primitives (CA generation, leaf issuance) and mutates two dictionaries
provided by the caller:

  - ``secrets_dict``: the in-memory representation of the SOPS-sealed
    secrets file. The orchestrator only reads/writes the KAMAILIO_* keys
    declared in ``tls_bootstrap``.
  - ``cert_state``: the in-memory representation of the install state
    file's ``cert_state`` subtree (metadata only — no key material).

No file I/O is performed on the SOPS-sealed file or the state file. That
is the caller's responsibility. The orchestrator does read manual-mode
cert files when ``config.cert_mode == "manual"``.

Phase A scope: this module + its tests. Pipeline / CLI / ansible / wizard
hooks are deferred to later phases (see design §6.0).
"""

from __future__ import annotations

import base64
import binascii
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from scripts.tls_bootstrap import (
    KAMAILIO_CA_CERT_KEY,
    KAMAILIO_CA_KEY_KEY,
    KAMAILIO_PAIRS,
    _b64,
    _generate_ca,
    _issue_leaf_signed_by_ca,
)


# Short-circuit threshold (design §6.2): leaves must have more than 30 days
# of remaining validity to skip reissue.
RENEWAL_THRESHOLD_DAYS = 30


class CertLifecycleError(RuntimeError):
    """Raised when the orchestrator cannot proceed (manual-mode layout
    invalid, ACME (not supported), bogus cert_mode, etc.)."""


@dataclass
class CertLifecycleResult:
    """What ``seed_kamailio_certs`` did."""

    did_reissue: bool = False
    generated_keys: tuple[str, ...] = ()
    skipped_keys: tuple[str, ...] = ()
    mode: str = ""


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------

def _compute_san_list(domain: str) -> list[str]:
    """Return the pinned SAN list for the install (design §3.1).

    Authoritative SAN list = ``sip.<domain>`` + ``registrar.<domain>``. The
    ``DOMAIN_NAME_TRUNK`` is dispatcher-only and intentionally absent.
    """
    if not domain or not isinstance(domain, str):
        raise CertLifecycleError(
            "config.domain is required for cert provisioning"
        )
    return [f"sip.{domain}", f"registrar.{domain}"]


def _required_keys_for_mode(mode: str) -> list[str]:
    if mode == "self_signed":
        keys = [KAMAILIO_CA_CERT_KEY, KAMAILIO_CA_KEY_KEY]
    elif mode == "manual":
        keys = []
    else:
        raise CertLifecycleError(
            f"_required_keys_for_mode: unsupported mode {mode!r}"
        )
    for _prefix, cert_key, priv_key in KAMAILIO_PAIRS:
        keys.extend([cert_key, priv_key])
    return keys


def _try_decode_pem_cert(value: object) -> Optional[x509.Certificate]:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        raw = base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError):
        return None
    try:
        return x509.load_pem_x509_certificate(raw)
    except Exception:
        return None


def _try_decode_pem_privkey(value: object) -> Optional[object]:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        raw = base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError):
        return None
    try:
        return serialization.load_pem_private_key(raw, password=None)
    except Exception:
        return None


def _audit_secret_completeness(
    secrets_dict: dict, mode: str
) -> tuple[bool, list[str]]:
    """Return (ok, missing_or_malformed_keys) for the given mode.

    A key is considered "ok" iff it is present, a non-empty string, base64
    decodable, and parses as the expected PEM object (cert vs private key).
    """
    required = _required_keys_for_mode(mode)
    bad: list[str] = []
    for key in required:
        value = secrets_dict.get(key)
        if "CERT" in key:
            decoded = _try_decode_pem_cert(value)
        else:
            decoded = _try_decode_pem_privkey(value)
        if decoded is None:
            bad.append(key)
    return (not bad, bad)


# ---------------------------------------------------------------------------
# Manual-mode helpers
# ---------------------------------------------------------------------------

def _validate_manual_cert_dir(
    cert_manual_dir: str, san_list: list[str], now: datetime,
) -> dict[str, tuple[bytes, bytes, x509.Certificate]]:
    """Validate the manual-mode directory layout and return parsed PEMs.

    Returns a dict mapping ``san`` -> ``(fullchain_pem, privkey_pem, leaf_cert)``.

    Raises ``CertLifecycleError`` (with remediation hint) if:
      - any required subdirectory or file is missing,
      - any PEM fails to parse,
      - any leaf certificate is expired.
    """
    if not cert_manual_dir or not isinstance(cert_manual_dir, str):
        raise CertLifecycleError(
            "cert_mode=manual requires config.cert_manual_dir to be set "
            "to an absolute path containing per-SAN subdirectories"
        )
    root = Path(cert_manual_dir)
    if not root.is_dir():
        raise CertLifecycleError(
            f"cert_manual_dir {cert_manual_dir!r} does not exist or is "
            "not a directory. Expected layout: "
            f"{cert_manual_dir}/<san>/{{fullchain,privkey}}.pem"
        )

    out: dict[str, tuple[bytes, bytes, x509.Certificate]] = {}
    for san in san_list:
        san_dir = root / san
        fullchain = san_dir / "fullchain.pem"
        privkey = san_dir / "privkey.pem"
        if not san_dir.is_dir():
            raise CertLifecycleError(
                f"manual-mode subdirectory missing: {san_dir}. "
                f"Expected: {san_dir}/fullchain.pem and {san_dir}/privkey.pem"
            )
        if not fullchain.is_file():
            raise CertLifecycleError(
                f"manual-mode fullchain.pem missing: {fullchain}"
            )
        if not privkey.is_file():
            raise CertLifecycleError(
                f"manual-mode privkey.pem missing: {privkey}"
            )
        fullchain_bytes = fullchain.read_bytes()
        privkey_bytes = privkey.read_bytes()
        try:
            leaf = x509.load_pem_x509_certificate(fullchain_bytes)
        except Exception as exc:  # pragma: no cover - cryptography raises various
            raise CertLifecycleError(
                f"manual-mode fullchain.pem at {fullchain} is not valid PEM: {exc}"
            ) from exc
        try:
            serialization.load_pem_private_key(privkey_bytes, password=None)
        except Exception as exc:  # pragma: no cover
            raise CertLifecycleError(
                f"manual-mode privkey.pem at {privkey} is not a valid PEM "
                f"private key: {exc}"
            ) from exc
        not_after = _cert_not_after(leaf)
        if not_after <= now:
            raise CertLifecycleError(
                f"manual-mode cert at {fullchain} is expired "
                f"(not_after={not_after.isoformat()}). Renew or replace."
            )
        out[san] = (fullchain_bytes, privkey_bytes, leaf)
    return out


# ---------------------------------------------------------------------------
# Self-signed helpers
# ---------------------------------------------------------------------------

def _cert_not_after(cert: x509.Certificate) -> datetime:
    """Return a timezone-aware UTC not_after for ``cert``.

    Tries ``not_valid_after_utc`` (cryptography >=42) and falls back to
    ``not_valid_after`` for older versions.
    """
    try:
        return cert.not_valid_after_utc
    except AttributeError:  # pragma: no cover
        naive = cert.not_valid_after
        if naive.tzinfo is None:
            return naive.replace(tzinfo=timezone.utc)
        return naive


def _fingerprint_sha256(cert: x509.Certificate) -> str:
    raw = cert.fingerprint(hashes.SHA256())
    return ":".join(f"{b:02X}" for b in raw)


def _populate_state_self_signed(
    cert_state: dict,
    san_list: list[str],
    ca_cert_pem: bytes,
    leaf_certs: dict[str, x509.Certificate],
) -> None:
    ca = x509.load_pem_x509_certificate(ca_cert_pem)
    cert_state["schema_version"] = 1
    cert_state["config_mode"] = "self_signed"
    cert_state["actual_mode"] = "self_signed"
    cert_state["acme_pending"] = False
    cert_state["ca_subject"] = ca.subject.rfc4514_string()
    cert_state["ca_not_after"] = _cert_not_after(ca).isoformat()
    cert_state["ca_fingerprint_sha256"] = _fingerprint_sha256(ca)
    cert_state["san_list"] = list(san_list)
    leaves: dict = {}
    for san, cert in leaf_certs.items():
        leaves[san] = {
            "not_after": _cert_not_after(cert).isoformat(),
            "fingerprint_sha256": _fingerprint_sha256(cert),
            "serial": cert.serial_number,
        }
    cert_state["leaf_certs"] = leaves


def _populate_state_manual(
    cert_state: dict,
    san_list: list[str],
    leaf_certs: dict[str, x509.Certificate],
) -> None:
    cert_state["schema_version"] = 1
    cert_state["actual_mode"] = "manual"
    cert_state["config_mode"] = "manual"
    cert_state["acme_pending"] = False
    # Manual mode: CA fields explicitly absent / cleared.
    for k in ("ca_subject", "ca_not_after", "ca_fingerprint_sha256"):
        cert_state.pop(k, None)
    cert_state["san_list"] = list(san_list)
    leaves: dict = {}
    for san, cert in leaf_certs.items():
        leaves[san] = {
            "not_after": _cert_not_after(cert).isoformat(),
            "fingerprint_sha256": _fingerprint_sha256(cert),
            "serial": cert.serial_number,
        }
    cert_state["leaf_certs"] = leaves


def _state_short_circuit_ok(
    cert_state: dict,
    mode: str,
    san_list: list[str],
    now: datetime,
) -> bool:
    if cert_state.get("config_mode") != mode:
        return False
    if list(cert_state.get("san_list") or []) != list(san_list):
        return False
    leaves = cert_state.get("leaf_certs") or {}
    threshold = timedelta(days=RENEWAL_THRESHOLD_DAYS)
    for san in san_list:
        entry = leaves.get(san)
        if not isinstance(entry, dict):
            return False
        not_after_str = entry.get("not_after")
        if not isinstance(not_after_str, str):
            return False
        try:
            not_after = datetime.fromisoformat(
                not_after_str.replace("Z", "+00:00")
            )
        except ValueError:
            return False
        if not_after.tzinfo is None:
            not_after = not_after.replace(tzinfo=timezone.utc)
        if not_after - now <= threshold:
            return False
    # PR-Z D5/D6/D7 fix: in self_signed mode, the CA must ALSO have more
    # than ``RENEWAL_THRESHOLD_DAYS`` of remaining validity. If the CA is
    # about to expire, every leaf it signed is effectively about to lose
    # its trust chain on the wire — reissue the entire stack rather than
    # short-circuiting on still-fresh leaf timestamps. Manual mode has no
    # ca_not_after to check (CA is external).
    if mode == "self_signed":
        ca_not_after_str = cert_state.get("ca_not_after")
        if not isinstance(ca_not_after_str, str):
            return False
        try:
            ca_not_after = datetime.fromisoformat(
                ca_not_after_str.replace("Z", "+00:00")
            )
        except ValueError:
            return False
        if ca_not_after.tzinfo is None:
            ca_not_after = ca_not_after.replace(tzinfo=timezone.utc)
        if ca_not_after - now <= threshold:
            return False
    return True


def _ca_pair_from_secrets(secrets_dict: dict) -> tuple[bytes, bytes]:
    ca_cert_b64 = secrets_dict[KAMAILIO_CA_CERT_KEY]
    ca_key_b64 = secrets_dict[KAMAILIO_CA_KEY_KEY]
    return (
        base64.b64decode(ca_cert_b64),
        base64.b64decode(ca_key_b64),
    )


def _ca_still_valid(ca_cert_pem: bytes, now: datetime) -> bool:
    try:
        cert = x509.load_pem_x509_certificate(ca_cert_pem)
    except Exception:
        return False
    threshold = timedelta(days=RENEWAL_THRESHOLD_DAYS)
    return _cert_not_after(cert) - now > threshold


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def seed_kamailio_certs(
    secrets_dict: dict,
    cert_state: dict,
    config: dict,
    now: Optional[datetime] = None,
) -> CertLifecycleResult:
    """Orchestrate Kamailio cert provisioning. Mutates inputs in place.

    Inputs:
      - ``secrets_dict``: contents of the SOPS-sealed secrets file (dict).
      - ``cert_state``: ``cert_state`` subtree of the install state file (dict).
      - ``config``: must contain at least ``cert_mode`` and ``domain``;
        for manual mode also ``cert_manual_dir``.
      - ``now``: optional fixed clock for tests; defaults to ``datetime.now(UTC)``.

    Returns ``CertLifecycleResult``.

    Raises ``CertLifecycleError`` for: ACME mode (not supported), unknown mode,
    manual-mode layout/parse/expiry problems, missing domain.
    """
    if now is None:
        now = datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)

    mode = config.get("cert_mode") if isinstance(config, dict) else None
    if mode == "acme":
        raise CertLifecycleError(
            "cert_mode=acme is not supported. "
            "Use cert_mode=self_signed or cert_mode=manual. "
            "For CA-issued certs, see 'Obtaining TLS Certificates' in README.md."
        )
    if mode not in ("self_signed", "manual"):
        raise CertLifecycleError(
            f"cert_mode must be one of: self_signed, manual (got {mode!r})"
        )

    domain = config.get("domain", "")
    san_list = _compute_san_list(domain)

    if mode == "self_signed":
        return _seed_self_signed(secrets_dict, cert_state, san_list, now)
    return _seed_manual(secrets_dict, cert_state, san_list, config, now)


def _seed_self_signed(
    secrets_dict: dict,
    cert_state: dict,
    san_list: list[str],
    now: datetime,
) -> CertLifecycleResult:
    audit_ok, _bad = _audit_secret_completeness(secrets_dict, "self_signed")

    if audit_ok and _state_short_circuit_ok(
        cert_state, "self_signed", san_list, now
    ):
        return CertLifecycleResult(
            did_reissue=False,
            generated_keys=(),
            skipped_keys=tuple(_required_keys_for_mode("self_signed")),
            mode="self_signed",
        )

    generated: list[str] = []

    # Decide: leaf-only reissue (CA still valid + audit OK) vs full reissue.
    leaf_only = False
    if audit_ok:
        ca_cert_pem, ca_key_pem = _ca_pair_from_secrets(secrets_dict)
        if _ca_still_valid(ca_cert_pem, now):
            leaf_only = True

    if not leaf_only:
        ca_cert_pem, ca_key_pem = _generate_ca()
        secrets_dict[KAMAILIO_CA_CERT_KEY] = _b64(ca_cert_pem)
        secrets_dict[KAMAILIO_CA_KEY_KEY] = _b64(ca_key_pem)
        generated.extend([KAMAILIO_CA_CERT_KEY, KAMAILIO_CA_KEY_KEY])

    leaf_certs: dict[str, x509.Certificate] = {}
    for prefix, cert_key, priv_key in KAMAILIO_PAIRS:
        # Map prefix -> SAN by index in KAMAILIO_PAIRS, since san_list
        # ordering is sip then registrar (design §3.1).
        san_index = {"sip": 0, "registrar": 1}[prefix]
        san = san_list[san_index]
        wildcard = (prefix == "registrar")
        leaf_pem, leaf_key_pem = _issue_leaf_signed_by_ca(
            san=san,
            ca_cert_pem=ca_cert_pem,
            ca_key_pem=ca_key_pem,
            wildcard=wildcard,
        )
        secrets_dict[cert_key] = _b64(leaf_pem)
        secrets_dict[priv_key] = _b64(leaf_key_pem)
        generated.extend([cert_key, priv_key])
        leaf_certs[san] = x509.load_pem_x509_certificate(leaf_pem)

    _populate_state_self_signed(
        cert_state, san_list, ca_cert_pem, leaf_certs
    )

    return CertLifecycleResult(
        did_reissue=True,
        generated_keys=tuple(generated),
        skipped_keys=(),
        mode="self_signed",
    )


def _seed_manual(
    secrets_dict: dict,
    cert_state: dict,
    san_list: list[str],
    config: dict,
    now: datetime,
) -> CertLifecycleResult:
    cert_manual_dir = config.get("cert_manual_dir") if isinstance(config, dict) else None
    parsed = _validate_manual_cert_dir(cert_manual_dir, san_list, now)

    generated: list[str] = []
    leaf_certs: dict[str, x509.Certificate] = {}
    for prefix, cert_key, priv_key in KAMAILIO_PAIRS:
        san_index = {"sip": 0, "registrar": 1}[prefix]
        san = san_list[san_index]
        fullchain_pem, privkey_pem, leaf_cert = parsed[san]
        secrets_dict[cert_key] = _b64(fullchain_pem)
        secrets_dict[priv_key] = _b64(privkey_pem)
        generated.extend([cert_key, priv_key])
        leaf_certs[san] = leaf_cert

    # Manual mode: CA keys are NOT written. If the operator previously ran
    # self-signed and we are now in manual mode, the stale CA keys remain in
    # secrets but state correctly reflects actual_mode=manual; pipeline-level
    # cleanup is out of scope for Phase A.

    _populate_state_manual(cert_state, san_list, leaf_certs)

    return CertLifecycleResult(
        did_reissue=True,
        generated_keys=tuple(generated),
        skipped_keys=(),
        mode="manual",
    )
