"""PR-Z Phase A tests: ``scripts/cert_lifecycle.py``.

Covers (design §11):
  - TestSeedSelfSignedHappy
  - TestSeedSelfSignedIdempotent
  - TestSeedSelfSignedSanChange
  - TestHalfStatePolicy
  - TestSeedManualMode
  - TestAcmeRejection
  - TestExpiryThreshold
"""

from __future__ import annotations

import base64
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from cryptography import x509
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from scripts.cert_lifecycle import (
    CertLifecycleError,
    CertLifecycleResult,
    _audit_secret_completeness,
    _compute_san_list,
    seed_kamailio_certs,
)
from scripts.tls_bootstrap import (
    KAMAILIO_CA_CERT_KEY,
    KAMAILIO_CA_KEY_KEY,
    KAMAILIO_PAIRS,
    _generate_ca,
    _issue_leaf_signed_by_ca,
)


SELF_SIGNED_KEYS = (
    KAMAILIO_CA_CERT_KEY,
    KAMAILIO_CA_KEY_KEY,
    "KAMAILIO_CERT_SIP_BASE64",
    "KAMAILIO_PRIVKEY_SIP_BASE64",
    "KAMAILIO_CERT_REGISTRAR_BASE64",
    "KAMAILIO_PRIVKEY_REGISTRAR_BASE64",
)

MANUAL_KEYS = (
    "KAMAILIO_CERT_SIP_BASE64",
    "KAMAILIO_PRIVKEY_SIP_BASE64",
    "KAMAILIO_CERT_REGISTRAR_BASE64",
    "KAMAILIO_PRIVKEY_REGISTRAR_BASE64",
)

DOMAIN = "example.com"
SAN_LIST = ["sip.example.com", "registrar.example.com"]


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _b64(b: bytes) -> str:
    return base64.b64encode(b).decode("ascii")


def _make_manual_leaf(
    san: str,
    not_before: datetime,
    not_after: datetime,
    wildcard: bool = False,
) -> tuple[bytes, bytes]:
    """Generate a fresh self-signed leaf with the given validity window.

    Used to populate manual-mode test fixtures with controlled expiry.
    """
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = issuer = x509.Name(
        [x509.NameAttribute(NameOID.COMMON_NAME, san)]
    )
    dns = [x509.DNSName(san)]
    if wildcard:
        dns.append(x509.DNSName(f"*.{san}"))
    from cryptography.hazmat.primitives import hashes
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(not_before)
        .not_valid_after(not_after)
        .add_extension(x509.SubjectAlternativeName(dns), critical=False)
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


@pytest.fixture
def manual_cert_dir(tmp_path: Path) -> Path:
    """A valid manual-mode cert directory with two SAN subdirs."""
    now = datetime.now(timezone.utc)
    not_before = now - timedelta(minutes=5)
    not_after = now + timedelta(days=180)
    root = tmp_path / "manual_certs"
    for san, wildcard in (("sip.example.com", False),
                          ("registrar.example.com", True)):
        sub = root / san
        sub.mkdir(parents=True)
        fullchain, privkey = _make_manual_leaf(
            san, not_before, not_after, wildcard=wildcard
        )
        (sub / "fullchain.pem").write_bytes(fullchain)
        (sub / "privkey.pem").write_bytes(privkey)
    return root


@pytest.fixture(scope="module")
def fresh_self_signed_secrets():
    """Pre-baked self_signed secrets dict + matching cert objects."""
    ca_cert_pem, ca_key_pem = _generate_ca()
    sip_pem, sip_key = _issue_leaf_signed_by_ca(
        "sip.example.com", ca_cert_pem, ca_key_pem
    )
    reg_pem, reg_key = _issue_leaf_signed_by_ca(
        "registrar.example.com", ca_cert_pem, ca_key_pem, wildcard=True
    )
    secrets = {
        KAMAILIO_CA_CERT_KEY: _b64(ca_cert_pem),
        KAMAILIO_CA_KEY_KEY: _b64(ca_key_pem),
        "KAMAILIO_CERT_SIP_BASE64": _b64(sip_pem),
        "KAMAILIO_PRIVKEY_SIP_BASE64": _b64(sip_key),
        "KAMAILIO_CERT_REGISTRAR_BASE64": _b64(reg_pem),
        "KAMAILIO_PRIVKEY_REGISTRAR_BASE64": _b64(reg_key),
    }
    return secrets, ca_cert_pem, sip_pem, reg_pem


