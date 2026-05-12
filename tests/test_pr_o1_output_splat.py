"""PR-O1: kamailio_external_ips output must use for-expression, not splat.

GAP-41 (regression of PR-N): the original splat expression
    google_compute_instance.kamailio[*].network_interface[0].access_config[0].nat_ip
silently evaluated to [null] in dogfood run 9w even though terraform state
showed the correct nat_ip. Splat through nested 0-or-1 blocks is unreliable
across terraform-provider-google versions. PR-O1 replaces it with an explicit
for-expression with try() fallback.

These tests pin the contract so the splat form cannot quietly regress.
"""

import re
from pathlib import Path


REPO = Path(__file__).resolve().parent.parent
OUTPUTS_TF = REPO / "terraform" / "outputs.tf"


def _kamailio_external_ips_block() -> str:
    content = OUTPUTS_TF.read_text()
    m = re.search(
        r'output\s+"kamailio_external_ips"\s*\{(.*?)^\}',
        content, re.DOTALL | re.MULTILINE,
    )
    assert m, "kamailio_external_ips output not found in outputs.tf"
    return m.group(1)


class TestKamailioExternalIpsOutput:
    def test_uses_for_expression(self):
        body = _kamailio_external_ips_block()
        assert "for inst in google_compute_instance.kamailio" in body, (
            "kamailio_external_ips must use an explicit for-expression to "
            "iterate per instance. A bare splat does not traverse the nested "
            "access_config block reliably and returns [null] (GAP-41)."
        )

    def test_does_not_use_splat_for_nat_ip(self):
        body = _kamailio_external_ips_block()
        # The splat form is the exact regression pattern. It must not appear
        # in this output even as a fallback.
        forbidden = (
            "google_compute_instance.kamailio[*]"
            ".network_interface[0].access_config[0].nat_ip"
        )
        assert forbidden not in body, (
            "Bare splat through access_config[0].nat_ip silently returns [null] "
            "for the kamailio output. Use a for-expression instead."
        )

    def test_returns_string_via_try(self):
        body = _kamailio_external_ips_block()
        assert "try(" in body and "nat_ip" in body, (
            "Use try() so a future kamailio_count=0 build does not crash "
            "with a missing-attribute error; fall back to empty string."
        )

    def test_references_nat_ip(self):
        body = _kamailio_external_ips_block()
        assert "access_config[0].nat_ip" in body
