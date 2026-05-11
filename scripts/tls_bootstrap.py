"""TLS bootstrap for fresh self-hosting installs.

Generates an in-memory self-signed RSA-2048 cert with multi-host SAN,
then materializes it as two Secrets in the `bin-manager` namespace:

  - `voipbin-tls`     Kubernetes TLS-shape Secret (tls.crt, tls.key).
                      Consumers in PR #3b (frontend nginx sidecar)
                      will mount this.
  - `voipbin-secret`  Existing Opaque Secret. We patch in
                      SSL_CERT_BASE64 / SSL_PRIVKEY_BASE64 keys that
                      bin-api-manager and bin-hook-manager read at
                      startup (env-var injection pattern).

Idempotent + atomic-pair contract (see design doc §5.2.1):
  - Both SSL keys empty: generate and patch both. Also create
    voipbin-tls Secret.
  - Both SSL keys non-empty (operator pre-supplied real cert):
    skip both Secrets entirely.
  - Exactly one key non-empty (partial fill): hard error, abort.

Private key never touches operator disk: PEM bytes flow through
subprocess stdin pipe directly to `kubectl apply -f -` /
`kubectl patch`.
"""

from __future__ import annotations

import base64
import json
import subprocess
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from scripts.display import print_step, print_success, print_warning


DEFAULT_HOSTS = ("api", "hook", "admin", "talk", "meet")
DEFAULT_NAMESPACE = "bin-manager"
DEFAULT_TLS_SECRET = "voipbin-tls"
DEFAULT_OPAQUE_SECRET = "voipbin-secret"
DEFAULT_VALID_DAYS = 3650
CN_PLACEHOLDER = "voipbin-self-signed"


class BootstrapError(RuntimeError):
    """Raised when the atomic-pair contract is violated or kubectl fails."""


@dataclass(frozen=True)
class BootstrapResult:
    """Return shape from bootstrap_voipbin_tls_secret."""

    voipbin_tls_action: str        # "created" or "skipped"
    voipbin_secret_action: str     # "patched", "skipped", or "skipped-prefilled"


def _generate_self_signed(
    hostnames: tuple[str, ...],
    valid_days: int = DEFAULT_VALID_DAYS,
) -> tuple[bytes, bytes]:
    """Return (cert_pem, key_pem) tuple. PEM bytes only — never written to disk."""
    if not hostnames:
        raise ValueError("hostnames must be non-empty")
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = issuer = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, CN_PLACEHOLDER)])
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
            x509.SubjectAlternativeName([x509.DNSName(h) for h in hostnames]),
            critical=False,
        )
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .sign(private_key=key, algorithm=hashes.SHA256())
    )
    cert_pem = cert.public_bytes(serialization.Encoding.PEM)
    key_pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    return cert_pem, key_pem


def _kubectl_run(args: list[str], stdin: bytes | None = None) -> subprocess.CompletedProcess:
    """Run kubectl with optional stdin pipe. Returns CompletedProcess."""
    return subprocess.run(
        args,
        input=stdin,
        capture_output=True,
        check=False,
        timeout=30,
    )


def _secret_exists(namespace: str, name: str) -> bool:
    res = _kubectl_run([
        "kubectl", "-n", namespace, "get", "secret", name, "-o", "name",
    ])
    return res.returncode == 0 and name in res.stdout.decode("utf-8", "replace")


def _ensure_namespace(namespace: str) -> None:
    """Create the namespace if it does not exist. Idempotent."""
    res = _kubectl_run(["kubectl", "get", "namespace", namespace, "-o", "name"])
    if res.returncode == 0 and namespace in res.stdout.decode("utf-8", "replace"):
        return
    manifest = json.dumps({
        "apiVersion": "v1",
        "kind": "Namespace",
        "metadata": {"name": namespace},
    }).encode("utf-8")
    create = _kubectl_run(["kubectl", "apply", "-f", "-"], stdin=manifest)
    if create.returncode != 0:
        raise BootstrapError(
            f"failed to ensure namespace {namespace}: "
            f"{create.stderr.decode('utf-8', 'replace')}"
        )


