"""PR-F: tests for per-service DNS realignment in terraform/dns.tf.

Parses terraform/dns.tf via regex (no terraform binary, no GCP). Verifies
each web-tier A record points at its per-service external static IP, sip
remains on the Kamailio external LB, and the new hook record exists.
"""

import re
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
_DNS_TF = _ROOT / "terraform" / "dns.tf"


def _read_dns_tf() -> str:
    return _DNS_TF.read_text()


_BLOCK_RE = re.compile(
    r'resource\s+"google_dns_record_set"\s+"(?P<name>[a-zA-Z0-9_-]+)"\s*\{(?P<body>.*?)\n\}',
    re.DOTALL,
)


def _blocks() -> dict[str, str]:
    return {m.group("name"): m.group("body") for m in _BLOCK_RE.finditer(_read_dns_tf())}


def _field(body: str, key: str) -> str | None:
    m = re.search(rf'^\s*{re.escape(key)}\s*=\s*(?P<v>.+?)\s*$', body, re.MULTILINE)
    return m.group("v") if m else None


def test_api_record_points_to_api_manager_static_ip():
    body = _blocks()["api"]
    rr = _field(body, "rrdatas")
    assert rr is not None
    assert 'google_compute_address.external_service["api-manager"].address' in rr


def test_admin_record_points_to_admin_static_ip():
    body = _blocks()["admin"]
    rr = _field(body, "rrdatas")
    assert rr is not None
    assert 'google_compute_address.external_service["admin"].address' in rr


def test_talk_record_points_to_talk_static_ip():
    body = _blocks()["talk"]
    rr = _field(body, "rrdatas")
    assert rr is not None
    assert 'google_compute_address.external_service["talk"].address' in rr


def test_meet_record_points_to_meet_static_ip():
    body = _blocks()["meet"]
    rr = _field(body, "rrdatas")
    assert rr is not None
    assert 'google_compute_address.external_service["meet"].address' in rr


def test_sip_record_still_points_to_kamailio_lb_external():
    """Regression guard: sip MUST stay on kamailio_lb_external."""
    body = _blocks()["sip"]
    rr = _field(body, "rrdatas")
    assert rr is not None
    assert "google_compute_address.kamailio_lb_external.address" in rr


def test_hook_record_exists_and_points_to_hook_manager():
    blocks = _blocks()
    assert "hook" in blocks, "new hook.<domain> A record must exist"
    body = blocks["hook"]
    assert _field(body, "type") == '"A"'
    assert _field(body, "ttl") == "300"
    assert _field(body, "name") == '"hook.${var.domain}."'
    rr = _field(body, "rrdatas")
    assert rr is not None
    assert 'google_compute_address.external_service["hook-manager"].address' in rr


@pytest.mark.parametrize("rec", ["api", "admin", "talk", "meet", "hook"])
def test_no_web_tier_record_references_kamailio_lb_external(rec):
    """Sweep guard: none of the web-tier records may point at the SIP edge."""
    body = _blocks()[rec]
    assert "kamailio_lb_external" not in body, (
        f"{rec} record must not reference kamailio_lb_external "
        "(original A-3 / GAP-18 bug regression)"
    )


def test_dns_records_gated_on_dns_mode_auto():
    """Every google_dns_record_set must be count-gated on dns_mode == 'auto'."""
    for name, body in _blocks().items():
        count = _field(body, "count")
        assert count is not None, f"{name} missing count"
        assert count == 'var.dns_mode == "auto" ? 1 : 0', (
            f"{name} count gate is {count!r}"
        )
