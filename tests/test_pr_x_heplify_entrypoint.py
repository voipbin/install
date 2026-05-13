"""PR-X regression test: heplify sidecar entrypoint pinned.

Background: PR-U-3 (#46) added the heplify-client sidecar to the Kamailio
docker-compose template using `command: [-i, any, -hs, ...]` but did NOT
specify `entrypoint:`. The sipcapture/heplify:1.56 image leaves
ENTRYPOINT empty and CMD = `./heplify -h`. With no entrypoint pinned,
compose-v2 treats the first element of `command:` (`-i`) as the executable
to exec, producing:

    exec: "-i": executable file not found in $PATH

at container startup. This surfaced in v6 dogfood iteration #5 (May 13
2026), blocking ansible_run from completing.

PR-X pins `entrypoint: ["./heplify"]` so `command:` carries only flags.

The tests below pin the contract so a future refactor cannot silently
remove the entrypoint and reintroduce the bug.

Approach (revised after iter-2 review feedback):
- Parse the Jinja template AND render it with a representative variable
  context, then YAML-load the result. Two layers of validation: template-
  source assertions (regex on .j2) for fast-fail clarity, and rendered-
  YAML assertions (structural via yaml.safe_load) to catch mutants the
  source-only regex misses (e.g. image tag drift, command-value swaps).
- Pin the image tag exactly so M10-style mutants (tag bumps) are caught.
- Pin command argv VALUES (not just shape) so M12-style mutants (drop
  -hs / blank ${HOMER_URI}) are caught.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

try:
    from jinja2 import Environment, FileSystemLoader
    import yaml
    _RENDER_AVAILABLE = True
except ImportError:  # pragma: no cover -- defensive
    _RENDER_AVAILABLE = False


REPO = Path(__file__).resolve().parent.parent
COMPOSE_J2_DIR = REPO / "ansible" / "roles" / "kamailio" / "templates"
COMPOSE_J2 = COMPOSE_J2_DIR / "docker-compose.yml.j2"


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


def _heplify_block(text: str) -> str:
    """Return the heplify-client compose service body, bounded by the
    OUTER `{% endif %}` that closes the `{% if homer_enabled ... %}` gate
    introduced by PR-U-3.

    iter-2 review caught: a non-greedy `.*?{% endif %}` would silently
    truncate if a future PR nested another `{% if %}...{% endif %}` inside
    the heplify-client body. We pair the outer gate explicitly: find the
    `{% if homer_enabled ... and heplify_lb_ip %}` and the LAST
    `{% endif %}` after it that is at the outer indentation.
    """
    open_match = re.search(
        r"\{% if homer_enabled\s*\|\s*bool\s+and\s+heplify_lb_ip\s*%\}",
        text,
    )
    assert open_match is not None, "could not find heplify-client jinja gate open"
    # The PR-U-3 block places `{% endif %}` at column 0 to balance the
    # opening at column 0. Locate the next column-0 `{% endif %}` after
    # the gate open.
    rest = text[open_match.end():]
    close_match = re.search(r"^\{% endif %\}", rest, re.MULTILINE)
    assert close_match is not None, "could not find heplify-client jinja gate close"
    return text[open_match.start(): open_match.end() + close_match.end()]


def _render_compose(homer_enabled: bool = True, heplify_lb_ip: str = "10.99.0.1") -> str:
    """Render the Jinja template with a representative variable set.

    Mirrors what ansible would supply on the dogfood VM. If a future
    refactor changes the variable surface, this function must be updated
    in lockstep — that itself is a useful regression signal.

    Ansible ships a `bool` Jinja filter that stock jinja2 does not have.
    We register a minimal compatible implementation locally so the
    template renders identically to ansible's behavior.
    """
    def _ansible_bool(value):
        # Match ansible's `bool` filter: truthy strings ('true','yes','1',
        # 'on') case-insensitive map to True; falsy strings to False;
        # native Python bool passes through.
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            return bool(value)
        if isinstance(value, str):
            return value.strip().lower() in {"true", "yes", "1", "on", "y", "t"}
        return bool(value)

    env = Environment(
        loader=FileSystemLoader(str(COMPOSE_J2_DIR)),
        keep_trailing_newline=True,
    )
    env.filters["bool"] = _ansible_bool
    tmpl = env.get_template("docker-compose.yml.j2")
    ctx = {
        "homer_enabled": homer_enabled,
        "heplify_lb_ip": heplify_lb_ip,
        # Default-ish values for the other vars referenced by the template.
        "kamailio_internal_lb_ip": "10.0.0.2",
        "kamailio_external_ips": ["34.55.250.28"],
        "sip_listen_addr_ip": "0.0.0.0",
        "sip_listen_addr_port": 5060,
        "rtpengine_socks": "udp:127.0.0.1:22222",
        "kamailio_auth_db_url": "mysql://u:p@h/d",
        "redis_cache_address": "10.0.0.8",
        "redis_password": "x",
        "homer_uri": f"udp:{heplify_lb_ip}:9060",
        "kamailio_internal_ip": "10.0.0.3",
        "kamailio_external_ip": "34.55.250.28",
        "kamailio_proxy_call_ip": "10.0.0.10",
        "kamailio_proxy_registrar_ip": "10.0.0.14",
        "kamailio_proxy_conference_ip": "10.0.0.12",
        "kamailio_proxy_call_port": 5060,
        "kamailio_proxy_registrar_port": 5060,
        "kamailio_proxy_conference_port": 5060,
        "kamailio_internal_lb_name": "kamailio-internal-lb",
    }
    return tmpl.render(**ctx)


# ============================================================================
# Source-level regex assertions (fast-fail, work without jinja2/yaml).
# ============================================================================


class TestHeplifyEntrypointSource:
    def test_entrypoint_field_present(self):
        """The block MUST declare `entrypoint:` so compose does not fall
        through to interpreting `command:` first-element as the
        executable."""
        body = _heplify_block(_read(COMPOSE_J2))
        assert re.search(r"^\s+entrypoint:", body, re.MULTILINE), (
            "heplify-client block is missing `entrypoint:`. The "
            "sipcapture/heplify:1.56 image has no ENTRYPOINT; without "
            "pinning, compose treats `command:` first element as the "
            "executable and the container fails with "
            'exec: "-i": executable file not found in $PATH.'
        )

    def test_entrypoint_pins_heplify_binary(self):
        """The entrypoint must invoke `./heplify` only. Both flow form
        and block form are accepted; anything else fails."""
        body = _heplify_block(_read(COMPOSE_J2))
        flow = re.search(r'entrypoint:\s*\[\s*"\./heplify"\s*\]', body)
        block = re.search(r'entrypoint:\s*\n\s+-\s+"\./heplify"\s*$', body, re.MULTILINE)
        assert flow or block, (
            "entrypoint must pin './heplify' as the only executable. Got "
            f"block: {body!r}"
        )

    def test_entrypoint_precedes_command(self):
        """`entrypoint:` should appear before `command:` for readability.
        Matches only top-of-service indented keys, not the word
        'command' inside comments."""
        body = _heplify_block(_read(COMPOSE_J2))
        ep_match = re.search(r"^\s+entrypoint:", body, re.MULTILINE)
        cmd_match = re.search(r"^\s+command:", body, re.MULTILINE)
        assert ep_match is not None and cmd_match is not None
        assert ep_match.start() < cmd_match.start()


# ============================================================================
# Rendered-YAML structural assertions (catch source-only-blind mutants).
# ============================================================================


@pytest.mark.skipif(not _RENDER_AVAILABLE, reason="jinja2 and pyyaml required")
class TestHeplifyEntrypointRendered:
    def test_rendered_yaml_has_entrypoint(self):
        """Render the template and parse the YAML. Assert the
        heplify-client service has `entrypoint: ["./heplify"]`. This is
        the strongest possible contract — it tests what actually lands
        on the VM."""
        rendered = _render_compose()
        doc = yaml.safe_load(rendered)
        services = doc.get("services") or {}
        assert "heplify-client" in services, (
            "heplify-client service missing from rendered compose. The "
            "Jinja gate may have evaluated false or the block was removed."
        )
        ep = services["heplify-client"].get("entrypoint")
        assert ep == ["./heplify"], (
            f"heplify-client entrypoint must be ['./heplify'], got: {ep!r}. "
            "The sipcapture/heplify:1.56 image has empty ENTRYPOINT; "
            "without this exact pinning, compose-v2 will exec the first "
            "element of `command:` as the binary."
        )

    def test_rendered_image_pinned_to_known_tag(self):
        """Pin the image AND tag. The bug class is image-specific: a
        future bump (e.g. :2.0) may ship its own ENTRYPOINT and make
        our pin redundant or wrong. Catching the bump here forces a
        review of whether `entrypoint:` is still needed."""
        rendered = _render_compose()
        doc = yaml.safe_load(rendered)
        image = doc["services"]["heplify-client"]["image"]
        assert image == "sipcapture/heplify:1.56", (
            f"heplify-client image must be exactly 'sipcapture/heplify:1.56', "
            f"got: {image!r}. If you bumped the tag intentionally, also "
            "verify `docker inspect <image> --format '{{.Config.Entrypoint}}'` "
            "and decide whether the entrypoint pin is still needed; update "
            "this test in lockstep."
        )

    def test_rendered_command_argv_is_full_flag_set(self):
        """Pin the command argv VALUES, not just shape. iter-2 review
        showed that argv-shape-only tests miss mutants like dropping
        `-hs` + ${HOMER_URI} (heplify runs but never ships to HOMER) or
        substituting an empty string for ${HOMER_URI}.

        We assert the exact 8-token argv that production parity requires.
        ${HOMER_URI} is rendered to the docker-compose .env-substitution
        token literal '${HOMER_URI}' because compose templates use
        bash-style expansion at container-up time, NOT at jinja render
        time. So we expect the literal string '${HOMER_URI}' here."""
        rendered = _render_compose()
        doc = yaml.safe_load(rendered)
        cmd = doc["services"]["heplify-client"]["command"]
        expected = ["-i", "any", "-hs", "${HOMER_URI}",
                    "-m", "SIP", "-dim", "REGISTER"]
        assert cmd == expected, (
            f"heplify-client command argv mismatch.\n"
            f"  expected: {expected}\n  got:      {cmd}\n"
            "Each flag must be present in this exact order to preserve "
            "production capture behavior."
        )

    def test_rendered_command_first_arg_is_flag_not_binary(self):
        """Defense-in-depth: even if someone prepends ./heplify into
        `command:` while keeping `entrypoint:` pinned, the argv becomes
        `./heplify ./heplify -i ...` which heplify rejects. Cmd[0] must
        be a CLI flag."""
        rendered = _render_compose()
        doc = yaml.safe_load(rendered)
        cmd = doc["services"]["heplify-client"]["command"]
        assert cmd and isinstance(cmd[0], str) and cmd[0].startswith("-"), (
            f"command[0] must be a CLI flag (e.g. '-i'); got {cmd[0]!r}. "
            "Prepending a binary path while entrypoint is pinned produces "
            "a malformed argv."
        )

    def test_rendered_entrypoint_not_shell_wrapper(self):
        """Forbid shell-wrapped entrypoints. argv preservation requires
        direct exec of ./heplify. Token-equality check (not substring)
        catches `/bin/bash`, `/usr/bin/dash`, busybox sh, etc."""
        rendered = _render_compose()
        doc = yaml.safe_load(rendered)
        ep = doc["services"]["heplify-client"]["entrypoint"]
        assert isinstance(ep, list) and len(ep) == 1, (
            f"entrypoint must be a single-element list, got: {ep!r}"
        )
        forbidden_basenames = {
            "sh", "bash", "dash", "ash", "ksh", "zsh", "fish", "busybox",
        }
        binary_path = ep[0]
        basename = binary_path.rsplit("/", 1)[-1]
        assert basename not in forbidden_basenames, (
            f"entrypoint must NOT use a shell wrapper; got {binary_path!r}. "
            "Direct exec of ./heplify is required so argv is preserved."
        )

    def test_rendered_omits_sidecar_when_homer_disabled(self):
        """The Jinja gate must DROP the entire heplify-client service
        when homer_enabled=false. Pinning the gate prevents a future
        refactor from leaking the sidecar into HOMER-off installs."""
        rendered = _render_compose(homer_enabled=False)
        doc = yaml.safe_load(rendered)
        services = doc.get("services") or {}
        assert "heplify-client" not in services, (
            "heplify-client must not render when homer_enabled is false; "
            f"got services keys: {sorted(services.keys())}"
        )

    def test_rendered_omits_sidecar_when_heplify_lb_ip_empty(self):
        """If the LoadBalancer IP has not been harvested yet, the gate
        must drop the sidecar — otherwise compose would substitute an
        empty ${HOMER_URI} and the container starts with a broken
        capture target."""
        rendered = _render_compose(heplify_lb_ip="")
        doc = yaml.safe_load(rendered)
        services = doc.get("services") or {}
        assert "heplify-client" not in services, (
            "heplify-client must not render when heplify_lb_ip is empty; "
            f"got services keys: {sorted(services.keys())}"
        )


# ============================================================================
# Production-divergence pin: production voip-kamailio-docker has no
# entrypoint either, so we deliberately differ here. The comment block
# documents this; this test ensures the documentation does not regress.
# ============================================================================


class TestProductionDivergenceDocumented:
    def test_comment_documents_production_divergence(self):
        """The original PR claimed parity with production, which was
        false (production voip-kamailio-docker also omits entrypoint).
        The corrected comment block documents the divergence. Pin the
        documentation so a future cleanup pass cannot strip it."""
        body = _heplify_block(_read(COMPOSE_J2))
        # The comment must mention the dogfood verification (docker
        # inspect evidence) AND the production divergence so a future
        # maintainer understands why we pin entrypoint here even though
        # production does not.
        assert "Config.Entrypoint" in body and "Config.Cmd" in body, (
            "Comment must reference the docker inspect verification that "
            "proves the image's ENTRYPOINT is empty. This is the empirical "
            "evidence iter-1 review demanded."
        )
        assert "production" in body.lower() and "compose-v2" in body.lower() and "diverges" in body.lower(), (
            "Comment must explicitly note the production-vs-compose-v2 "
            "divergence (with the word 'diverges') so a future maintainer "
            "doesn't accidentally remove the pinning to 'match production'."
        )