# ---------------------------------------------------------------------------
# TestSeedSelfSignedHappy (4)
# ---------------------------------------------------------------------------

class TestSeedSelfSignedHappy:
    def test_writes_six_keys(self):
        secrets, state = {}, {}
        seed_kamailio_certs(
            secrets, state,
            {"cert_mode": "self_signed", "domain": DOMAIN},
        )
        for k in SELF_SIGNED_KEYS:
            assert k in secrets and secrets[k]

    def test_did_reissue_true(self):
        secrets, state = {}, {}
        result = seed_kamailio_certs(
            secrets, state,
            {"cert_mode": "self_signed", "domain": DOMAIN},
        )
        assert result.did_reissue is True
        assert result.mode == "self_signed"

    def test_generated_keys_has_six(self):
        secrets, state = {}, {}
        result = seed_kamailio_certs(
            secrets, state,
            {"cert_mode": "self_signed", "domain": DOMAIN},
        )
        assert set(result.generated_keys) == set(SELF_SIGNED_KEYS)
        assert len(result.generated_keys) == 6

    def test_cert_state_populated(self):
        secrets, state = {}, {}
        seed_kamailio_certs(
            secrets, state,
            {"cert_mode": "self_signed", "domain": DOMAIN},
        )
        assert state["config_mode"] == "self_signed"
        assert state["actual_mode"] == "self_signed"
        assert state["san_list"] == SAN_LIST
        assert "VoIPBin Install CA" in state["ca_subject"]
        assert ":" in state["ca_fingerprint_sha256"]
        assert "ca_not_after" in state
        for san in SAN_LIST:
            entry = state["leaf_certs"][san]
            assert "not_after" in entry
            assert "fingerprint_sha256" in entry
            assert isinstance(entry["serial"], int)


# ---------------------------------------------------------------------------
# TestSeedSelfSignedIdempotent (3)
# ---------------------------------------------------------------------------

def _build_state_for(secrets: dict, san_list: list[str]) -> dict:
    """Build a state dict consistent with ``secrets`` (self_signed)."""
    state: dict = {}
    # Re-run the orchestrator once into a throwaway dict so we get state
    # exactly the way the orchestrator would write it.
    from copy import deepcopy
    sec_copy = deepcopy(secrets)
    seed_kamailio_certs(
        sec_copy, state,
        {"cert_mode": "self_signed", "domain": san_list[0].split(".", 1)[1]},
    )
    # State now describes the deepcopy; but since we did not actually reissue
    # in caller, we need state derived from the *original* secrets. The
    # simplest way is to call the orchestrator path that populates state
    # without reissuing — done in fresh_state_from_secrets below.
    return state


def _state_for_existing_secrets(secrets: dict, san_list: list[str]) -> dict:
    """Derive a cert_state dict from existing self_signed secrets without
    reissuing — by parsing the secrets directly."""
    ca = x509.load_pem_x509_certificate(
        base64.b64decode(secrets[KAMAILIO_CA_CERT_KEY])
    )
    sip = x509.load_pem_x509_certificate(
        base64.b64decode(secrets["KAMAILIO_CERT_SIP_BASE64"])
    )
    reg = x509.load_pem_x509_certificate(
        base64.b64decode(secrets["KAMAILIO_CERT_REGISTRAR_BASE64"])
    )

    def fp(c):
        from cryptography.hazmat.primitives import hashes
        return ":".join(f"{b:02X}" for b in c.fingerprint(hashes.SHA256()))

    def na(c):
        return (
            c.not_valid_after_utc
            if hasattr(c, "not_valid_after_utc")
            else c.not_valid_after.replace(tzinfo=timezone.utc)
        )

    return {
        "schema_version": 1,
        "config_mode": "self_signed",
        "actual_mode": "self_signed",
        "acme_pending": False,
        "ca_subject": ca.subject.rfc4514_string(),
        "ca_not_after": na(ca).isoformat(),
        "ca_fingerprint_sha256": fp(ca),
        "san_list": list(san_list),
        "leaf_certs": {
            san_list[0]: {
                "not_after": na(sip).isoformat(),
                "fingerprint_sha256": fp(sip),
                "serial": sip.serial_number,
            },
            san_list[1]: {
                "not_after": na(reg).isoformat(),
                "fingerprint_sha256": fp(reg),
                "serial": reg.serial_number,
            },
        },
    }


