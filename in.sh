#!/usr/bin/env bash
# Fix 2: a poll that can wait minutes must say it is waiting.
# One commit, verified before it is pushed. Safe to re-run.
#
#   bash fix_progress.sh
#
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

say "tooling"
python -m pip install -q -r tests/requirements-dev.txt
python -c "import pytest" 2>/dev/null || bad "pytest not importable after install"
ok "pytest $(python -c 'import pytest; print(pytest.__version__)')"

# ---------------------------------------------------------------- server.py
say "patch server.py"
python - <<'PY'
import pathlib, sys

HELPER_OLD = '''@mcp.tool()
def wait_for_live(slug: str, timeout: int = 180, interval: int = 10,'''

HELPER_NEW = '''def _progress(what: str, elapsed: float, budget: int, detail: str = "") -> None:
    """Report that a poll is still waiting. Written to stderr, and only to stderr.

    stdout is the MCP protocol stream; writing there corrupts the transport. stderr is
    forwarded to the caller's terminal by stdio_client (its `errlog` parameter
    defaults to sys.stderr), and on the direct route it is simply the terminal. So it
    is the only channel a tool has for saying anything at all before it returns.

    These lines carry no meaning for any caller. The return value is still the entire
    result and nothing parses this output — which is the point: a progress line that
    something depends on is an undeclared protocol, and the next person to reword it
    breaks a consumer nobody knew existed.

    Why it exists. publish_post returns one result at the end, so across the three
    polls here — up to 330 seconds at the default budgets — a correct slow publish and
    a dead process are byte-identical from outside: both are a blank terminal. On
    2026-09-14 that silence was read as a hang and the run was killed four minutes
    after it had already committed, pushed, and gone live; recovering from the
    "failure" cost three hours and republished the post four times. The chain was
    right and unreadable, and unreadable was the expensive half.

    Printed only when about to sleep, never before the first attempt, so a check that
    succeeds immediately stays silent. Waiting is the thing worth announcing; working
    is not.
    """
    print(f"  ... waiting on {what}: {elapsed:.0f}s of {budget}s{detail}",
          file=sys.stderr, flush=True)


@mcp.tool()
def wait_for_live(slug: str, timeout: int = 180, interval: int = 10,'''

PAGE_OLD = '''                    "last_status": last, "error": f"not live after {timeout}s (last seen: {last})"}
        time.sleep(max(interval, 1))'''

PAGE_NEW = '''                    "last_status": last, "error": f"not live after {timeout}s (last seen: {last})"}
        _progress("the page", timeout - (deadline - time.monotonic()), timeout,
                  f" (last status: {last})")
        time.sleep(max(interval, 1))'''

SITEMAP_OLD = '''                f"running, so the blog index and feed may not show this post yet"
            )
            break
        time.sleep(max(interval, 1))'''

SITEMAP_NEW = '''                f"running, so the blog index and feed may not show this post yet"
            )
            break
        _progress("the sitemap", sitemap_timeout - (sm_deadline - time.monotonic()),
                  sitemap_timeout, f" (last status: {sm_status})")
        time.sleep(max(interval, 1))'''

OG_OLD = '''                f"the build-og Action may still be running, or failed to render {og_image}"
            )
            return result
        time.sleep(max(interval, 1))'''

OG_NEW = '''                f"the build-og Action may still be running, or failed to render {og_image}"
            )
            return result
        _progress("the share image", og_timeout - (og_deadline - time.monotonic()),
                  og_timeout, f" (last status: {og_last})")
        time.sleep(max(interval, 1))'''

p = pathlib.Path("server.py")
s = p.read_text(encoding="utf-8")
out = s

# Each edit carries a MARKER that exists only in its patched form. "Already applied?"
# cannot be answered by counting OLD: HELPER_NEW ends with the exact text of
# HELPER_OLD — it inserts a function *above* the line it anchors on — so OLD is still
# present after a successful patch, and a count-based check re-applies it on every
# run. Found by running this script twice and getting two commits.
EDITS = [
    (HELPER_OLD,  HELPER_NEW,  "def _progress("),
    (PAGE_OLD,    PAGE_NEW,    '_progress("the page"'),
    (SITEMAP_OLD, SITEMAP_NEW, '_progress("the sitemap"'),
    (OG_OLD,      OG_NEW,      '_progress("the share image"'),
]

for old, new, marker in EDITS:
    if marker in out:
        continue
    n = out.count(old)
    if n != 1:
        sys.exit(f"  stop  server.py: expected exactly one match for the anchor before "
                 f"{marker!r}, found {n} — file has moved, re-fetch")
    out = out.replace(old, new)

if out == s:
    print("  skip  server.py already patched")
else:
    p.write_text(out, encoding="utf-8")
    sites = out.count("_progress(") - out.count("def _progress(")
    print(f"  ok    server.py patched — _progress called at {sites} poll site(s)")
PY

python -c "import ast,pathlib; ast.parse(pathlib.Path('server.py').read_text(encoding='utf-8'))" \
  || bad "server.py no longer parses — do not commit"
ok "server.py parses"

# ---------------------------------------------------------------- the test
say "pin it with a test"
if grep -q "test_a_waiting_poll_announces_itself" tests/test_chain.py; then
  skip "test already present"
else
  cat >> tests/test_chain.py <<'PYEOF'


