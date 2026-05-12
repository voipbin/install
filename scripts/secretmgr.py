"""Secret generation and SOPS encryption for VoIPBin installer.

The schema is sourced from ``scripts/secret_schema.py``:

  - 26 operator-editable keys (22 bin secrets + 3 bin DSNs + 1 voip
    DATABASE_ASTERISK_PASSWORD) are seeded with placeholder/dummy values.
  - 5 init-generated keys (JWT_KEY + 4 SSL_*_BASE64) are seeded by
    ``scripts/tls_bootstrap.py`` at first ``voipbin-install init``.

``secrets.yaml`` may contain ONLY keys in the 31-entry union. Any unknown
key triggers a hard error so typos like ``JWT_KEYS`` cannot silently be
ignored.
"""

import os
from pathlib import Path
from typing import Any, Optional

import yaml

from scripts.display import print_error
from scripts.secret_schema import (
    BIN_SECRET_KEYS,
    all_allowed_secrets_yaml_keys,
    sops_editable_keys,
)
from scripts.utils import generate_key, generate_password, run_cmd


# Public: callers (verify, init) check this set.
ALLOWED_SECRET_KEYS: frozenset[str] = frozenset(all_allowed_secrets_yaml_keys())


def generate_all_secrets() -> dict[str, str]:
    """Seed values for the 26 operator-editable keys.

    JWT_KEY + 4 SSL_*_BASE64 are NOT generated here — ``tls_bootstrap.py``
    populates them. Dummy values come from ``secret_schema.py`` defaults
    when sensible; ``secret``-class entries get freshly generated random
    passwords so first-run installs don't ship known-weak dummies into
    sops-encrypted state.
    """
    seeded: dict[str, str] = {}
    for key in sorted(sops_editable_keys()):
        meta = BIN_SECRET_KEYS.get(key)
        if meta is None:
            # voip-only key (DATABASE_ASTERISK_PASSWORD).
            seeded[key] = generate_password(24)
            continue
        if meta["class"] == "secret":
            seeded[key] = generate_password(24)
        else:
            # dsn-class: keep the placeholder DSN; operator overrides as needed.
            seeded[key] = str(meta["default"])
    return seeded


def validate_secrets_keys(secrets_dict: dict[str, Any]) -> None:
    """Hard-fail if any key in ``secrets_dict`` is unknown.

    Allowed = 26 operator-editable + 5 init-generated.
    """
    unknown = [k for k in secrets_dict.keys() if k not in ALLOWED_SECRET_KEYS]
    if unknown:
        raise ValueError(
            "secrets.yaml contains unknown key(s): "
            f"{', '.join(sorted(unknown))}. "
            "Allowed keys are defined in scripts/secret_schema.py "
            f"({len(ALLOWED_SECRET_KEYS)} total: 26 operator-editable + "
            "5 init-generated)."
        )


def write_secrets_yaml(secrets_dict: dict[str, str], path: Path) -> None:
    """Write plaintext secrets to a YAML file (temporary — encrypt immediately)."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        yaml.safe_dump(secrets_dict, f, default_flow_style=False, sort_keys=True)


def encrypt_with_sops(plaintext_path: Path, kms_key_id: str) -> bool:
    """Encrypt a YAML file in-place using SOPS + GCP KMS. Returns True on success."""
    result = run_cmd(
        ["sops", "--encrypt", "--in-place", "--gcp-kms", kms_key_id, str(plaintext_path)],
        timeout=60,
    )
    return result.returncode == 0


def decrypt_with_sops(encrypted_path: Path) -> Optional[dict[str, Any]]:
    """Decrypt a SOPS-encrypted YAML file. Returns parsed dict or None."""
    result = run_cmd(["sops", "--decrypt", str(encrypted_path)], timeout=60)
    if result.returncode != 0:
        print_error(f"SOPS decryption failed for {encrypted_path}: {result.stderr.strip()}")
        print_error(
            "Ensure Application Default Credentials are valid and your account "
            "has roles/cloudkms.cryptoKeyDecrypter on the KMS key."
        )
        return None
    parsed = yaml.safe_load(result.stdout)
    if isinstance(parsed, dict):
        # Best-effort hard-fail on unknown keys at decrypt time too.
        try:
            validate_secrets_keys(parsed)
        except ValueError as exc:
            print_error(str(exc))
            return None
    return parsed


def write_sops_config(kms_key_id: str, config_dir: Path) -> None:
    """Write .sops.yaml configuration file."""
    sops_config = {
        "creation_rules": [
            {
                "path_regex": r"secrets\.yaml$",
                "gcp_kms": kms_key_id,
            }
        ]
    }
    sops_path = config_dir / ".sops.yaml"
    with open(sops_path, "w") as f:
        f.write("# SOPS configuration for VoIPBin installer\n")
        f.write("# Auto-generated — do not edit manually\n")
        yaml.safe_dump(sops_config, f, default_flow_style=False)


def generate_and_encrypt(
    kms_key_id: str,
    secrets_path: Path,
    domain: str = "",
) -> tuple[bool, dict[str, str]]:
    """Generate secrets, seed init-generated keys, write to file, encrypt with SOPS.

    Returns (success, secrets_dict). The secrets_dict is the plaintext
    values so the caller can display them. The file on disk is encrypted.
    """
    # Local import to avoid circular dep: tls_bootstrap is leaf-level.
    from scripts.tls_bootstrap import seed_secrets_yaml

    secrets_dict = generate_all_secrets()
    seed_secrets_yaml(secrets_dict, domain=domain)
    write_secrets_yaml(secrets_dict, secrets_path)

    ok = encrypt_with_sops(secrets_path, kms_key_id)
    if not ok:
        try:
            os.unlink(secrets_path)
        except OSError:
            pass
        return False, secrets_dict

    return True, secrets_dict