class TestSeedSelfSignedIdempotent:
    def test_did_reissue_false(self, fresh_self_signed_secrets):
        secrets = dict(fresh_self_signed_secrets[0])
        state = _state_for_existing_secrets(secrets, SAN_LIST)
        result = seed_kamailio_certs(
            secrets, state,
            {"cert_mode": "self_signed", "domain": DOMAIN},
        )
        assert result.did_reissue is False

    def test_generated_keys_empty(self, fresh_self_signed_secrets):
        secrets = dict(fresh_self_signed_secrets[0])
        state = _state_for_existing_secrets(secrets, SAN_LIST)
        result = seed_kamailio_certs(
            secrets, state,
            {"cert_mode": "self_signed", "domain": DOMAIN},
        )
        assert result.generated_keys == ()

    def test_state_untouched(self, fresh_self_signed_secrets):
        secrets = dict(fresh_self_signed_secrets[0])
        state = _state_for_existing_secrets(secrets, SAN_LIST)
        before = dict(state)
        seed_kamailio_certs(
            secrets, state,
            {"cert_mode": "self_signed", "domain": DOMAIN},
        )
        assert state == before


# ---------------------------------------------------------------------------
# TestSeedSelfSignedSanChange (2)
# ---------------------------------------------------------------------------

class TestSeedSelfSignedSanChange:
    def test_leaves_reissued_ca_preserved(self, fresh_self_signed_secrets):
        secrets = dict(fresh_self_signed_secrets[0])
        ca_before = secrets[KAMAILIO_CA_CERT_KEY]
        ca_key_before = secrets[KAMAILIO_CA_KEY_KEY]
        sip_before = secrets["KAMAILIO_CERT_SIP_BASE64"]
        # State pinned to OLD san list (different domain).
        old_san = ["sip.old.example.com", "registrar.old.example.com"]
        state = _state_for_existing_secrets(secrets, old_san)
        # Override state.san_list to OLD; but secrets are still for new.
        state["san_list"] = old_san
        result = seed_kamailio_certs(
            secrets, state,
            {"cert_mode": "self_signed", "domain": DOMAIN},
        )
        assert result.did_reissue is True
        # CA preserved (same base64).
        assert secrets[KAMAILIO_CA_CERT_KEY] == ca_before
        assert secrets[KAMAILIO_CA_KEY_KEY] == ca_key_before
        # Leaves changed.
        assert secrets["KAMAILIO_CERT_SIP_BASE64"] != sip_before

    def test_san_list_updated_in_state(self, fresh_self_signed_secrets):
        secrets = dict(fresh_self_signed_secrets[0])
        state = _state_for_existing_secrets(secrets, SAN_LIST)
        state["san_list"] = ["sip.old.example.com", "registrar.old.example.com"]
        seed_kamailio_certs(
            secrets, state,
            {"cert_mode": "self_signed", "domain": DOMAIN},
        )
        assert state["san_list"] == SAN_LIST


# ---------------------------------------------------------------------------
# TestHalfStatePolicy (4)
# ---------------------------------------------------------------------------

