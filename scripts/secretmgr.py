"""Secret generation and SOPS encryption for VoIPBin installer."""

import os
from pathlib import Path
from typing import Any, Optional

import yaml

from scripts.display import print_error
from scripts.utils import generate_key, generate_password, run_cmd


def generate_all_secrets() -> dict[str, str]:
    """Generate all required secrets for VoIPBin deployment."""
    return {
        "jwt_key": generate_key(32),
        "cloudsql_password": generate_password(24),
        "redis_password": generate_password(24),
        "rabbitmq_user": "voipbin",
        "rabbitmq_password": generate_password(24),
        "api_signing_key": generate_key(32),
    }


def write_secrets_yaml(secrets_dict: dict[str, str], path: Path) -> None:
    """Write plaintext secrets to a YAML file (temporary — encrypt immediately)."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        yaml.safe_dump(secrets_dict, f, default_flow_style=False)


def encrypt_with_sops(plaintext_path: Path, kms_key_id: str) -> bool:
    """Encrypt a YAML file in-place using SOPS + GCP KMS.

    Returns True on success.
    """
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
    return yaml.safe_load(result.stdout)


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
) -> tuple[bool, dict[str, str]]:
    """Generate secrets, write to file, encrypt with SOPS.

    Returns (success, secrets_dict). The secrets_dict is the plaintext
    values so the caller can display them. The file on disk is encrypted.
    """
    secrets_dict = generate_all_secrets()
    write_secrets_yaml(secrets_dict, secrets_path)

    ok = encrypt_with_sops(secrets_path, kms_key_id)
    if not ok:
        # Encryption failed — remove plaintext file for safety
        try:
            os.unlink(secrets_path)
        except OSError:
            pass
        return False, secrets_dict

    return True, secrets_dict
