"""TLS bootstrap for fresh self-hosting installs (PR #4 rewrite).

This is a pure "seed the sops file" script. It generates:

  - JWT_KEY (64-char random hex), and
  - Four base64-encoded PEM strings: SSL_CERT_API_BASE64,
    SSL_PRIVKEY_API_BASE64, SSL_CERT_HOOK_BASE64, SSL_PRIVKEY_HOOK_BASE64.

These five keys are written directly into the operator's ``secrets.yaml``
(sops-encrypted) and surface into ``Secret/voipbin`` via the existing sops
decrypt + substitution pipeline. There is no kubectl interaction here.

Idempotency (design §4.8):
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

import yaml
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID


DEFAULT_VALID_DAYS = 3650
CN_PLACEHOLDER = "voipbin-self-signed"

SSL_PAIRS: tuple[tuple[str, str, str], ...] = (
    ("api", "SSL_CERT_API_BASE64", "SSL_PRIVKEY_API_BASE64"),
    ("hook", "SSL_CERT_HOOK_BASE64", "SSL_PRIVKEY_HOOK_BASE64"),
)


class BootstrapError(RuntimeError):
    """Raised when secrets.yaml is in a corrupt half-state or I/O fails."""


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

    Pure: no I/O. Callers handle reading and writing the sops file.
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
                f"Corrupt half-state in secrets.yaml: one of "
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
    """Read secrets.yaml (plaintext or sops-decrypted), seed, write back.

    Caller responsibility: provide a plaintext-on-disk path. SOPS
    encrypt/decrypt is left to the caller (``scripts/secretmgr.py``).
    This keeps tls_bootstrap testable without sops in the test sandbox.
    """
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
