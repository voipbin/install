# PR-AC-1 Design — kamailioro password: raw (no percent-encode)

**Status:** D1 (single-axis — root cause confirmed live, 1-line behavioural change)
**Author:** Hermes (CPO)
**Parent:** PR-AB-1 (#54, merged as e94e7ce)
**Branch:** `NOJIRA-PR-AC-1-kamailio-mysql-password-raw`

## Goal

Make Kamailio successfully authenticate to Cloud SQL as `kamailioro` so
the Kamailio container's healthcheck passes and `ansible_run` finishes
the kamailio role.

## Background

Dogfood iter#11 (post-PR-AB-1 merge) advanced through ALL cert-deploy
ansible tasks (PR-Z/AA/AB/AB-1 validated end-to-end) and reached
`docker compose up`. The kamailio container CrashLoops with:

```
db_mysql: Access denied for user 'kamailioro'@'10.0.0.3' (using password: YES)
```

Live diagnosis (operator session, 2026-05-13):

| Password form | mysql client auth result |
|---|---|
| `terraform output -raw cloudsql_mysql_password_kamailioro` (raw, 24 chars) | **SELECT 1 → 1 (PASS)** |
| `urllib.parse.quote(raw, safe="!*-._~")` (current `.env` value, 28 chars due to two `+` → `%2B`) | **Access denied** |

Three-place hash comparison:

| Source | SHA-256[0:12] | Length |
|---|---|---|
| Cloud SQL `kamailioro` user password (raw) | `55e3888c57a3` | 24 |
| `install` .env on VM | `0903e271a342` | 28 |
| `secrets.yaml` (no kamailioro entry — SHA of empty) | `e3b0c44298fc` | 0 |

The bug: `scripts/ansible_runner.py:_build_kamailio_auth_db_url` percent-
encodes the password into a URL-shaped `KAMAILIO_AUTH_DB_URL` env var
before writing `.env`. Kamailio's `db_mysql` driver (which delegates to
libmysqlclient via the URL string) does NOT percent-decode the password
component; it passes the literal bytes through. Result: the byte
sequence MySQL receives differs from the byte sequence stored as the
user's password, by exactly the two `+` → `%2B` substitutions
(2 chars × +2 bytes = +4 length).

The original code comment acknowledges this risk in the wrong direction:

> "avoids ambiguity with MySQL URL parsers that treat '+' as form-encoded space"

This concern applies to query-string parsers (HTTP form-encoding
convention), NOT to MySQL connection-string parsers. libmysqlclient is
not a URL parser; it splits on `://`, `@`, `:`, `/` and treats the
password component as opaque bytes.

## Scope

### In

- `scripts/ansible_runner.py:_build_kamailio_auth_db_url`: emit the raw
  password (no percent-encoding) in the URL. The locked alphabet regex
  guard (`_KAMAILIORO_URL_ALPHABET_RE`) already excludes characters that
  would actually break URL parsing (`@`, `:`, `/`, `?`, `#`, space, `%`),
  so raw-emit is safe.
- Update inline code comments to document the corrected understanding.
- Update `docs/operations/cloudsql-credentials.md` to reflect the
  corrected URL escape policy.
- Add `tests/test_pr_ac_1_kamailio_auth_url_raw_password.py` with three
  regression guards.

### Out