def _read_secret_ssl_keys(namespace: str, name: str) -> tuple[str, str]:
    """Return (cert_b64, privkey_b64) currently stored in the Secret.

    Returns ("", "") if the Secret itself does not exist (fresh install
    pre-kubectl-apply state) or has no SSL data keys. The caller treats
    "both empty" as the trigger for fresh-cert generation.
    """
    res = _kubectl_run([
        "kubectl", "-n", namespace, "get", "secret", name, "-o", "json",
    ])
    if res.returncode != 0:
        stderr = res.stderr.decode("utf-8", "replace").lower()
        # NotFound is the fresh-install case: namespace or Secret not yet
        # applied. Treat both keys as empty so bootstrap takes the
        # "generate + create" path. The bootstrap will also create the
        # Secret with placeholder non-SSL keys preserved as-is by a
        # later `kubectl apply -k k8s/` run that includes the manifest.
        if "notfound" in stderr.replace(" ", "") or "not found" in stderr:
            return "", ""
        raise BootstrapError(
            f"failed to read Secret {namespace}/{name}: "
            f"{res.stderr.decode('utf-8', 'replace')}"
        )
    try:
        data = json.loads(res.stdout)
    except json.JSONDecodeError as exc:
        raise BootstrapError(f"unparseable Secret JSON: {exc}") from exc
    payload = data.get("data") or {}
    cert = payload.get("SSL_CERT_BASE64", "") or ""
    key = payload.get("SSL_PRIVKEY_BASE64", "") or ""
    return cert, key


def _tls_secret_manifest(namespace: str, name: str, cert_pem: bytes, key_pem: bytes) -> bytes:
    """Build a Kubernetes TLS Secret manifest as JSON bytes for kubectl apply."""
    body = {
        "apiVersion": "v1",
        "kind": "Secret",
        "type": "kubernetes.io/tls",
        "metadata": {"name": name, "namespace": namespace},
        "data": {
            "tls.crt": base64.b64encode(cert_pem).decode("ascii"),
            "tls.key": base64.b64encode(key_pem).decode("ascii"),
        },
    }
    return json.dumps(body).encode("utf-8")


def _create_tls_secret(namespace: str, name: str, cert_pem: bytes, key_pem: bytes) -> None:
    manifest = _tls_secret_manifest(namespace, name, cert_pem, key_pem)
    res = _kubectl_run(["kubectl", "apply", "-f", "-"], stdin=manifest)
    if res.returncode != 0:
        raise BootstrapError(
            f"kubectl apply for {namespace}/{name} failed: "
            f"{res.stderr.decode('utf-8', 'replace')}"
        )


def _patch_opaque_secret_ssl(
    namespace: str, name: str, cert_pem: bytes, key_pem: bytes,
) -> None:
    """Patch SSL_CERT_BASE64 + SSL_PRIVKEY_BASE64 atomically via merge-patch.

    Uses JSON merge patch (`--type=merge`) so the 6 existing non-SSL
    keys (JWT_KEY, DB_USER, DB_PASSWORD, REDIS_PASSWORD,
    RABBITMQ_PASSWORD, API_SIGNING_KEY) are preserved.
    """
    patch = {
        "data": {
            "SSL_CERT_BASE64": base64.b64encode(cert_pem).decode("ascii"),
            "SSL_PRIVKEY_BASE64": base64.b64encode(key_pem).decode("ascii"),
        },
    }
    patch_json = json.dumps(patch)
    res = _kubectl_run([
        "kubectl", "-n", namespace, "patch", "secret", name,
        "--type=merge", "-p", patch_json,
    ])
    if res.returncode != 0:
        raise BootstrapError(
            f"kubectl patch for {namespace}/{name} failed: "
            f"{res.stderr.decode('utf-8', 'replace')}"
        )


