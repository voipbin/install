#!/usr/bin/env bash
# check-plan-sensitive.sh
#
# Sensitive-data audit gate for `docs/plans/*.md`.
#
# This script greps committed plan files for patterns that would leak
# environment-specific values into a publicly visible repository:
#
#   1. Public IP ranges of the canonical production environment.
#   2. Private IP ranges of the canonical production VPC and GKE CIDRs.
#   3. Identifier strings (GCP project name, GKE cluster name, Cloud
#      SQL instance names, public domains tied to a specific tenant).
#
# Categories 1-3 are NOT hard-coded into this script. They are read
# from `~/.voipbin/sensitive-patterns.txt`, one extended regex per
# line, kept out of version control. This keeps the script itself
# publishable while still giving the maintainer a working audit gate.
#
# If `~/.voipbin/sensitive-patterns.txt` is not present, the script
# falls back to a minimal generic pattern set (RFC 1918 private IPs
# referenced in plan body and obvious "production"-suffixed
# identifiers). This minimum is intentionally weak; the maintainer
# should always set up the full pattern file.
#
# Usage:
#   scripts/dev/check-plan-sensitive.sh             # check all plan files
#   scripts/dev/check-plan-sensitive.sh path/to/p.md  # check one file
#
# Exit codes:
#   0  no hits
#   1  one or more hits (offending file:line printed to stderr)
#   2  invocation error (no plan files found, etc.)

set -euo pipefail

PATTERN_FILE="${VOIPBIN_SENSITIVE_PATTERNS:-$HOME/.voipbin/sensitive-patterns.txt}"

if [[ "${1:-}" == "--help" || "${1:-}" == "-h" ]]; then
  sed -n '2,32p' "$0"
  exit 0
fi

if [[ $# -ge 1 ]]; then
  TARGETS=("$@")
else
  mapfile -t TARGETS < <(find docs/plans -type f -name '*.md' 2>/dev/null)
fi

if [[ ${#TARGETS[@]} -eq 0 ]]; then
  echo "check-plan-sensitive: no plan files found" >&2
  exit 2
fi

PATTERNS=()

if [[ -r "$PATTERN_FILE" ]]; then
  while IFS= read -r line; do
    [[ -z "$line" || "$line" =~ ^# ]] && continue
    PATTERNS+=("$line")
  done < "$PATTERN_FILE"
else
  echo "check-plan-sensitive: WARNING $PATTERN_FILE not found; using minimal fallback patterns" >&2
  PATTERNS+=('10\.[0-9]+\.[0-9]+\.[0-9]+')
  PATTERNS+=('172\.(1[6-9]|2[0-9]|3[01])\.[0-9]+\.[0-9]+')
  PATTERNS+=('192\.168\.[0-9]+\.[0-9]+')
  PATTERNS+=('-production[^-a-zA-Z]?')
fi

# Allow RFC 5737 documentation prefixes and obvious placeholders.
ALLOW_REGEX='203\.0\.113\.|198\.51\.100\.|192\.0\.2\.|10\.0\.0\.|<your-|example\.com|PLACEHOLDER'

EXIT=0
HITS_FILE=$(mktemp -t psens_hits.XXXXXX)
trap 'rm -f "$HITS_FILE"' EXIT

for target in "${TARGETS[@]}"; do
  for pat in "${PATTERNS[@]}"; do
    # For each line matching the sensitive pattern, isolate just the
    # offending tokens and check those (not the full line) against the
    # allow list. This avoids the "co-occurring allowlist token on the
    # same line silently whitelists a real production identifier" bypass.
    if grep -nEo "$pat" "$target" 2>/dev/null | grep -vE "$ALLOW_REGEX" > "$HITS_FILE"; then
      if [[ -s "$HITS_FILE" ]]; then
        echo "check-plan-sensitive: HIT in $target for pattern: $pat" >&2
        # Re-emit the original full-line matches for human context.
        grep -nE "$pat" "$target" 2>/dev/null | sed 's/^/    /' >&2
        EXIT=1
      fi
    fi
    : > "$HITS_FILE"
  done
done

if [[ $EXIT -eq 0 ]]; then
  echo "check-plan-sensitive: OK (${#TARGETS[@]} file(s), ${#PATTERNS[@]} pattern(s))"
fi

exit "$EXIT"
