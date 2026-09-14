#!/usr/bin/env bash
# Two commits, verified before either is pushed:
#   1. repair the dev-requirements path after the move to tests/
#   2. pin the stdin claim with a test
#
#   bash fix_and_test.sh
#
# Safe to re-run: every edit is guarded and detects work already done.
# Nothing is pushed if the suite is red.
set -euo pipefail

G="\033[32m"; R="\033[31m"; Y="\033[33m"; C="\033[36m"; N="\033[0m"
say() { echo -e "\n${C}== $* ==${N}"; }
ok()  { echo -e "  ${G}ok${N}    $*"; }
skip(){ echo -e "  ${Y}skip${N}  $*"; }
bad() { echo -e "  ${R}stop${N}  $*"; exit 1; }

[ -f server.py ] && [ -d tests ] || bad "run this from the rnv-publishing-agent repo root"
ok "repo root: $(pwd)"

say "sync"
git pull --rebase --autostash
ok "up to date with origin"

# =================================================================== PART 1
# The file moved to tests/ and three references did not follow it. Worse, the file
# itself did not survive the move: pip resolves a nested `-r` relative to the
# requirements file's OWN directory, so `-r requirements.txt` inside tests/ means
# tests/requirements.txt, which does not exist. The file was broken from every
# location, including the one CI uses.
say "part 1 — dev requirements path"

[ -f tests/requirements-dev.txt ] || bad "tests/requirements-dev.txt not found — is this branch current?"

python - <<'PY'
import pathlib, sys

EDITS = [
    # Anchored on newlines: the file's own comment mentions `-r requirements.txt`
    # while explaining why pytest is separate, so the bare string matches twice.
    # Match the line that does something, not the prose that describes it.
    ("tests/requirements-dev.txt",
     "\n-r requirements.txt\n",
     "\n-r ../requirements.txt\n"),
    ("README.md",
     "pip install -r requirements.txt    # add -r requirements-dev.txt for the test tooling",
     "pip install -r requirements.txt          # add -r tests/requirements-dev.txt for the test tooling"),
    ("README.md",
     "pip install -r requirements-dev.txt\npython -m pytest tests -q",
     "pip install -r tests/requirements-dev.txt\npython -m pytest tests -q"),
    (".github/workflows/ci.yml",
     "          pip install -r requirements-dev.txt",
     "          pip install -r tests/requirements-dev.txt"),
]

changed = []
for path, old, new in EDITS:
    p = pathlib.Path(path)
    s = p.read_text(encoding="utf-8")
    if s.count(new) >= 1 and s.count(old) == 0:
        print(f"  skip  {path}: already updated")
        continue
    n = s.count(old)
    if n != 1:
        sys.exit(f"  stop  {path}: expected exactly one match for\n        {old!r}\n        found {n} — file has moved, re-fetch")
    p.write_text(s.replace(old, new), encoding="utf-8")
    changed.append(path)
    print(f"  ok    {path}: updated")

print("CHANGED:" + ",".join(changed))
PY

# Prove the requirements file actually resolves now, rather than assuming the edit
# was sufficient. --dry-run resolves every nested -r and installs nothing.
say "verify pip resolves it"
python -m pip install -q --dry-run -r tests/requirements-dev.txt >/dev/null 2>&1 \
  || bad "pip still cannot resolve tests/requirements-dev.txt — do not commit"
ok "tests/requirements-dev.txt resolves"

say "install test tooling"
python -m pip install -q -r tests/requirements-dev.txt
python -c "import pytest" 2>/dev/null || bad "pytest still not importable after install"
ok "pytest $(python -c 'import pytest; print(pytest.__version__)')"

if git diff --quiet -- tests/requirements-dev.txt README.md .github/workflows/ci.yml; then
  skip "part 1 already committed — nothing to commit"
else
  git add tests/requirements-dev.txt README.md .github/workflows/ci.yml
  git commit -q -m "Repair the dev requirements path after the move to tests/

The file moved and three references did not follow it: README twice and
the CI test job's Install step, all still naming the repo root. That path
404s, so the install step could not have been succeeding.

The file also did not survive its own move. pip resolves a nested -r
relative to the requirements file's directory, so \`-r requirements.txt\`
inside tests/ means tests/requirements.txt, which does not exist — the
file was unusable from every location, including CI's. Now ../.

The move was invisible because nothing reads a path until it runs, and
the one consumer that runs it on every push reports as a job failure
rather than as a missing file.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_013Ai6mP1x3Rv5n9ygKQgidt"
  ok "part 1 committed"
fi

# =================================================================== PART 2
say "part 2 — pin the stdin claim"

N_FIXED=$(grep -c "stdin=subprocess.DEVNULL" server.py || true)
[ "$N_FIXED" -ge 2 ] || bad "server.py has $N_FIXED stdin=subprocess.DEVNULL call(s), expected 2 — the fix commit is missing here"
ok "stdin fix present ($N_FIXED call sites)"

if grep -q "test_every_subprocess_call_declares_its_stdin" tests/test_refusal.py; then
  skip "test already present"
else
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

# A test that only ever passes proves nothing. Break the property in place, require
# the EXPECTED message — not merely a non-zero exit, since a missing pytest and an
# import error are also non-zero — then restore from the copy.
say "mutation check (server.py restored immediately after)"
cp server.py /tmp/_srv_real.py
python - <<'PY'
import pathlib
p = pathlib.Path("server.py"); s = p.read_text(encoding="utf-8")
s2 = s.replace(",\n                          stdin=subprocess.DEVNULL, env=_git_env())", ")", 1)
s2 = s2.replace(",\n                              stdin=subprocess.DEVNULL, env=_git_env())", ")", 1)
assert s2 != s, "could not break the property — mutation check cannot run"
p.write_text(s2, encoding="utf-8")
PY
MUT=$(python -m pytest tests/test_refusal.py::test_every_subprocess_call_declares_its_stdin -q 2>&1 || true)
cp /tmp/_srv_real.py server.py
rm -f /tmp/_srv_real.py
echo "$MUT" | grep -q "do not pass stdin" \
  || bad $'the mutation check did not produce the expected failure. pytest said:\n'"$MUT"
ok "test fails with the right message when the property is broken"

say "full suite"
python -m pytest -q || bad "suite is red — part 2 not committed, nothing pushed"
ok "green"

if git diff --quiet -- tests/test_refusal.py; then
  skip "part 2 already committed"
else
  git add tests/test_refusal.py
  git commit -q -m "Pin the stdin claim: every subprocess in server.py declares it

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
  ok "part 2 committed"
fi

say "push"
git push
ok "pushed"

say "done"
git log --oneline -3
echo
echo -e "${Y}Check the Actions tab: the test job's Install step should now succeed.${N}"
