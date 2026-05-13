"""PR-AC-1: kamailio MySQL connection URL emits RAW password.

Pins the dogfood iter#11 (2026-05-13) lesson that Kamailio's db_mysql
driver does not percent-decode the password component of the connection
URL. Percent-encoding `+` to `%2B` causes MySQL to reject auth with
'Access denied for user kamailioro@<vm-ip>'.

The fix: emit the password raw in the URL. The locked alphabet regex
guards against URL-structural characters (`:`, `/`, `@`, `?`, `#`,
space, `%`) so raw emission cannot accidentally collide with delimiters.

Tests cover three contracts:
1. URL emits raw password literally (no percent-encoding leaks back in)
2. Alphabet guard rejects URL-structural characters that would break
   raw emission
3. End-to-end URL shape matches the expected mysql:// form exactly
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from scripts.ansible_runner import _build_kamailio_auth_db_url


HOST = "10.19.32.3"


def _cfg(host: str = HOST) -> MagicMock:
    cfg = MagicMock()
    cfg.get.side_effect = lambda key, default="": {
        "cloudsql_private_ip": host
    }.get(key, default)
    return cfg


class TestRawPasswordEmission:
    """URL contains the raw password verbatim; no percent-encoded forms."""

    def test_url_contains_raw_password_unencoded(self):
        # Locked-alphabet password with `+` × 2 — the exact char class that
        # the previous code percent-encoded into %2B and broke MySQL auth.
        pw = "Aa1Bb2Cc3.+-_~!*+xyz"
        url = _build_kamailio_auth_db_url(
            _cfg(), {"cloudsql_mysql_password_kamailioro": pw}
        )
        assert pw in url, f"raw password missing from URL: {url}"
        assert "%2B" not in url, (
            f"URL still contains percent-encoded `+` (%2B); Kamailio "
            f"db_mysql does not decode: {url}"
        )
        # Catch other percent-encodings of locked-alphabet specials too.
        for hex_form in ("%21", "%2A", "%2D", "%2E", "%5F", "%7E"):
            assert hex_form not in url, (
                f"URL contains percent-encoded char {hex_form}; should "
                f"be raw: {url}"
            )

    def test_url_contains_no_urllib_quote_artifacts(self):
        """Confirm urllib.parse.quote is not silently re-introduced."""
        # If quote were re-introduced even with safe="!*-._~", the bare `+`
        # would still get encoded. Pin that on a +-heavy password.
        pw = "AaBbCc++++DdEeFf"
        url = _build_kamailio_auth_db_url(
            _cfg(), {"cloudsql_mysql_password_kamailioro": pw}
        )
        assert "++++" in url, (
            f"raw `++++` not preserved in URL; quote() may have been "
            f"re-introduced: {url}"
        )


class TestAlphabetGuardRejectsUrlStructural:
    """Raw emission is only safe because the alphabet excludes URL-
    structural characters. If a future password generator widens the
    alphabet without updating this URL builder, raw emission would
    break URL parsing. The guard must reject every URL-structural char.
    """

    @pytest.mark.parametrize("bad_char", [":", "/", "@", "?", "#", " ", "%"])
    def test_url_structural_char_raises(self, bad_char):
        pw = f"AaBb1{bad_char}xyz"
        with pytest.raises(RuntimeError) as exc:
            _build_kamailio_auth_db_url(
                _cfg(), {"cloudsql_mysql_password_kamailioro": pw}
            )
        assert "cloudsql-credentials.md" in str(exc.value), (
            f"alphabet RuntimeError must reference rotation doc for "
            f"operator self-service. char={bad_char!r}"
        )

    def test_alphabet_accepts_full_locked_set(self):
        """The alphabet must accept every locked-set character so a
        future random_password generation that uses the full alphabet
        is not falsely rejected."""
        # One representative from each locked character class.
        pw = "AaZz09!*+-._~"
        url = _build_kamailio_auth_db_url(
            _cfg(), {"cloudsql_mysql_password_kamailioro": pw}
        )
        assert pw in url, (
            f"locked-alphabet password rejected by URL builder: {url}"
        )


class TestFullUrlShape:
    """End-to-end URL shape verification — proves the assembly order
    and delimiters around the raw password are correct."""

    def test_full_url_exact_shape(self):
        pw = "Abc1+def_xyz"
        url = _build_kamailio_auth_db_url(
            _cfg("10.99.0.3"),
            {"cloudsql_mysql_password_kamailioro": pw},
        )
        assert url == f"mysql://kamailioro:{pw}@10.99.0.3:3306/asterisk"
