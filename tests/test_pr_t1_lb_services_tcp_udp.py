"""PR-T1 regression. _LB_SERVICES Service-name suffixes must match the live
asterisk Helm chart's TCP/UDP-split Service naming convention.

Background. PR-R initially encoded `asterisk-registrar` and
`asterisk-conference` without the `-udp` suffix. The live voipbin chart
creates separate `asterisk-<component>-tcp` and `asterisk-<component>-udp`
LoadBalancer Services, so harvest_loadbalancer_ips() polled non-existent
Service names until the harvest timeout (default 300s) elapsed.

This test pins the suffix invariant so a future refactor of either side
(the Helm chart or _LB_SERVICES) is caught at unit-test time, not after
a 5-minute dogfood smoke timeout.

Kamailio's env.j2 has one slot per asterisk component
(ASTERISK_{CALL,REGISTRAR,CONFERENCE}_LB_ADDR) and SIP signaling uses UDP,
so we select the `-udp` variant for each. The `-tcp` Service IPs are
allocated by GCP but currently unused by Kamailio.
"""

from __future__ import annotations

from scripts.k8s import _LB_SERVICES


class TestAsteriskServicesUseUdpSuffix:
    """All three asterisk Services must reference the UDP variant."""

    def test_asterisk_call_uses_udp_suffix(self):
        services = {svc for (_ns, svc, _key) in _LB_SERVICES}
        assert "asterisk-call-udp" in services, (
            "asterisk-call-udp missing from _LB_SERVICES. "
            "The live chart splits asterisk-call into -tcp/-udp; harvest "
            "must target -udp for SIP signaling."
        )
        # The non-suffixed name must NOT appear — that Service does not exist.
        assert "asterisk-call" not in services, (
            "asterisk-call (no suffix) present in _LB_SERVICES. "
            "Live chart only has asterisk-call-tcp and asterisk-call-udp. "
            "Use asterisk-call-udp."
        )

    def test_asterisk_registrar_uses_udp_suffix(self):
        services = {svc for (_ns, svc, _key) in _LB_SERVICES}
        assert "asterisk-registrar-udp" in services, (
            "asterisk-registrar-udp missing from _LB_SERVICES. "
            "PR-T1 regression — see "
            "docs/plans/2026-05-13-pr-r-pipeline-reorder-k8s-outputs-design.md"
        )
        assert "asterisk-registrar" not in services, (
            "asterisk-registrar (no suffix) present in _LB_SERVICES. "
            "Use asterisk-registrar-udp."
        )

    def test_asterisk_conference_uses_udp_suffix(self):
        services = {svc for (_ns, svc, _key) in _LB_SERVICES}
        assert "asterisk-conference-udp" in services, (
            "asterisk-conference-udp missing from _LB_SERVICES."
        )
        assert "asterisk-conference" not in services, (
            "asterisk-conference (no suffix) present in _LB_SERVICES. "
            "Use asterisk-conference-udp."
        )

    def test_heplify_uses_udp_suffix(self):
        """PR-U-1 added heplify-udp Service to capture Kamailio HEP traffic.
        Must use the -udp suffix because heplify-server's HEP capture
        listens on UDP/9060; the heplify-tcp Service is for the homer-webapp
        UI and config/TLS ports, not for SIP capture."""
        services = {svc for (_ns, svc, _key) in _LB_SERVICES}
        assert "heplify-udp" in services, (
            "heplify-udp missing from _LB_SERVICES. PR-U-1 regression."
        )
        assert "heplify" not in services, (
            "heplify (no suffix) present in _LB_SERVICES. "
            "Use heplify-udp."
        )


class TestServiceCountAndNamespaceUnchanged:
    """PR-T1 originally pinned 5-Service contract; PR-U-1 expanded to 6
    by adding heplify-udp in the infrastructure namespace."""

    def test_six_services(self):
        assert len(_LB_SERVICES) == 6

    def test_namespaces_unchanged(self):
        # PR-U-1 contract: 3 in infrastructure (redis, rabbitmq, heplify-udp),
        # 3 in voip (asterisk-call-udp, -registrar-udp, -conference-udp).
        ns_counts: dict[str, int] = {}
        for (ns, _svc, _key) in _LB_SERVICES:
            ns_counts[ns] = ns_counts.get(ns, 0) + 1
        assert ns_counts == {"infrastructure": 3, "voip": 3}

    def test_output_keys_unchanged(self):
        # PR-T flat-vars in ansible_runner.py depend on these output keys.
        # Changing them would silently re-introduce the CrashLoop bug PR-T fixed.
        keys = {key for (_ns, _svc, key) in _LB_SERVICES}
        assert keys == {
            "redis_lb_ip",
            "rabbitmq_lb_ip",
            "heplify_lb_ip",
            "asterisk_call_lb_ip",
            "asterisk_registrar_lb_ip",
            "asterisk_conference_lb_ip",
        }
