"""PR-Z Phase A tests: pure-crypto extensions to ``scripts/tls_bootstrap.py``.

Covers (per design §11):
  - TestSanListIsExactlyTwo: KAMAILIO_PAIRS pinned to sip + registrar
  - TestSelfSignedCaGeneration: RSA-2048, X.509v3, CN, validity, BasicConstraints
  - TestLeafCertIssuance: chain, SAN, wildcard, EKU, validity, AKI
  - TestCaValidityInvariant: CA outlives leaf
  - TestModuleSplitContract: no orchestration imports / strings in tls_bootstrap
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from cryptography import x509
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import ExtendedKeyUsageOID

from scripts import tls_bootstrap
from scripts.tls_bootstrap import (
    KAMAILIO_CA_CERT_KEY,
    KAMAILIO_CA_KEY_KEY,
    KAMAILIO_PAIRS,
    _generate_ca,
    _issue_leaf_signed_by_ca,
)


# ---------------------------------------------------------------------------
# TestSanListIsExactlyTwo (2)
# ---------------------------------------------------------------------------

class TestSanListIsExactlyTwo:
    def test_exactly_two_pairs(self):
        assert len(KAMAILIO_PAIRS) == 2

    def test_pinned_sip_and_registrar_no_trunk_stable_order(self):
        prefixes = [p[0] for p in KAMAILIO_PAIRS]
        assert prefixes == ["sip", "registrar"]
        # Design §3.1: trunk is dispatcher-only, NOT a TLS server_name.
        assert "trunk" not in prefixes
        # Verify the secret-key names match the pinned schema (design §6.1).
        assert KAMAILIO_PAIRS[0][1:] == (
            "KAMAILIO_CERT_SIP_BASE64",
            "KAMAILIO_PRIVKEY_SIP_BASE64",
        )
        assert KAMAILIO_PAIRS[1][1:] == (
            "KAMAILIO_CERT_REGISTRAR_BASE64",
            "KAMAILIO_PRIVKEY_REGISTRAR_BASE64",
        )


# ---------------------------------------------------------------------------
# TestSelfSignedCaGeneration (5)
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def ca_pair():
    return _generate_ca()


@pytest.fixture(scope="module")
def ca_cert(ca_pair):
    return x509.load_pem_x509_certificate(ca_pair[0])


@pytest.fixture(scope="module")
def ca_key(ca_pair):
    return serialization.load_pem_private_key(ca_pair[1], password=None)


class TestSelfSignedCaGeneration:
    def test_rsa_2048(self, ca_key):
        assert isinstance(ca_key, rsa.RSAPrivateKey)
        assert ca_key.key_size == 2048

    def test_x509_v3(self, ca_cert):
        # cryptography exposes ``version`` as ``Version.v3``.
        assert ca_cert.version == x509.Version.v3

    def test_cn_includes_voipbin_install_ca(self, ca_cert):
        cn = ca_cert.subject.rfc4514_string()
        assert "VoIPBin Install CA" in cn

    def test_validity_roughly_3650d(self, ca_cert):
        not_before = (
            ca_cert.not_valid_before_utc
            if hasattr(ca_cert, "not_valid_before_utc")
            else ca_cert.not_valid_before.replace(tzinfo=timezone.utc)
        )
        not_after = (
            ca_cert.not_valid_after_utc
            if hasattr(ca_cert, "not_valid_after_utc")
            else ca_cert.not_valid_after.replace(tzinfo=timezone.utc)
        )
        delta = not_after - not_before
        # 3650d +/- 1 day tolerance for the 5-minute backdating.
        assert abs(delta - timedelta(days=3650)) < timedelta(days=1)

    def test_basic_constraints_ca_true_pathlen_zero(self, ca_cert):
        bc = ca_cert.extensions.get_extension_for_class(
            x509.BasicConstraints
        )
        assert bc.critical is True
        assert bc.value.ca is True
        assert bc.value.path_length == 0


# ---------------------------------------------------------------------------
# TestLeafCertIssuance (6)
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def leaf_sip(ca_pair):
    pem, _key = _issue_leaf_signed_by_ca(
        "sip.example.com", ca_pair[0], ca_pair[1], wildcard=False
    )
    return x509.load_pem_x509_certificate(pem)


@pytest.fixture(scope="module")
def leaf_registrar(ca_pair):
    pem, _key = _issue_leaf_signed_by_ca(
        "registrar.example.com", ca_pair[0], ca_pair[1], wildcard=True
    )
    return x509.load_pem_x509_certificate(pem)


class TestLeafCertIssuance:
    def test_leaf_verifies_against_ca(self, leaf_sip, ca_cert):
        # cryptography >=40 provides verify_directly_issued_by.
        leaf_sip.verify_directly_issued_by(ca_cert)

    def test_san_matches_input_no_wildcard(self, leaf_sip):
        san_ext = leaf_sip.extensions.get_extension_for_class(
            x509.SubjectAlternativeName
        )
        dns_names = san_ext.value.get_values_for_type(x509.DNSName)
        assert dns_names == ["sip.example.com"]

    def test_registrar_wildcard_adds_star_san(self, leaf_registrar):
        san_ext = leaf_registrar.extensions.get_extension_for_class(
            x509.SubjectAlternativeName
        )
        dns_names = san_ext.value.get_values_for_type(x509.DNSName)
        assert "registrar.example.com" in dns_names
        assert "*.registrar.example.com" in dns_names

    def test_eku_server_auth(self, leaf_sip):
        eku = leaf_sip.extensions.get_extension_for_class(
            x509.ExtendedKeyUsage
        )
        assert ExtendedKeyUsageOID.SERVER_AUTH in list(eku.value)

    def test_validity_roughly_365d(self, leaf_sip):
        nb = (
            leaf_sip.not_valid_before_utc
            if hasattr(leaf_sip, "not_valid_before_utc")
            else leaf_sip.not_valid_before.replace(tzinfo=timezone.utc)
        )
        na = (
            leaf_sip.not_valid_after_utc
            if hasattr(leaf_sip, "not_valid_after_utc")
            else leaf_sip.not_valid_after.replace(tzinfo=timezone.utc)
        )
        assert abs((na - nb) - timedelta(days=365)) < timedelta(days=1)

    def test_aki_matches_ca_ski(self, leaf_sip, ca_cert):
        aki = leaf_sip.extensions.get_extension_for_class(
            x509.AuthorityKeyIdentifier
        ).value
        ski = ca_cert.extensions.get_extension_for_class(
            x509.SubjectKeyIdentifier
        ).value
        assert aki.key_identifier == ski.digest


# ---------------------------------------------------------------------------
# TestCaValidityInvariant (1)
# ---------------------------------------------------------------------------

class TestCaValidityInvariant:
    def test_ca_not_after_outlives_leaf_by_30d(self, ca_cert, leaf_sip):
        ca_na = (
            ca_cert.not_valid_after_utc
            if hasattr(ca_cert, "not_valid_after_utc")
            else ca_cert.not_valid_after.replace(tzinfo=timezone.utc)
        )
        leaf_na = (
            leaf_sip.not_valid_after_utc
            if hasattr(leaf_sip, "not_valid_after_utc")
            else leaf_sip.not_valid_after.replace(tzinfo=timezone.utc)
        )
        assert ca_na > leaf_na + timedelta(days=30)


# ---------------------------------------------------------------------------
# TestModuleSplitContract (2)
# ---------------------------------------------------------------------------

class TestModuleSplitContract:
    def test_globals_have_no_orchestration_imports(self):
        forbidden = {"yaml", "cert_lifecycle", "secretmgr"}
        present = forbidden & set(vars(tls_bootstrap))
        assert not present, (
            f"tls_bootstrap module globals must not include orchestration "
            f"imports {sorted(forbidden)}; found: {sorted(present)}"
        )

    def test_source_has_no_state_or_secrets_string_literals(self):
        src_path = Path(tls_bootstrap.__file__)
        text = src_path.read_text(encoding="utf-8")
        # Strip comments and docstrings before scanning so that documentation
        # mentioning "secrets.yaml" or "state.yaml" does not trip the check.
        # Strip Python comments.
        code = re.sub(r"#[^\n]*", "", text)
        # Strip triple-quoted strings (docstrings on classes/funcs/module).
        code = re.sub(r'"""(?:.|\n)*?"""', "", code)
        code = re.sub(r"'''(?:.|\n)*?'''", "", code)
        assert "secrets.yaml" not in code, (
            "tls_bootstrap source must not contain the literal 'secrets.yaml'"
        )
        assert "state.yaml" not in code, (
            "tls_bootstrap source must not contain the literal 'state.yaml'"
        )
