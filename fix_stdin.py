#!/usr/bin/env python3
"""Fix: never hand a subprocess an inherited stdin it doesn't need.

Run from the rnv-publishing-agent repo root:   python fix_stdin.py
Undo:                                          python fix_stdin.py --undo

THE BUG
  `subprocess.run(..., capture_output=True)` captures stdout and stderr but leaves
  stdin INHERITED. Under the MCP stdio transport, server.py's stdin is the pipe the
  client writes JSON-RPC into — so every git call was handed a live transport pipe
  as its standard input. Observed on Windows 2026-09-14: `git status --porcelain`
  never returned, the chain stalled before committing, and interrupting the parent
  released it (tearing down the pipe let git proceed), which is why the publish
  appeared to be "triggered by Ctrl-C".

THE FIX
  stdin=subprocess.DEVNULL on every git call. Nothing in this chain reads stdin, so
  this removes a capability rather than changing behaviour. It also makes git fail
  fast instead of waiting forever if a credential helper ever wants a prompt.

  GIT_TERMINAL_PROMPT=0 is set alongside it for the same reason, one layer up: a
  prompt that cannot be answered should be an error, not a hang. A gate that waits
  forever is indistinguishable from a gate that passed.

  Both helpers are patched — blog and corpus — because the corpus push runs under
  the same transport and would fail the same way, just later and more confusingly.
"""
import sys
from pathlib import Path

# These substrings are identical whether or not instrument_git.py is applied, so the
# fix can go in on top of the instrumentation and be verified in the same run.
BLOG_OLD = '''subprocess.run(["git", *args], cwd=blog_repo(), capture_output=True, text=True)'''
BLOG_NEW = '''subprocess.run(["git", *args], cwd=blog_repo(), capture_output=True, text=True,
                          stdin=subprocess.DEVNULL, env=_git_env())'''

CORPUS_OLD = '''subprocess.run(["git", *args], cwd=corpus_repo(), capture_output=True, text=True)'''
CORPUS_NEW = '''subprocess.run(["git", *args], cwd=corpus_repo(), capture_output=True, text=True,
                              stdin=subprocess.DEVNULL, env=_git_env())'''

HELPER_OLD = '''def _git(*args):'''
HELPER_NEW = '''def _git_env() -> dict:
    """The environment for a git subprocess, with prompting disabled.

    GIT_TERMINAL_PROMPT=0 turns "ask the user" into an immediate error. Under the MCP
    transport there is no user to ask and no terminal to ask on, so a prompt is a hang
    with extra steps. Explicit failure beats silent waiting: the caller can report a
    credential problem, it cannot report a wait.
    """
    env = dict(os.environ)
    env["GIT_TERMINAL_PROMPT"] = "0"
    return env


def _git(*args):'''

PAIRS = [(HELPER_OLD, HELPER_NEW), (BLOG_OLD, BLOG_NEW), (CORPUS_OLD, CORPUS_NEW)]


def main() -> int:
    undo = "--undo" in sys.argv
    p = Path("server.py")
    if not p.is_file():
        print("server.py not found — run this from the rnv-publishing-agent repo root")
        return 1

    s = p.read_text(encoding="utf-8")
    out = s
    for old, new in PAIRS:
        frm, to = (new, old) if undo else (old, new)
        n = out.count(frm)
        if n == 0 and out.count(to) == 1:
            continue
        assert n == 1, f"expected exactly one match, found {n} — server.py has moved, re-fetch"
        out = out.replace(frm, to)

    if out == s:
        print("nothing to do (already in that state)")
        return 0

    p.write_text(out, encoding="utf-8")
    print(f"  ok    server.py {'reverted' if undo else 'patched'}")
    print(f"  check stdin=DEVNULL on {out.count('stdin=subprocess.DEVNULL')} git call site(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
