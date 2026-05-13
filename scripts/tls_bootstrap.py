"""TLS bootstrap — pure crypto primitives (PR #4 + PR-Z Phase A).

This module is a **pure crypto module**. It generates:

  - JWT_KEY (64-char random hex), and
  - Four base64-encoded PEM strings for the API/Hook self-signed pairs
    (SSL_CERT_API_BASE64, SSL_PRIVKEY_API_BASE64, SSL_CERT_HOOK_BASE64,
    SSL_PRIVKEY_HOOK_BASE64), plus
  - (PR-Z) a self-signed CA pair and per-SAN leaf certs for Kamailio's
    sip + registrar listeners.

State-aware orchestration (audit, short-circuit, reissue decision, dict
mutation across runs) lives in ``scripts/cert_lifecycle.py``. This module
never touches the SOPS sealed secrets file, the install state file, or
the on-disk staging directory.

Idempotency (design §4.8) for the legacy API/Hook seeding flow:
  - JWT_KEY: skip if present, generate if absent.
  - SSL_*_BASE64: api-cert pair and hook-cert pair are independent units.
    * Both keys of a pair present -> skip that pair.
    * Both keys of a pair absent  -> regenerate the pair.
    * Exactly one key of a pair present (corrupt-half) -> raise
      BootstrapError; do not silently regenerate.
"""

from __future__ import annotations

import base64
import secrets as _secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID


DEFAULT_VALID_DAYS = 3650
CN_PLACEHOLDER = "voipbin-self-signed"

SSL_PAIRS: tuple[tuple[str, str, str], ...] = (
    ("api", "SSL_CERT_API_BASE64", "SSL_PRIVKEY_API_BASE64"),
    ("hook", "SSL_CERT_HOOK_BASE64", "SSL_PRIVKEY_HOOK_BASE64"),
)

# PR-Z: Kamailio SAN pin (design §3.1). Authoritative SAN list is exactly
# sip.<domain> + registrar.<domain>. DOMAIN_NAME_TRUNK is dispatcher-routing
# only and is NOT a TLS server_name; do not add it here.
KAMAILIO_PAIRS: tuple[tuple[str, str, str], ...] = (
    ("sip", "KAMAILIO_CERT_SIP_BASE64", "KAMAILIO_PRIVKEY_SIP_BASE64"),
    ("registrar", "KAMAILIO_CERT_REGISTRAR_BASE64",
     "KAMAILIO_PRIVKEY_REGISTRAR_BASE64"),
)

KAMAILIO_CA_CERT_KEY = "KAMAILIO_CA_CERT_BASE64"
KAMAILIO_CA_KEY_KEY = "KAMAILIO_CA_KEY_BASE64"

# PR-Z: CA defaults — 10 years for the install CA, 365d for leaves
# (design §5 D7 revised).
DEFAULT_CA_VALID_DAYS = 3650
DEFAULT_LEAF_VALID_DAYS = 365
CA_COMMON_NAME = "VoIPBin Install CA"


class BootstrapError(RuntimeError):
    """Raised when the SOPS-sealed secrets file is in a corrupt half-state."""


@dataclass(frozen=True)
class BootstrapResult:
    """What ``run`` did. ``generated`` lists keys we just wrote."""

    generated: tuple[str, ...] = ()
    skipped: tuple[str, ...] = ()