class TestHalfStatePolicy:
    def test_missing_ca_cert_triggers_full_reissue(
        self, fresh_self_signed_secrets,
    ):
        secrets = dict(fresh_self_signed_secrets[0])
        ca_before = secrets[KAMAILIO_CA_CERT_KEY]
        del secrets[KAMAILIO_CA_CERT_KEY]
        state = {}
        result = seed_kamailio_certs(
            secrets, state,
            {"cert_mode": "self_signed", "domain": DOMAIN},
        )
        assert result.did_reissue is True
        assert KAMAILIO_CA_CERT_KEY in result.generated_keys
        # CA was regenerated, so it should differ from before.
        assert secrets[KAMAILIO_CA_CERT_KEY] != ca_before

    def test_missing_one_leaf_triggers_full_reissue(
        self, fresh_self_signed_secrets,
    ):
        secrets = dict(fresh_self_signed_secrets[0])
        ca_before = secrets[KAMAILIO_CA_CERT_KEY]
        del secrets["KAMAILIO_CERT_SIP_BASE64"]
        state = _state_for_existing_secrets(
            fresh_self_signed_secrets[0], SAN_LIST
        )
        result = seed_kamailio_certs(
            secrets, state,
            {"cert_mode": "self_signed", "domain": DOMAIN},
        )
        assert result.did_reissue is True
        # Half-state policy: missing one leaf => FULL reissue (CA too).
        assert KAMAILIO_CA_CERT_KEY in result.generated_keys
        assert secrets[KAMAILIO_CA_CERT_KEY] != ca_before

    def test_malformed_base64_triggers_reissue(
        self, fresh_self_signed_secrets,
    ):
        secrets = dict(fresh_self_signed_secrets[0])
        secrets[KAMAILIO_CA_CERT_KEY] = "!!!not-base64!!!"
        state = _state_for_existing_secrets(
            fresh_self_signed_secrets[0], SAN_LIST
        )
        # _audit should flag the bad key.
        ok, bad = _audit_secret_completeness(secrets, "self_signed")
        assert ok is False
        assert KAMAILIO_CA_CERT_KEY in bad
        result = seed_kamailio_certs(
            secrets, state,
            {"cert_mode": "self_signed", "domain": DOMAIN},
        )
        assert result.did_reissue is True

    def test_stale_state_with_valid_secrets_corrected(
        self, fresh_self_signed_secrets,
    ):
        """Valid secrets + stale state (different san_list) ⇒ state corrected,
        leaf-only reissue."""
        secrets = dict(fresh_self_signed_secrets[0])
        state = {"config_mode": "self_signed", "san_list": ["bogus"]}
        seed_kamailio_certs(
            secrets, state,
            {"cert_mode": "self_signed", "domain": DOMAIN},
        )
        # State should now reflect the truth: real SAN list.
        assert state["san_list"] == SAN_LIST
        assert state["config_mode"] == "self_signed"


# ---------------------------------------------------------------------------
# TestSeedManualMode (5)
# ---------------------------------------------------------------------------

