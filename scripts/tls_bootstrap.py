"""TLS bootstrap for fresh self-hosting installs.

Generates an in-memory self-signed RSA-2048 cert with multi-host SAN,
then materializes it as:

  - `voipbin-tls` Kubernetes TLS-shape Secret (tls.crt, tls.key) in
                  EACH configured namespace. PR #3a wrote only to
                  `bin-manager`. PR #3b extends to `square-manager`
                  so the frontend nginx sidecar can mount the same
                  cert.
  - `voipbin-secret` existing Opaque Secret in `bin-manager` only.
                     We patch in SSL_CERT_BASE64 / SSL_PRIVKEY_BASE64
                     keys that bin-api-manager and bin-hook-manager
                     read at startup (env-var injection pattern).

Self-healing-on-retry contract (see design doc §5.6):
  - All SSL keys empty AND no `voipbin-tls` exists in any namespace:
    generate cert, patch opaque, create voipbin-tls in EACH ns.
  - All SSL keys empty AND voipbin-tls exists in ANY ns:
    delete from ALL ns (--ignore-not-found), then fresh-generate.
  - Both SSL keys non-empty (operator pre-supplied real cert):
    skip all writes.
  - Exactly one key non-empty (partial fill): hard error, abort.

The multi-namespace creates are sequential, not transactional. On
partial failure, the next `init` invocation re-takes the
fresh-generate path because the stale-cleanup branch sees the
half-written state.

Private key never touches operator disk: PEM bytes flow through
subprocess stdin pipe directly to `kubectl apply -f -` /
`kubectl patch`.
"""

from __future__ import annotations

import base64
import json
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from scripts.display import print_step, print_success, print_warning


DEFAULT_HOSTS = ("api", "hook", "admin", "talk", "meet")
DEFAULT_NAMESPACE = "bin-manager"
DEFAULT_TLS_NAMESPACES = ("bin-manager", "square-manager")
DEFAULT_TLS_SECRET = "voipbin-tls"
DEFAULT_OPAQUE_SECRET = "voipbin-secret"
DEFAULT_VALID_DAYS = 3650
CN_PLACEHOLDER = "voipbin-self-signed"


class BootstrapError(RuntimeError):
    """Raised when the self-healing-on-retry contract is violated or kubectl fails."""


@dataclass(frozen=True)
class BootstrapResult:
    """Return shape from bootstrap_voipbin_tls_secret.

    voipbin_tls_action maps namespace → action ("created" or "skipped").
    voipbin_secret_action is a single string because we patch only
    `bin-manager/voipbin-secret`.
    """

    voipbin_tls_action: dict[str, str] = field(default_factory=dict)
    voipbin_secret_action: str = "skipped"


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
    namespaces: list[str] | None = None,
    hostnames: list[str] | None = None,
    tls_secret_name: str = DEFAULT_TLS_SECRET,
    opaque_secret_name: str = DEFAULT_OPAQUE_SECRET,
    opaque_secret_namespace: str = DEFAULT_NAMESPACE,
    valid_days: int = DEFAULT_VALID_DAYS,
) -> BootstrapResult:
    """Bootstrap TLS Secrets for a fresh install. See module docstring.

    `voipbin-tls` is created in EACH namespace in `namespaces`.
    `voipbin-secret` is patched only in `opaque_secret_namespace`
    (default `bin-manager`) because only bin-api-manager and
    bin-hook-manager Pods consume the env-var pair.
    """
    if hostnames is None:
        hostnames = list(DEFAULT_HOSTS)
    if namespaces is None:
        namespaces = list(DEFAULT_TLS_NAMESPACES)
    if not namespaces:
        raise ValueError("namespaces must be non-empty")
    san_hosts = tuple(hostnames)

    cert_b64, key_b64 = _read_secret_ssl_keys(
        opaque_secret_namespace, opaque_secret_name,
    )
    cert_present = bool(cert_b64.strip())
    key_present = bool(key_b64.strip())

    if cert_present != key_present:
        raise BootstrapError(
            "SSL_CERT_BASE64 and SSL_PRIVKEY_BASE64 are partially set in "
            f"{opaque_secret_namespace}/{opaque_secret_name} — refusing to mix "
            "bootstrap values with operator-provided values. "
            "Fix manually and rerun init."
        )

    if cert_present and key_present:
        print_step(
            f"SSL keys already populated in "
            f"{opaque_secret_namespace}/{opaque_secret_name}; "
            f"leaving untouched. {tls_secret_name} Secret(s) will be "
            "created by operator or by sidecar consumers."
        )
        return BootstrapResult(
            voipbin_tls_action={ns: "skipped" for ns in namespaces},
            voipbin_secret_action="skipped-prefilled",
        )

    # Stale-cleanup detection: if voipbin-tls exists in ANY of the
    # configured namespaces (e.g., from a prior partial run), wipe
    # it from ALL configured namespaces before regenerating a single
    # fresh cert pair. This guarantees every voipbin-tls Secret in
    # every namespace holds the SAME cert/key as voipbin-secret.
    stale_namespaces = [
        ns for ns in namespaces if _secret_exists(ns, tls_secret_name)
    ]
    if stale_namespaces:
        print_warning(
            f"Found stale {tls_secret_name} in "
            f"{', '.join(stale_namespaces)} from a partial run; "
            "deleting from all configured namespaces so a fresh cert "
            "pair can be regenerated."
        )
        for ns in namespaces:
            delete_res = _kubectl_run([
                "kubectl", "-n", ns, "delete", "secret", tls_secret_name,
                "--ignore-not-found",
            ])
            if delete_res.returncode != 0:
                raise BootstrapError(
                    f"failed to delete stale {ns}/{tls_secret_name}: "
                    f"{delete_res.stderr.decode('utf-8', 'replace')}"
                )

    cert_pem, key_pem = _generate_self_signed(san_hosts, valid_days=valid_days)

    # Order matters for self-healing-on-retry:
    # 1. Patch voipbin-secret FIRST. If this fails, no voipbin-tls
    #    exists yet so retry takes the fresh-generate path cleanly.
    # 2. Create voipbin-tls in EACH ns SECOND. If any create fails,
    #    voipbin-secret holds the cert. Retry detects the partial
    #    state via stale-cleanup (some ns have voipbin-tls, others
    #    don't) → delete-from-all → regenerate.
    _patch_opaque_secret_ssl(
        opaque_secret_namespace, opaque_secret_name, cert_pem, key_pem,
    )
    print_success(
        f"Patched {opaque_secret_namespace}/{opaque_secret_name} "
        "(SSL_CERT_BASE64 + SSL_PRIVKEY_BASE64)."
    )

    tls_actions: dict[str, str] = {}
    for ns in namespaces:
        _create_tls_secret(ns, tls_secret_name, cert_pem, key_pem)
        tls_actions[ns] = "created"
        print_success(
            f"Self-signed TLS cert created in {ns}/{tls_secret_name} "
            f"(SAN: {', '.join(san_hosts)}; valid {valid_days} days)."
        )

    print_warning(
        "Self-signed cert is for fresh-install bring-up only. "
        "Replace with a real cert before production use "
        "(see Production Checklist in README)."
    )

    return BootstrapResult(
        voipbin_tls_action=tls_actions,
        voipbin_secret_action="patched",
    )