- Changing the terraform `random_password.mysql_kamailioro` alphabet.
  The current alphabet (`A-Za-z0-9` + `!*+-._~`) is fine; the bug is in
  the consumer (install repo's URL builder), not the source. Touching
  terraform would rotate the password and cascade to live MySQL.
- Adding application-level percent-decode in Kamailio config. Kamailio
  is a downstream consumer; install repo is the source of truth for
  the URL format.
- Changing the URL scheme away from `mysql://`. The scheme is
  consumed verbatim by Kamailio's db_mysql driver.

### Non-goals

- Generalising encoding policy for OTHER password-bearing URLs in the
  repo. `_build_kamailio_auth_db_url` is the only call site in
  `scripts/ansible_runner.py` today (grep verified). If similar URL
  builders are added later, they should follow the same raw-pass policy
  and the test will enforce it via a repo-wide guard.

## Design

### Code change in `scripts/ansible_runner.py`

```python
def _build_kamailio_auth_db_url(
    config: InstallerConfig,
    terraform_outputs: dict[str, Any],
) -> str:
    """Return the kamailio_auth_db_url for the env.j2 template.

    The password is emitted RAW (no percent-encoding). Kamailio's
    db_mysql driver does not percent-decode the password component;
    percent-encoding the password causes 'Access denied' at runtime
    (dogfood iter#11 lesson, 2026-05-13).

    The locked alphabet regex (_KAMAILIORO_URL_ALPHABET_RE) excludes
    URL-structural characters (':', '/', '@', '?', '#', space, '%'),
    so raw emission is safe.
    """
    raw_password = terraform_outputs.get("cloudsql_mysql_password_kamailioro", "")
    if raw_password is None:
        raw_password = ""
    mysql_host = str(config.get("cloudsql_private_ip", "") or "").strip()
    if not raw_password or not mysql_host:
        return ""
    if not _KAMAILIORO_URL_ALPHABET_RE.match(raw_password):
        raise RuntimeError(
            "kamailioro password contains characters outside the locked "
            "URL-safe alphabet (A-Za-z0-9 + '!*+-._~'). Update terraform "
            "override_special and this URL builder together. "
            "See docs/operations/cloudsql-credentials.md."
        )
    return f"mysql://kamailioro:{raw_password}@{mysql_host}:3306/asterisk"
```

Differences from current code:
- Drop `encoded = _urlquote(raw_password, safe="!*-._~")`.
- Use `raw_password` directly in the f-string.
- Comment rewrite to document corrected understanding.

## Trade-offs

| Decision | Pro | Con |
|---|---|---|
| Emit raw password | Matches Kamailio db_mysql byte semantics; tested live | If alphabet is ever widened to include URL-structural chars without updating the regex, .env breaks; current regex is tight enough |
| Vs. keep encoding, ask Kamailio to decode | Cleaner URL hygiene | Requires monorepo voip-kamailio entrypoint change; install repo cannot dictate downstream parsing |
| Vs. change alphabet to exclude `+` | Encoding becomes no-op | Rotates Cloud SQL password; risky during live debugging cycle; doesn't fix the principle |

## Test surface

| Test | What it pins | Synthetic injection result |
|---|---|---|
| `test_url_contains_raw_password_unencoded` | Given a stub password with `+` characters, build URL, assert the URL contains the raw password literally and does NOT contain `%2B` | FAIL if quote() is re-introduced |
| `test_alphabet_guard_rejects_url_structural_chars` | Password containing `@`/`:`/`/`/`?`/`#`/space/`%` MUST raise RuntimeError. The locked alphabet guard is the only thing keeping raw-emit safe | FAIL if guard regex weakened |
| `test_url_shape_full_template` | End-to-end: stub TF outputs + config → URL exactly equals `mysql://kamailioro:<raw>@<host>:3306/asterisk` | FAIL if any URL segment renders wrong |

## Acceptance criteria

1. Three new tests in `tests/test_pr_ac_1_kamailio_auth_url_raw_password.py`.
2. Programmatic mutant matrix in PR body (re-generated, not hand-classified).
3. `pytest tests/ -q --ignore=tests/test_pr_w_conftest_import_shim.py` →
   928 passed (925 baseline + 3 new).
4. `bash scripts/dev/check-plan-sensitive.sh` → OK.
5. `git merge-tree` vs `origin/main` → no conflicts.
6. R1 + R2 + R3 review iterations.

## Live verification post-merge

Iter#12 must show:
- `cert_provision` short-circuit (existing certs valid).
- `ansible_run` reaches `Start Kamailio Docker Compose stack` and the
  kamailio container reaches `Up (healthy)` instead of `Restarting`.
- If kamailio container reaches healthy: continue through RTPEngine role.

## Abort criteria

- If iter#12 still fails with `Access denied for user 'kamailioro'` but
  different password hash, diagnosis was incomplete — investigate
  before opening another fix PR.
- If iter#12 fails on a DIFFERENT kamailio container error (e.g.
  asterisk db connection, websocket setup), that is a new category
  and gets its own PR — do not expand PR-AC-1.

## Risks

| Risk | Mitigation |
|---|---|
| Future password regeneration introduces `%` directly into the alphabet | Test #2 (alphabet guard rejects `%`) catches; terraform's `override_special` does not include `%` by default |
| Other consumers of this URL (e.g. asterisk) might percent-decode | Out of scope; this PR fixes Kamailio specifically. If a future asterisk integration needs encoded, it gets its own builder |
| The "live verified" claim might not survive a Cloud SQL password rotation | Test on raw-emit semantics survives rotation; only the live login test would need re-running |

## Carry-forward to PR-AC (skill patches still owed)

The PR-AC scope continues to accumulate lessons that need to land in
the `voipbin-install-dogfood-loop` skill:

- Programmatic mutant table regeneration MANDATORY before PR open
  (PR-X/AA/AB recurrence)
- `delegate_to: localhost` pairs with `become: false`; `synchronize`
  push to root-owned dest needs `rsync_path: "sudo rsync"`
- Ansible-touching PRs need live-iter validation; static YAML mutants
  cannot derive runtime privilege semantics (PR-AB-1 lesson)
- **NEW: URL-encoding policy for connection strings must align with
  the consumer's parser, not with web-form RFC conventions. Verify
  live before designing the encoder.** (PR-AC-1 lesson)
- R2 PR-AB-1 carry-forward minors (absolute-path sudo / Jinja
  rsync_path / `/srv/` gap / pull-mode dest-only)
