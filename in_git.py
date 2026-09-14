#!/usr/bin/env python3
"""Temporary instrumentation for server.py — times every git call to stderr.

Run from the rnv-publishing-agent repo root:   python instrument_git.py
Undo when finished:                            python instrument_git.py --undo

Why stderr: the MCP client passes the child's stderr straight through to your
terminal, so these lines appear live while the agent sits there looking frozen.
That is the whole point — we need to see what it is doing DURING the silence,
not reconstruct it from git afterwards.
"""
import sys
from pathlib import Path

OLD = '''def _git(*args):
    """Run a git command inside the blog repo; returns the CompletedProcess."""
    return subprocess.run(["git", *args], cwd=blog_repo(), capture_output=True, text=True)'''

NEW = '''def _git(*args):
    """Run a git command inside the blog repo; returns the CompletedProcess."""
    _t0 = time.time()  # INSTRUMENT
    print(f"[git] -> {' '.join(args)}", file=sys.stderr, flush=True)  # INSTRUMENT
    _r = subprocess.run(["git", *args], cwd=blog_repo(), capture_output=True, text=True)
    print(f"[git] <- {' '.join(args)}  rc={_r.returncode}  {time.time()-_t0:.1f}s", file=sys.stderr, flush=True)  # INSTRUMENT
    return _r'''

ENTRY_OLD = '''    trace = []
    warnings = []

    cfg = config_report(for_real=for_real)'''

ENTRY_NEW = '''    print(f"[pub] publish_post entered slug={slug} for_real={for_real}", file=sys.stderr, flush=True)  # INSTRUMENT
    trace = []
    warnings = []

    cfg = config_report(for_real=for_real)'''

LIVE_OLD = '''    live = step("wait_for_live", wait_for_live(slug, timeout=timeout, interval=interval,'''

LIVE_NEW = '''    print("[pub] entering wait_for_live (this polls; up to ~5.5 min of silence)", file=sys.stderr, flush=True)  # INSTRUMENT
    live = step("wait_for_live", wait_for_live(slug, timeout=timeout, interval=interval,'''

PAIRS = [(OLD, NEW), (ENTRY_OLD, ENTRY_NEW), (LIVE_OLD, LIVE_NEW)]


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
            print(f"  skip  already {'reverted' if undo else 'applied'}")
            continue
        assert n == 1, f"expected exactly one match, found {n} — server.py has moved, re-fetch"
        out = out.replace(frm, to)

    if out == s:
        print("nothing to do")
        return 0

    p.write_text(out, encoding="utf-8")
    print(f"  ok    server.py {'reverted' if undo else 'instrumented'}")
    print(f"  check {out.count('# INSTRUMENT')} INSTRUMENT markers present")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
