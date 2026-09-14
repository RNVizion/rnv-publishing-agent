#!/usr/bin/env bash
# Adds the stdin claim test to tests/test_refusal.py, verifies, commits, pushes.
# Safe to re-run: it detects its own work and stops rather than duplicating it.
# It will NOT push if the suite is red.
#
#   bash add_stdin_test.sh
#
set -euo pipefail

G="\033[32m"; R="\033[31m"; Y="\033[33m"; C="\033[36m"; N="\033[0m"
say() { echo -e "\n${C}== $* ==${N}"; }
ok()  { echo -e "  ${G}ok${N}    $*"; }
bad() { echo -e "  ${R}stop${N}  $*"; exit 1; }

# ---------------------------------------------------------------- 0. where am I
[ -f server.py ] && [ -d tests ] || bad "run this from the rnv-publishing-agent repo root"
ok "repo root: $(pwd)"

say "sync"
git pull --rebase
ok "up to date with origin"

# ---------------------------------------------------------------- 1. preconditions
say "preconditions"

# The test asserts a property the fix provides. Adding it to a tree without the fix
# turns a green suite red for a reason that looks like the test's fault, so check the
# thing being claimed before claiming it.
N_FIXED=$(grep -c "stdin=subprocess.DEVNULL" server.py || true)
[ "$N_FIXED" -ge 2 ] || bad "server.py has $N_FIXED stdin=subprocess.DEVNULL call(s), expected 2 — the fix commit is missing here"
ok "stdin fix present ($N_FIXED call sites)"

if grep -q "test_every_subprocess_call_declares_its_stdin" tests/test_refusal.py; then
  echo -e "  ${Y}skip${N}  test already present in tests/test_refusal.py"
  ALREADY=1
else
  ALREADY=0
fi

# ---------------------------------------------------------------- 2. append
if [ "$ALREADY" -eq 0 ]; then
  say "append test"
  cat >> tests/test_refusal.py <<'PYEOF'


def test_every_subprocess_call_declares_its_stdin():
    """Every subprocess in server.py must pass stdin explicitly.

    Bought with an incident, 2026-09-14. `capture_output=True` reads as total and is
    not: it redirects stdout and stderr and says nothing about stdin, which stays
    INHERITED. Under the MCP stdio transport server.py's stdin is the pipe the client
    writes JSON-RPC into, so every git call was handed a live transport pipe as its
    standard input. `git status --porcelain` — a local read — took 278.6 seconds and
    returned only when the parent process was interrupted.

    Why this is a source check rather than a behavioural one. The failure needs a real
    stdio transport to appear at all: the direct route has no pipe, so calling
    publish_post in-process is exactly the configuration that cannot reproduce it, and
    a test that spawned a transport would be timing-dependent and slow. The property
    that actually matters is structural and is decidable by reading the file, so read
    the file. A test asserting "this hangs" would have to wait to find out; this one
    knows immediately.

    `ast` rather than a regex on purpose: a regex over source cannot tell a call from
    the string that describes it, and this module's docstrings discuss `subprocess.run`
    by name. Use and mention, again.
    """
    import ast
    import inspect
    import pathlib

    import server

    source = pathlib.Path(inspect.getfile(server)).read_text(encoding="utf-8")
    tree = ast.parse(source)

    calls = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in {"run", "Popen", "call", "check_call", "check_output"}
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "subprocess"
    ]

    assert calls, "found no subprocess calls in server.py — the check is looking in the wrong place"

    missing = [
        node.lineno for node in calls
        if not any(kw.arg == "stdin" for kw in node.keywords)
    ]

    assert not missing, (
        f"subprocess call(s) at line(s) {missing} in server.py do not pass stdin. "
        "capture_output=True does not cover stdin; an inherited stdin under the MCP "
        "stdio transport is a live pipe and git will block on it indefinitely. "
        "Pass stdin=subprocess.DEVNULL."
    )
PYEOF
  ok "appended to tests/test_refusal.py"
fi

# ---------------------------------------------------------------- 3. prove it fails without the fix
# A test that only ever passes proves nothing. Break the property in a scratch copy,
# confirm the test notices, then throw the copy away. Nothing here touches server.py.
say "mutation check (temporary copy; server.py untouched)"
cp server.py /tmp/_srv_real.py
python - <<'PY'
import pathlib, re
p = pathlib.Path("server.py"); s = p.read_text(encoding="utf-8")
s2 = s.replace(",\n                          stdin=subprocess.DEVNULL, env=_git_env())", ")", 1)
s2 = s2.replace(",\n                              stdin=subprocess.DEVNULL, env=_git_env())", ")", 1)
assert s2 != s, "could not break the property — mutation check cannot run"
p.write_text(s2, encoding="utf-8")
PY
if python -m pytest tests/test_refusal.py::test_every_subprocess_call_declares_its_stdin -q >/dev/null 2>&1; then
  cp /tmp/_srv_real.py server.py
  bad "the test PASSED against a broken server.py — it proves nothing, do not commit it"
fi
cp /tmp/_srv_real.py server.py
rm -f /tmp/_srv_real.py
ok "test fails when the property is broken"

# ---------------------------------------------------------------- 4. full suite
say "full suite"
python -m pytest -q || bad "suite is red — nothing committed, nothing pushed"
ok "green"

# ---------------------------------------------------------------- 5. commit and push
if [ "$ALREADY" -eq 1 ] && git diff --quiet tests/test_refusal.py; then
  say "done"
  ok "nothing to commit — already on this branch"
  exit 0
fi

say "commit and push"
git add tests/test_refusal.py
git commit -m "Pin the stdin claim: every subprocess in server.py declares it

A source check rather than a behavioural one, deliberately. The failure
needs a real stdio transport to appear: in-process calls have no pipe, so
the configuration that is easy to test is exactly the one that cannot
reproduce it. The property that matters is structural, so read the
structure. A behavioural test would have to wait to find out; this one
knows immediately.

AST rather than regex, because this module's docstrings now discuss
subprocess.run by name and a source regex cannot tell a call from a
mention of one.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_013Ai6mP1x3Rv5n9ygKQgidt"
git push
ok "pushed"

say "done"
git log --oneline -2
