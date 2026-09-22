#!/usr/bin/env bash
# Apply the 2026-09-22 preflight change set. Run from the agent repo root.
#
# Two guards, at two granularities, because they refuse different things:
#
#   1. The blob guard below refuses if tools/preflight.py is not byte-identical
#      to what this change was cut from. `git hash-object` applies the
#      repository's own filters, so it answers the question git itself would
#      answer and a core.autocrlf=true checkout matches as it should.
#   2. `git apply --check` refuses if any hunk's context has moved.
#
#   Measured, so the scope is stated rather than assumed: an edit inside a hunk's
#   context is refused by both. An append at end of file is tolerated by
#   `git apply --check` — correctly, it collides with nothing — and caught only
#   by the blob guard. The blob guard is therefore not redundant.
#
# HEAD is deliberately NOT asserted. The change touches one file and one new
# path; a commit elsewhere in the repo is not a reason to refuse.
set -euo pipefail

EXPECTED=bf9f15bd96cbff9651ddc13fc4f9a3b840f35022   # tools/preflight.py @ c9579a8
PATCH=${1:?usage: apply-rev13.sh /path/to/rev13-preflight.patch}

# Ordered so a re-run gets the accurate diagnosis. Applying the change moves the
# blob, so the guard below would otherwise report "has moved" at someone who had
# simply run this twice — true, and the wrong thing to tell them.
if [ -e tests/test_preflight.py ]; then
  echo "REFUSED: tests/test_preflight.py already exists — this looks applied." >&2
  echo "  git diff --stat; git log --oneline -1" >&2
  exit 1
fi

actual=$(git hash-object -- tools/preflight.py)
if [ "$actual" != "$EXPECTED" ]; then
  echo "REFUSED: tools/preflight.py has moved since this change was cut." >&2
  echo "  expected blob $EXPECTED" >&2
  echo "  found         $actual" >&2
  echo "  Re-read the file and re-cut; do not force this." >&2
  exit 1
fi

git apply --check "$PATCH"
git apply "$PATCH"
echo "applied: tools/preflight.py updated, tests/test_preflight.py added"