class TestSeedManualMode:
    def test_valid_manual_dir_writes_four_keys(self, manual_cert_dir):
        secrets, state = {}, {}
        result = seed_kamailio_certs(
            secrets, state,
            {"cert_mode": "manual", "domain": DOMAIN,
             "cert_manual_dir": str(manual_cert_dir)},
        )
        for k in MANUAL_KEYS:
            assert k in secrets and secrets[k]
        # CA keys NOT written in manual mode.
        assert KAMAILIO_CA_CERT_KEY not in secrets
        assert KAMAILIO_CA_KEY_KEY not in secrets
        assert state["actual_mode"] == "manual"
        assert result.mode == "manual"

    def test_missing_subdir_raises(self, tmp_path: Path):
        # Only sip exists, registrar missing.
        sip_dir = tmp_path / "sip.example.com"
        sip_dir.mkdir()
        now = datetime.now(timezone.utc)
        fc, pk = _make_manual_leaf(
            "sip.example.com", now - timedelta(minutes=5),
            now + timedelta(days=180),
        )
        (sip_dir / "fullchain.pem").write_bytes(fc)
        (sip_dir / "privkey.pem").write_bytes(pk)
        with pytest.raises(CertLifecycleError, match="registrar"):
            seed_kamailio_certs(
                {}, {},
                {"cert_mode": "manual", "domain": DOMAIN,
                 "cert_manual_dir": str(tmp_path)},
            )

    def test_expired_manual_cert_raises(self, tmp_path: Path):
        now = datetime.now(timezone.utc)
        for san, wc in (("sip.example.com", False),
                        ("registrar.example.com", True)):
            sub = tmp_path / san
            sub.mkdir()
            fc, pk = _make_manual_leaf(
                san,
                now - timedelta(days=400),
                now - timedelta(days=10),  # expired
                wildcard=wc,
            )
            (sub / "fullchain.pem").write_bytes(fc)
            (sub / "privkey.pem").write_bytes(pk)
        with pytest.raises(CertLifecycleError, match="expired"):
            seed_kamailio_certs(
                {}, {},
                {"cert_mode": "manual", "domain": DOMAIN,
                 "cert_manual_dir": str(tmp_path)},
            )

    def test_malformed_pem_raises(self, tmp_path: Path):
        for san in ("sip.example.com", "registrar.example.com"):
            sub = tmp_path / san
            sub.mkdir()
            (sub / "fullchain.pem").write_bytes(b"-----not a pem-----")
            (sub / "privkey.pem").write_bytes(b"-----not a pem-----")
        with pytest.raises(CertLifecycleError):
            seed_kamailio_certs(
                {}, {},
                {"cert_mode": "manual", "domain": DOMAIN,
                 "cert_manual_dir": str(tmp_path)},
            )

    def test_wildcard_san_accepted_in_manual_mode(self, manual_cert_dir):
        """Registrar fullchain has a wildcard SAN — orchestrator must accept
        without rejecting the cert as malformed."""
        # The fixture already produces a wildcard on the registrar leaf.
        # We verify by parsing the SAN extension after seeding.
        secrets, state = {}, {}
        seed_kamailio_certs(
            secrets, state,
            {"cert_mode": "manual", "domain": DOMAIN,
             "cert_manual_dir": str(manual_cert_dir)},
        )
        leaf = x509.load_pem_x509_certificate(
            base64.b64decode(secrets["KAMAILIO_CERT_REGISTRAR_BASE64"])
        )
        san_ext = leaf.extensions.get_extension_for_class(
            x509.SubjectAlternativeName
        )
        dns = san_ext.value.get_values_for_type(x509.DNSName)
        assert "*.registrar.example.com" in dns


# ---------------------------------------------------------------------------
# TestAcmeRejection (2)
# ---------------------------------------------------------------------------

class TestAcmeRejection:
    def test_acme_raises_with_pr_ac_mention(self):
        with pytest.raises(CertLifecycleError, match="PR-AC"):
            seed_kamailio_certs(
                {}, {},
                {"cert_mode": "acme", "domain": DOMAIN},
            )

    def test_bogus_mode_raises(self):
        with pytest.raises(CertLifecycleError):
            seed_kamailio_certs(
                {}, {},
                {"cert_mode": "bogus", "domain": DOMAIN},
            )


# ---------------------------------------------------------------------------
# TestExpiryThreshold (2)
# ---------------------------------------------------------------------------

class TestExpiryThreshold:
    def test_leaf_with_29_days_remaining_reissues(
        self, fresh_self_signed_secrets,
    ):
        secrets = dict(fresh_self_signed_secrets[0])
        state = _state_for_existing_secrets(secrets, SAN_LIST)
        # Real leaf has 365d validity; ask "now" close enough to expiry
        # that 29 days remain.
        leaf_na = datetime.fromisoformat(
            state["leaf_certs"][SAN_LIST[0]]["not_after"].replace("Z", "+00:00")
        )
        fake_now = leaf_na - timedelta(days=29)
        result = seed_kamailio_certs(
            secrets, state,
            {"cert_mode": "self_signed", "domain": DOMAIN},
            now=fake_now,
        )
        assert result.did_reissue is True

    def test_leaf_with_31_days_remaining_short_circuits(
        self, fresh_self_signed_secrets,
    ):
        secrets = dict(fresh_self_signed_secrets[0])
        state = _state_for_existing_secrets(secrets, SAN_LIST)
        leaf_na = datetime.fromisoformat(
            state["leaf_certs"][SAN_LIST[0]]["not_after"].replace("Z", "+00:00")
        )
        fake_now = leaf_na - timedelta(days=31)
        result = seed_kamailio_certs(
            secrets, state,
            {"cert_mode": "self_signed", "domain": DOMAIN},
            now=fake_now,
        )
        assert result.did_reissue is False