def bootstrap_voipbin_tls_secret(
    namespace: str = DEFAULT_NAMESPACE,
    hostnames: list[str] | None = None,
    tls_secret_name: str = DEFAULT_TLS_SECRET,
    opaque_secret_name: str = DEFAULT_OPAQUE_SECRET,
    valid_days: int = DEFAULT_VALID_DAYS,
) -> BootstrapResult:
    """Bootstrap TLS Secrets for a fresh install. See module docstring."""
    if hostnames is None:
        hostnames = list(DEFAULT_HOSTS)
    san_hosts = tuple(hostnames)

    cert_b64, key_b64 = _read_secret_ssl_keys(namespace, opaque_secret_name)
    cert_present = bool(cert_b64.strip())
    key_present = bool(key_b64.strip())

    if cert_present != key_present:
        raise BootstrapError(
            "SSL_CERT_BASE64 and SSL_PRIVKEY_BASE64 are partially set in "
            f"{namespace}/{opaque_secret_name} — refusing to mix bootstrap "
            "values with operator-provided values. Fix manually and rerun init."
        )

    if cert_present and key_present:
        print_step(
            f"SSL keys already populated in {namespace}/{opaque_secret_name}; "
            f"leaving untouched. {namespace}/{tls_secret_name} will be created "
            "by operator or by PR #3b consumer."
        )
        return BootstrapResult(
            voipbin_tls_action="skipped",
            voipbin_secret_action="skipped-prefilled",
        )

    if _secret_exists(namespace, tls_secret_name):
        # Recovery case: a prior partial run created voipbin-tls but
        # never patched voipbin-secret. Delete the stale voipbin-tls so
        # we can regenerate a fresh pair below and keep both Secrets
        # holding the same cert. Without this step, _generate_self_signed
        # below would produce cert B while voipbin-tls still holds cert
        # A, leaving the two Secrets divergent for PR #3b consumers.
        print_warning(
            f"Found stale {namespace}/{tls_secret_name} from a partial run; "
            "deleting so a fresh cert pair can be regenerated."
        )
        delete_res = _kubectl_run([
            "kubectl", "-n", namespace, "delete", "secret", tls_secret_name,
            "--ignore-not-found",
        ])
        if delete_res.returncode != 0:
            raise BootstrapError(
                f"failed to delete stale {namespace}/{tls_secret_name}: "
                f"{delete_res.stderr.decode('utf-8', 'replace')}"
            )

    cert_pem, key_pem = _generate_self_signed(san_hosts, valid_days=valid_days)

    # Order matters for atomicity across kubectl-call failures:
    # 1. Patch voipbin-secret FIRST. If this fails, voipbin-tls is not
    #    yet created so retry takes the fresh-cert path cleanly.
    # 2. Create voipbin-tls SECOND. If this fails, voipbin-secret now
    #    holds the cert. Retry hits the both-populated skip branch
    #    (design §5.2.1) — operator is expected to manually create
    #    voipbin-tls or PR #3b's sidecar surfaces a clear NotFound.
    _patch_opaque_secret_ssl(namespace, opaque_secret_name, cert_pem, key_pem)
    print_success(
        f"Patched {namespace}/{opaque_secret_name} "
        "(SSL_CERT_BASE64 + SSL_PRIVKEY_BASE64)."
    )

    _create_tls_secret(namespace, tls_secret_name, cert_pem, key_pem)
    print_success(
        f"Self-signed TLS cert created in {namespace}/{tls_secret_name} "
        f"(SAN: {', '.join(san_hosts)}; valid {valid_days} days)."
    )

    print_warning(
        "Self-signed cert is for fresh-install bring-up only. "
        "Replace with a real cert before production use "
        "(see Production Checklist in README)."
    )

    return BootstrapResult(
        voipbin_tls_action="created",
        voipbin_secret_action="patched",
    )
