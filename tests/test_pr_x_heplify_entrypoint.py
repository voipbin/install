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

PR-X pins `entrypoint: ["./heplify"]` so `command:` carries only flags,
mirroring the production voip-kamailio-docker pattern.

The tests below pin the contract so a future refactor cannot silently
remove the entrypoint and reintroduce the bug.
"""

from __future__ import annotations

import re
from pathlib import Path


REPO = Path(__file__).resolve().parent.parent
COMPOSE_J2 = REPO / "ansible" / "roles" / "kamailio" / "templates" / "docker-compose.yml.j2"


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


def _heplify_block(text: str) -> str:
    """Return the heplify-client compose service body (`heplify-client:` to
    the next blank-line-separated service or the closing `{% endif %}`).

    Returns the substring starting at the `heplify-client:` line through
    the closing `{% endif %}`. The block is wrapped by a Jinja gate and
    sits at the end of the template, so this captures everything that
    will render into the final compose file when the gate is true.
    """
    m = re.search(r"  heplify-client:.*?\{% endif %\}", text, re.DOTALL)
    assert m, "could not isolate heplify-client block"
    return m.group(0)


class TestHeplifyEntrypoint:
    def test_entrypoint_field_present(self):
        """The block MUST declare `entrypoint:` so compose does not fall
        through to interpreting `command:` first-element as the
        executable. Without this assertion, a future edit that drops the
        entrypoint line slips past CI and breaks the live container
        identically to v6 iteration #5."""
        body = _heplify_block(_read(COMPOSE_J2))
        assert re.search(r"^\s+entrypoint:", body, re.MULTILINE), (
            "heplify-client block is missing `entrypoint:`. The "
            "sipcapture/heplify:1.56 image has no ENTRYPOINT; without "
            "pinning, compose treats `command:` first element as the "
            "executable and the container fails with "
            'exec: "-i": executable file not found in $PATH.'
        )

    def test_entrypoint_pins_heplify_binary(self):
        """The entrypoint must invoke `./heplify` (the binary baked into
        the image at /heplify, working directory is its parent). Any
        deviation — empty list, shell wrapper, wrong path — must fail."""
        body = _heplify_block(_read(COMPOSE_J2))
        # Match `entrypoint: ["./heplify"]` or block-form `entrypoint:\n  - "./heplify"`.
        flow = re.search(r'entrypoint:\s*\[\s*"\./heplify"\s*\]', body)
        block = re.search(r'entrypoint:\s*\n\s+-\s+"\./heplify"\s*$', body, re.MULTILINE)
        assert flow or block, (
            "entrypoint must pin './heplify' as the only executable, "
            "either as `entrypoint: [\"./heplify\"]` (flow form) or "
            "`entrypoint:\\n  - \"./heplify\"` (block form). Found "
            f"block: {body!r}"
        )

    def test_command_first_element_is_a_flag_not_a_binary(self):
        """Defense-in-depth: even with entrypoint pinned, a future refactor
        might restructure `command:` to lead with a binary path. The
        contract this test pins is that `command:` is a list of FLAGS,
        starting with `-i`. If someone later prepends `./heplify` into
        `command:` (the old broken-state shape) AND keeps `entrypoint`,
        argv becomes `./heplify ./heplify -i any -hs ...` which heplify
        rejects."""
        body = _heplify_block(_read(COMPOSE_J2))
        # Isolate the command: block (block-form list expected).
        m = re.search(
            r"^\s+command:\s*\n((?:\s+-\s+\".*\"\s*\n)+)",
            body,
            re.MULTILINE,
        )
        assert m, "could not parse heplify-client command: block"
        first_item = re.search(r'-\s+"([^"]+)"', m.group(1)).group(1)
        assert first_item.startswith("-"), (
            f"heplify-client command: first element must be a CLI flag "
            f"(e.g. `-i`), not a binary path. Got: {first_item!r}. "
            "Prepending the binary while entrypoint is also pinned "
            "produces a malformed argv at runtime."
        )

    def test_entrypoint_precedes_command(self):
        """Stylistic but semantically meaningful: `entrypoint:` should
        appear BEFORE `command:` in the service body so any reviewer
        sees the executable pinning before the flag list. Lexical order
        prevents the silent-omission failure mode."""
        body = _heplify_block(_read(COMPOSE_J2))
        # Match the YAML keys at top-of-service indentation only, NOT the
        # word "command:" inside comments or docstrings.
        ep_match = re.search(r"^\s+entrypoint:", body, re.MULTILINE)
        cmd_match = re.search(r"^\s+command:", body, re.MULTILINE)
        assert ep_match is not None, "entrypoint key missing"
        assert cmd_match is not None, "command key missing"
        assert ep_match.start() < cmd_match.start(), (
            "entrypoint: must appear before command: so the executable "
            "pinning is visually adjacent to the argv it owns."
        )

    def test_no_shell_wrapper_in_entrypoint(self):
        """Reject anti-pattern: `entrypoint: ["sh", "-c", "..."]` which
        would lose argv structure and obscure the actual binary."""
        body = _heplify_block(_read(COMPOSE_J2))
        m = re.search(r"entrypoint:\s*(\[[^\]]+\]|\n(?:\s+-\s+\".*\"\s*\n)+)", body)
        assert m, "entrypoint not found in expected forms"
        ep_text = m.group(1)
        for forbidden in ('"sh"', '"bash"', '"/bin/sh"', '"-c"'):
            assert forbidden not in ep_text, (
                f"entrypoint must NOT use a shell wrapper ({forbidden} found). "
                "Direct exec of ./heplify is required so argv is preserved."
            )