def _generate_self_signed_pair(
    hostname: str, valid_days: int = DEFAULT_VALID_DAYS,
) -> tuple[bytes, bytes]:
    """Return (cert_pem, key_pem) for a single-host self-signed RSA-2048 cert."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = issuer = x509.Name(
        [x509.NameAttribute(NameOID.COMMON_NAME, CN_PLACEHOLDER)]
    )
    now = datetime.now(timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=5))
        .not_valid_after(now + timedelta(days=valid_days))
        .add_extension(
            x509.SubjectAlternativeName([x509.DNSName(hostname)]),
            critical=False,
        )
        .add_extension(
            x509.BasicConstraints(ca=False, path_length=None), critical=True
        )
        .sign(private_key=key, algorithm=hashes.SHA256())
    )
    cert_pem = cert.public_bytes(serialization.Encoding.PEM)
    key_pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    return cert_pem, key_pem


def _generate_ca(
    common_name: str = CA_COMMON_NAME,
    validity_days: int = DEFAULT_CA_VALID_DAYS,
) -> tuple[bytes, bytes]:
    """Generate a self-signed RSA-2048 CA suitable for issuing leaf certs.

    Pure crypto: no I/O, no dict mutation. Returns (ca_cert_pem, ca_key_pem).

    The CA has:
      - BasicConstraints CA=True, path_length=0 (cannot issue sub-CAs)
      - KeyUsage keyCertSign + cRLSign (digital_signature for cert signing)
      - SubjectKeyIdentifier derived from the public key
    """
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = issuer = x509.Name(
        [
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, "VoIPBin"),
            x509.NameAttribute(NameOID.COMMON_NAME, common_name),
        ]
    )
    now = datetime.now(timezone.utc)
    ski = x509.SubjectKeyIdentifier.from_public_key(key.public_key())
    builder = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=5))
        .not_valid_after(now + timedelta(days=validity_days))
        .add_extension(
            x509.BasicConstraints(ca=True, path_length=0), critical=True
        )
        .add_extension(
            x509.KeyUsage(
                digital_signature=False,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=True,
                crl_sign=True,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(ski, critical=False)
    )
    cert = builder.sign(private_key=key, algorithm=hashes.SHA256())
    cert_pem = cert.public_bytes(serialization.Encoding.PEM)
    key_pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    return cert_pem, key_pem


def _issue_leaf_signed_by_ca(
    san: str,
    ca_cert_pem: bytes,
    ca_key_pem: bytes,
    validity_days: int = DEFAULT_LEAF_VALID_DAYS,
    wildcard: bool = False,
) -> tuple[bytes, bytes]:
    """Issue a leaf certificate signed by the supplied CA.

    Pure crypto: no I/O, no dict mutation. Returns (leaf_pem, leaf_key_pem).

    - Subject CN = san
    - SAN extension contains [san] (and additionally ``*.{san}`` when
      ``wildcard=True``)
    - ExtendedKeyUsage = serverAuth
    - AuthorityKeyIdentifier matches the CA's SubjectKeyIdentifier
    - KeyUsage digital_signature + key_encipherment
    - BasicConstraints CA=False
    """
    ca_cert = x509.load_pem_x509_certificate(ca_cert_pem)
    ca_key = serialization.load_pem_private_key(ca_key_pem, password=None)
    if not isinstance(ca_key, rsa.RSAPrivateKey):
        raise ValueError("CA private key must be RSA")

    leaf_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = x509.Name(
        [x509.NameAttribute(NameOID.COMMON_NAME, san)]
    )
    now = datetime.now(timezone.utc)

    dns_names: list[x509.GeneralName] = [x509.DNSName(san)]
    if wildcard:
        dns_names.append(x509.DNSName(f"*.{san}"))

    # AKI must reference the CA's SKI (per RFC 5280 §4.2.1.1).
    try:
        ca_ski_ext = ca_cert.extensions.get_extension_for_class(
            x509.SubjectKeyIdentifier
        )
        aki = x509.AuthorityKeyIdentifier.from_issuer_subject_key_identifier(
            ca_ski_ext.value
        )
    except x509.ExtensionNotFound:
        aki = x509.AuthorityKeyIdentifier.from_issuer_public_key(
            ca_cert.public_key()
        )

    builder = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(ca_cert.subject)
        .public_key(leaf_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=5))
        .not_valid_after(now + timedelta(days=validity_days))
        .add_extension(
            x509.SubjectAlternativeName(dns_names), critical=False
        )
        .add_extension(
            x509.BasicConstraints(ca=False, path_length=None), critical=True
        )
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                content_commitment=False,
                key_encipherment=True,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=False,
                crl_sign=False,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(
            x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]),
            critical=False,
        )
        .add_extension(
            x509.SubjectKeyIdentifier.from_public_key(leaf_key.public_key()),
            critical=False,
        )
        .add_extension(aki, critical=False)
    )
    leaf_cert = builder.sign(private_key=ca_key, algorithm=hashes.SHA256())
    leaf_pem = leaf_cert.public_bytes(serialization.Encoding.PEM)
    leaf_key_pem = leaf_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    return leaf_pem, leaf_key_pem


def _b64(pem: bytes) -> str:
    return base64.b64encode(pem).decode("ascii")


def _present(value: object) -> bool:
    """A value counts as 'present' if it is a non-empty string."""
    return isinstance(value, str) and value.strip() != ""


def seed_secrets_yaml(
    secrets_dict: dict,
    domain: str = "",
    valid_days: int = DEFAULT_VALID_DAYS,
) -> BootstrapResult:
    """Mutate ``secrets_dict`` in place, returning a BootstrapResult.

    Pure: no I/O. Callers handle reading and writing the SOPS sealed file.
    """
    generated: list[str] = []
    skipped: list[str] = []

    # JWT_KEY: skip if exists, generate if missing.
    if _present(secrets_dict.get("JWT_KEY")):
        skipped.append("JWT_KEY")
    else:
        secrets_dict["JWT_KEY"] = _secrets.token_hex(32)
        generated.append("JWT_KEY")

    # SSL pairs.
    for prefix, cert_key, priv_key in SSL_PAIRS:
        cert_present = _present(secrets_dict.get(cert_key))
        priv_present = _present(secrets_dict.get(priv_key))
        if cert_present and priv_present:
            skipped.extend([cert_key, priv_key])
            continue
        if cert_present != priv_present:
            raise BootstrapError(
                f"Corrupt half-state in the SOPS sealed file: one of "
                f"{cert_key}/{priv_key} is set but the other is empty. "
                "Cert and private key must be supplied as a unit. "
                "Fix manually (remove both or supply both) and rerun init."
            )
        # Both absent: generate.
        hostname = f"{prefix}.{domain}" if domain else f"{prefix}.local"
        cert_pem, key_pem = _generate_self_signed_pair(hostname, valid_days)
        secrets_dict[cert_key] = _b64(cert_pem)
        secrets_dict[priv_key] = _b64(key_pem)
        generated.extend([cert_key, priv_key])

    return BootstrapResult(generated=tuple(generated), skipped=tuple(skipped))


def run(
    secrets_path: Path,
    domain: str = "",
    valid_days: int = DEFAULT_VALID_DAYS,
    sops_encrypt: Optional[callable] = None,
) -> BootstrapResult:
    """Read the sealed file (plaintext or sops-decrypted), seed, write back.

    Caller responsibility: provide a plaintext-on-disk path. SOPS
    encrypt/decrypt is left to the caller (``scripts/secretmgr.py``).
    This keeps tls_bootstrap testable without sops in the test sandbox.

    Note: ``yaml`` is imported locally to keep this module's globals free
    of orchestration imports (see ModuleSplitContract).
    """
    import yaml  # local import — see module docstring

    if secrets_path.exists():
        with secrets_path.open("r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        if not isinstance(data, dict):
            raise BootstrapError(
                f"{secrets_path} does not parse as a YAML mapping"
            )
    else:
        data = {}

    result = seed_secrets_yaml(data, domain=domain, valid_days=valid_days)

    if result.generated:
        # Atomic write so a crash mid-write does not leave a partial file.
        tmp = secrets_path.with_suffix(secrets_path.suffix + ".tmp")
        with tmp.open("w", encoding="utf-8") as f:
            yaml.safe_dump(data, f, default_flow_style=False, sort_keys=True)
        tmp.replace(secrets_path)
        if sops_encrypt is not None:
            sops_encrypt(secrets_path)

    return result
