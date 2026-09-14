#!/usr/bin/env python3
"""Temporary instrumentation for server.py — timestamps every stage to a FILE.

Run from the rnv-publishing-agent repo root:   python instrument_git.py
Undo when finished:                            python instrument_git.py --undo

Writes to publish-debug.log beside server.py. A file, not stderr: the MCP client
captures the child's stderr, so anything printed there is invisible. A file is
unconditional — it does not depend on how the transport treats the child's streams.

Watch it live from a second window:
    tail -f C:/Users/vizio/rnv/rnv-publishing-agent/publish-debug.log

The first line it writes happens at IMPORT, before any tool runs. If the file
never appears at all, the child process is not starting; if it appears but stops
after "import", the process starts and the tool is never called. Those are two
different bugs and this is the cheapest way to tell them apart.
"""
import sys
from pathlib import Path

HELPER_OLD = '''mcp = FastMCP("rnv-publishing")'''

HELPER_NEW = '''mcp = FastMCP("rnv-publishing")

# --- INSTRUMENT (temporary; remove with instrument_git.py --undo) -------------
_DBG_PATH = Path(__file__).resolve().parent / "publish-debug.log"


def _dbg(msg: str) -> None:
    """Append one timestamped line. Swallows its own errors on purpose: a broken
    debug log must never be the reason a publish fails."""
    try:
        with open(_DBG_PATH, "a", encoding="utf-8") as fh:
            fh.write(f"{time.strftime('%H:%M:%S')}  {msg}\\n")
    except Exception:
        pass


_dbg(f"=== server.py imported (pid {os.getpid()}) ===")
# --- end INSTRUMENT -----------------------------------------------------------'''

GIT_OLD = '''def _git(*args):
    """Run a git command inside the blog repo; returns the CompletedProcess."""
    return subprocess.run(["git", *args], cwd=blog_repo(), capture_output=True, text=True)'''

GIT_NEW = '''def _git(*args):
    """Run a git command inside the blog repo; returns the CompletedProcess."""
    _t0 = time.time()  # INSTRUMENT
    _dbg(f"git -> {' '.join(args)}")  # INSTRUMENT
    _r = subprocess.run(["git", *args], cwd=blog_repo(), capture_output=True, text=True)
    _dbg(f"git <- {' '.join(args)}  rc={_r.returncode}  {time.time()-_t0:.1f}s"
         f"{'  stderr=' + _r.stderr.strip()[:200] if _r.returncode else ''}")  # INSTRUMENT
    return _r'''

ENTRY_OLD = '''    trace = []
    warnings = []

    cfg = config_report(for_real=for_real)'''

ENTRY_NEW = '''    _dbg(f"publish_post ENTERED slug={slug} for_real={for_real}")  # INSTRUMENT
    trace = []
    warnings = []

    cfg = config_report(for_real=for_real)'''

VALID_OLD = '''    v = step("validate_post", validate_post(slug))'''

VALID_NEW = '''    _dbg("about to validate_post")  # INSTRUMENT
    v = step("validate_post", validate_post(slug))'''

PUSH_OLD = '''    cp = step("commit_and_push", commit_and_push(slug))'''

PUSH_NEW = '''    _dbg("about to commit_and_push")  # INSTRUMENT
    cp = step("commit_and_push", commit_and_push(slug))'''

LIVE_OLD = '''    live = step("wait_for_live", wait_for_live(slug, timeout=timeout, interval=interval,'''

LIVE_NEW = '''    _dbg("about to wait_for_live (polls; up to ~5.5 min of silence is NORMAL)")  # INSTRUMENT
    live = step("wait_for_live", wait_for_live(slug, timeout=timeout, interval=interval,'''

CORPUS_OLD = '''    uc = step("update_corpus", update_corpus(slug))'''

CORPUS_NEW = '''    _dbg("about to update_corpus")  # INSTRUMENT
    uc = step("update_corpus", update_corpus(slug))'''

PAIRS = [
    (HELPER_OLD, HELPER_NEW),
    (GIT_OLD, GIT_NEW),
    (ENTRY_OLD, ENTRY_NEW),
    (VALID_OLD, VALID_NEW),
    (PUSH_OLD, PUSH_NEW),
    (LIVE_OLD, LIVE_NEW),
    (CORPUS_OLD, CORPUS_NEW),
]


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
    print(f"  ok    server.py {'reverted' if undo else 'instrumented'}")
    print(f"  check {out.count('INSTRUMENT')} instrument markers present")
    if not undo:
        print("  log   publish-debug.log  (tail -f it from a second window)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