# ---------------------------------------------------------------------------
# Sanity: _compute_san_list
# ---------------------------------------------------------------------------

def test_compute_san_list_pins_sip_registrar():
    assert _compute_san_list("foo.example") == [
        "sip.foo.example", "registrar.foo.example"
    ]


def test_compute_san_list_empty_domain_raises():
    with pytest.raises(CertLifecycleError):
        _compute_san_list("")


# ---------------------------------------------------------------------------
# PR-Z D5/D6/D7 fix #2: CA expiry must gate the short-circuit
# ---------------------------------------------------------------------------

class TestCaExpiryShortCircuit:
    """``_state_short_circuit_ok`` must verify CA expiry in self_signed
    mode in addition to leaf expiry — otherwise an installer with leaves
    that still have 60d but a CA with only 5d left will short-circuit and
    ship a doomed cert chain to operators. PR-Z review iter D5/D6/D7."""

    from scripts.cert_lifecycle import _state_short_circuit_ok

    SAN_LIST_ = ["sip.example.com", "registrar.example.com"]

    def _make_state(
        self,
        *,
        config_mode: str,
        leaf_not_after,
        ca_not_after,
    ) -> dict:
        st = {
            "config_mode": config_mode,
            "san_list": list(self.SAN_LIST_),
            "leaf_certs": {
                san: {
                    "not_after": leaf_not_after.isoformat(),
                    "fingerprint_sha256": "AA",
                    "serial": 1,
                }
                for san in self.SAN_LIST_
            },
        }
        if ca_not_after is not None:
            st["ca_not_after"] = ca_not_after.isoformat()
        return st

    def test_self_signed_ca_about_to_expire_forces_reissue(self):
        from scripts.cert_lifecycle import _state_short_circuit_ok
        now = datetime(2025, 1, 1, tzinfo=timezone.utc)
        state = self._make_state(
            config_mode="self_signed",
            leaf_not_after=now + timedelta(days=300),  # fresh leaves
            ca_not_after=now + timedelta(days=20),     # CA <30d
        )
        assert _state_short_circuit_ok(
            state, "self_signed", self.SAN_LIST_, now
        ) is False

    def test_self_signed_ca_fresh_short_circuits(self):
        from scripts.cert_lifecycle import _state_short_circuit_ok
        now = datetime(2025, 1, 1, tzinfo=timezone.utc)
        state = self._make_state(
            config_mode="self_signed",
            leaf_not_after=now + timedelta(days=300),
            ca_not_after=now + timedelta(days=365),
        )
        assert _state_short_circuit_ok(
            state, "self_signed", self.SAN_LIST_, now
        ) is True

    def test_self_signed_ca_not_after_missing_forces_reissue(self):
        from scripts.cert_lifecycle import _state_short_circuit_ok
        now = datetime(2025, 1, 1, tzinfo=timezone.utc)
        state = self._make_state(
            config_mode="self_signed",
            leaf_not_after=now + timedelta(days=300),
            ca_not_after=None,
        )
        assert _state_short_circuit_ok(
            state, "self_signed", self.SAN_LIST_, now
        ) is False

    def test_manual_mode_ignores_ca_not_after(self):
        from scripts.cert_lifecycle import _state_short_circuit_ok
        now = datetime(2025, 1, 1, tzinfo=timezone.utc)
        # ca_not_after absent (manual mode has external CA) — still OK.
        state = self._make_state(
            config_mode="manual",
            leaf_not_after=now + timedelta(days=300),
            ca_not_after=None,
        )
        assert _state_short_circuit_ok(
            state, "manual", self.SAN_LIST_, now
        ) is True