def test_a_waiting_poll_announces_itself(blog, corpus, site, srv, fake_web, capfd):
    """A poll that waits says so on stderr, every attempt.

    Bought with an incident, 2026-09-14. publish_post returns one result at the end,
    so across the three polls here — up to 330 seconds at the default budgets — a
    correct slow publish and a dead process are byte-identical from outside: both are
    a blank terminal. That silence was read as a hang. The run was killed four minutes
    after it had already committed, pushed and gone live, and undoing the publish that
    had in fact succeeded cost three hours.

    stderr rather than stdout, and this is not a style choice: stdout is the MCP
    protocol stream and writing to it corrupts the transport. The assertion is
    deliberately on the presence of the waiting, not on its wording — the lines are
    for a human watching a terminal, nothing parses them, and a test that pinned the
    phrasing would make an undeclared protocol out of a diagnostic.
    """
    from conftest import sitemap_xml

    write_post(blog, "slow", site)
    fake_web[f"{site}/blog/slow/"] = 200
    # Live page, but a sitemap that has not caught up and an image that never
    # arrives: wave two lagging, which is exactly when the waiting is long.
    fake_web[f"{site}/sitemap.xml"] = (200, sitemap_xml(site))

    result = srv.wait_for_live("slow", timeout=5, interval=1,
                               og_timeout=3, sitemap_timeout=3)

    assert result["ok"] is True
    assert result["sitemap_listed"] is False
    assert result["og_image_live"] is False

    err = capfd.readouterr().err
    assert "waiting on the sitemap" in err, "a lagging sitemap poll said nothing"
    assert "waiting on the share image" in err, "a lagging image poll said nothing"
PYEOF
  ok "appended to tests/test_chain.py"
fi

# ---------------------------------------------------------------- mutation check
say "mutation check (server.py restored immediately after)"
cp server.py /tmp/_srv_real.py
python - <<'PY'
import pathlib
# Line-based, balancing parens: the calls span two lines and contain nested calls
# and f-string braces, so a regex over the whole text either misses them or eats
# too much. Counting brackets is the thing that is actually true about the syntax.
p = pathlib.Path("server.py")
lines = p.read_text(encoding="utf-8").splitlines(keepends=True)
out, i, removed = [], 0, 0
while i < len(lines):
    if lines[i].lstrip().startswith("_progress("):
        depth = lines[i].count("(") - lines[i].count(")")
        i += 1
        removed += 1
        while depth > 0 and i < len(lines):
            depth += lines[i].count("(") - lines[i].count(")")
            i += 1
        continue
    out.append(lines[i])
    i += 1
assert removed == 3, f"expected to remove 3 progress calls, removed {removed} — mutation check cannot run"
p.write_text("".join(out), encoding="utf-8")
PY
MUT=$(python -m pytest tests/test_chain.py::test_a_waiting_poll_announces_itself -q 2>&1 || true)
cp /tmp/_srv_real.py server.py
rm -f /tmp/_srv_real.py
echo "$MUT" | grep -q "said nothing" \
  || bad $'the mutation check did not produce the expected failure. pytest said:\n'"$MUT"
ok "test fails with the right message when the progress calls are removed"

say "full suite"
python -m pytest -q || bad "suite is red — nothing committed, nothing pushed"
ok "green"

# ---------------------------------------------------------------- demo still clean
# Act 4 narrates the demo's stdout. Progress goes to stderr, so stdout must be
# unchanged — verify rather than assume, since the recording depends on it.
say "demo: stdout unchanged, stderr now carries the waiting"
python demo/run_demo.py >/tmp/_d.out 2>/tmp/_d.err || bad "demo failed"
OUT_LINES=$(wc -l < /tmp/_d.out); ERR_LINES=$(wc -l < /tmp/_d.err)
grep -cE "^ *PASS" /tmp/_d.out | grep -q "^5$" || bad "expected 5 PASS lines on stdout, demo output changed"
grep -q "waiting on" /tmp/_d.err || bad "no progress output during the demo's polls"
ok "stdout $OUT_LINES lines, 5 PASS; stderr $ERR_LINES progress lines"

# ---------------------------------------------------------------- commit
if git diff --quiet -- server.py tests/test_chain.py; then
  skip "already committed"
else
  git add server.py tests/test_chain.py
  git commit -q -m "Say that a poll is waiting

publish_post returns one result at the end, so across wait_for_live's
three polls — up to 330 seconds at the default budgets — a correct slow
publish and a dead process are byte-identical from outside: both are a
blank terminal.

That is not hypothetical. On 2026-09-14 the silence was read as a hang
and the run was killed four minutes after it had already committed,
pushed and gone live. Undoing a publish that had in fact succeeded cost
three hours and four republish cycles.

stderr, not stdout: stdout is the MCP protocol stream and writing to it
corrupts the transport. stdio_client forwards the child's stderr to the
caller's terminal, and on the direct route it is the terminal. Nothing
parses these lines and the test asserts only that the waiting is
announced, not how — a diagnostic something depends on is an undeclared
protocol.

Printed only before sleeping, so a check that succeeds immediately stays
silent. Waiting is worth announcing; working is not.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_013Ai6mP1x3Rv5n9ygKQgidt"
  ok "committed"
fi

say "push"
git push
ok "pushed"

say "done"
git log --oneline -2
