#!/usr/bin/env python3
"""A self-contained run of the real publish chain, against throwaway repos.

Nothing here is stubbed. It creates two git repositories and a bare remote in a
temp directory, serves them over a real local HTTP server, and then calls the same
tools the agent calls in production. The git pushes are real pushes; the liveness
poll is a real HTTP request. Only the destination is disposable.

    python demo/run_demo.py            # every act
    python demo/run_demo.py --keep     # leave the temp dir in place to inspect

Requires no API key, no credentials, and no network access.
"""
from __future__ import annotations

import argparse
import contextlib
import functools
import http.server
import json
import os
import shutil
import socket
import socketserver
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

BOLD, DIM, GREEN, RED, YELLOW, RESET = "\033[1m", "\033[2m", "\033[32m", "\033[31m", "\033[33m", "\033[0m"
if not sys.stdout.isatty() or os.environ.get("NO_COLOR"):
    BOLD = DIM = GREEN = RED = YELLOW = RESET = ""

# The real chain waits three minutes for a Pages deploy and ninety seconds for the
# share image. Against a local server both are instant, so the demo passes short
# budgets through rather than sitting on a poll it has already satisfied.
FAST = {"timeout": 20, "interval": 1, "og_timeout": 6, "sitemap_timeout": 6}

POST = """<!doctype html>
<html lang="en">
<head>
<meta property="og:url" content="{site}/blog/{slug}/">
<meta property="og:title" content="{title}">
<meta property="og:description" content="{title}">
<meta property="og:image" content="{site}/assets/og/{slug}.png">
<meta property="article:published_time" content="2026-08-19T09:00:00Z">
<meta property="article:author" content="Christian Smith">
<meta name="card:summary" content="{title}">
</head>
<body><article><p>Body copy.</p></article></body>
</html>
"""

SITEMAP = '<?xml version="1.0" encoding="UTF-8"?><urlset>{urls}</urlset>'


def rebuild_sitemap(blog, site, *slugs):
    """Stand in for the build-feed Action regenerating the sitemap after a push."""
    urls = "".join(f"<url><loc>{site}/blog/{s}/</loc></url>" for s in slugs)
    (blog / "sitemap.xml").write_text(SITEMAP.format(urls=urls), encoding="utf-8")


# No <article> block, no article:published_time — two required fields absent.
DRAFT = """<!doctype html>
<html lang="en">
<head>
<meta property="og:url" content="{site}/blog/{slug}/">
<meta property="og:title" content="{title}">
</head>
<body><p>Still a draft.</p></body>
</html>
"""


def sh(cmd, cwd):
    subprocess.run(cmd, cwd=cwd, check=True, capture_output=True, text=True)


def init_repo(path: Path):
    path.mkdir(parents=True, exist_ok=True)
    sh(["git", "init", "-q", "-b", "main"], path)
    sh(["git", "config", "user.email", "demo@example.invalid"], path)
    sh(["git", "config", "user.name", "Publish Demo"], path)
    sh(["git", "config", "commit.gpgsign", "false"], path)


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def serve(directory: Path, port: int):
    handler = functools.partial(QuietHandler, directory=str(directory))
    httpd = socketserver.TCPServer(("127.0.0.1", port), handler)
    httpd.allow_reuse_address = True
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd


class QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *a):
        pass


def banner(n, title, claim):
    print(f"\n{BOLD}── Act {n}: {title} {RESET}{DIM}— {claim}{RESET}")


def show(label, result):
    print(f"{DIM}  {label}{RESET}")
    print("  " + json.dumps(result, indent=2).replace("\n", "\n  "))


def git_log(repo: Path) -> list[str]:
    out = subprocess.run(["git", "log", "--format=%s"], cwd=repo,
                         capture_output=True, text=True, check=True)
    return [l for l in out.stdout.splitlines() if l.strip()]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--keep", action="store_true", help="leave the temp workspace in place")
    args = ap.parse_args()

    work = Path(tempfile.mkdtemp(prefix="rnv-publish-demo-"))
    blog, remote = work / "blog", work / "blog-remote.git"
    corpus, corpus_remote = work / "corpus", work / "corpus-remote.git"

    for bare in (remote, corpus_remote):
        bare.mkdir(parents=True)
        sh(["git", "init", "-q", "--bare", "-b", "main"], bare)

    init_repo(blog)
    (blog / "blog").mkdir()
    (blog / "assets" / "og").mkdir(parents=True)
    (blog / "README.md").write_text("demo blog\n", encoding="utf-8")
    # Wave two: build-feed regenerates this after the push, so at first it lists only
    # what was already published.
    (blog / "sitemap.xml").write_text(SITEMAP.format(urls=""), encoding="utf-8")
    sh(["git", "add", "-A"], blog)
    sh(["git", "commit", "-qm", "init"], blog)
    sh(["git", "remote", "add", "origin", str(remote)], blog)
    sh(["git", "push", "-q", "-u", "origin", "main"], blog)

    init_repo(corpus)
    (corpus / "sources.json").write_text(json.dumps({"sources": []}, indent=2) + "\n", encoding="utf-8")
    sh(["git", "add", "-A"], corpus)
    sh(["git", "commit", "-qm", "init"], corpus)
    sh(["git", "remote", "add", "origin", str(corpus_remote)], corpus)
    sh(["git", "push", "-q", "-u", "origin", "main"], corpus)

    port = free_port()
    site = f"http://127.0.0.1:{port}"
    httpd = serve(blog, port)

    os.environ["BLOG_REPO"] = str(blog)
    os.environ["CORPUS_REPO"] = str(corpus)
    os.environ["SITE_URL"] = site

    import server  # imported after the env is set; config resolves per call

    (blog / "blog" / "half-written").mkdir(parents=True)
    (blog / "blog" / "half-written" / "index.html").write_text(
        DRAFT.format(site=site, slug="half-written", title="Half Written"), encoding="utf-8")
    (blog / "blog" / "the-honest-machine").mkdir(parents=True)
    (blog / "blog" / "the-honest-machine" / "index.html").write_text(
        POST.format(site=site, slug="the-honest-machine", title="The Honest Machine"), encoding="utf-8")

    print(f"{BOLD}rnv-publishing — live chain against a throwaway site{RESET}")
    print(f"{DIM}workspace {work}\nserving   {site}{RESET}")

    # ---- Act 1 ---------------------------------------------------------------
    banner(1, "It refuses", "a post that is not ready does not get published")
    before = git_log(blog)
    r1 = server.publish_post("half-written", for_real=True, **FAST)
    show("publish_post('half-written', for_real=True)", r1)
    ok1 = (r1["ok"] is False and r1["stopped_at"] == "validate_post"
           and [s["step"] for s in r1["trace"]] == ["validate_post"]
           and git_log(blog) == before)
    print(f"  {GREEN if ok1 else RED}{'PASS' if ok1 else 'FAIL'}{RESET} "
          f"stopped at validation; nothing committed")

    # ---- Act 2 ---------------------------------------------------------------
    banner(2, "The safe default", "for_real defaults to False and writes nothing")
    before = git_log(blog)
    r2 = server.publish_post("the-honest-machine")
    show("publish_post('the-honest-machine')   # no for_real argument", r2)
    ok2 = r2.get("dry_run") is True and git_log(blog) == before
    print(f"  {GREEN if ok2 else RED}{'PASS' if ok2 else 'FAIL'}{RESET} "
          f"validated only; git log unchanged ({len(before)} commits before and after)")

    # ---- Act 3 ---------------------------------------------------------------
    banner(3, "The real thing", "commit, push, poll until live, ingest — and one honest warning")
    print(f"{DIM}  Wave two has not run: build-feed has not regenerated the sitemap, and the\n"
          f"  share image job has not rendered the card. Neither is this post's defect,\n"
          f"  so the chain should publish anyway and say so loudly about both.{RESET}")
    r3 = server.publish_post("the-honest-machine", for_real=True, **FAST)
    show("publish_post('the-honest-machine', for_real=True)", r3)

    steps = [s["step"] for s in r3["trace"]]
    pushed = "Publish: the-honest-machine" in subprocess.run(
        ["git", "log", "--format=%s", "main"], cwd=remote,
        capture_output=True, text=True, check=True).stdout
    sources = json.loads((corpus / "sources.json").read_text(encoding="utf-8"))["sources"]
    warned = bool(r3.get("warnings"))
    ok3 = (r3["ok"] is True and steps == ["validate_post", "commit_and_push",
                                          "wait_for_live", "update_corpus"]
           and pushed and any(s["id"] == "the-honest-machine" for s in sources) and warned)
    print(f"  {GREEN if ok3 else RED}{'PASS' if ok3 else 'FAIL'}{RESET} "
          f"four steps ran, commit landed in the remote, corpus updated")
    if warned:
        for w in r3["warnings"]:
            print(f"  {YELLOW}warning surfaced:{RESET} {w}")

    # ---- Act 3b --------------------------------------------------------------
    banner("3b", "Wave two catches up", "the same run, when the second-wave jobs finish in time")
    og = blog / "assets" / "og" / "late-arrival.png"

    def wave_two_late():
        """Both second-wave jobs finishing a beat after the push, as they really do."""
        time.sleep(2)
        og.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 64)
        rebuild_sitemap(blog, site, "the-honest-machine", "late-arrival")

    (blog / "blog" / "late-arrival").mkdir(parents=True)
    (blog / "blog" / "late-arrival" / "index.html").write_text(
        POST.format(site=site, slug="late-arrival", title="Late Arrival"), encoding="utf-8")
    threading.Thread(target=wave_two_late, daemon=True).start()
    print(f"{DIM}  A background job rebuilds the sitemap and writes the share image two\n"
          f"  seconds from now, the way the second-wave Actions land after a push.{RESET}")
    r3b = server.publish_post("late-arrival", for_real=True, **FAST)
    live_step = next(s for s in r3b["trace"] if s["step"] == "wait_for_live")
    ok3b = (r3b["ok"] is True and live_step.get("og_image_live") is True
            and live_step.get("sitemap_listed") is True and not r3b.get("warnings"))
    print(f"  {GREEN if ok3b else RED}{'PASS' if ok3b else 'FAIL'}{RESET} "
          f"polled until the sitemap listed it and the image appeared; no warning")

    # ---- Act 4 ---------------------------------------------------------------
    banner(4, "Idempotent", "running it again does not double-publish")
    r4 = server.publish_post("the-honest-machine", for_real=True, **FAST)
    ok4 = r4["ok"] is True and any(
        s["step"] == "commit_and_push" and s.get("committed") is False for s in r4["trace"])
    print(f"  {GREEN if ok4 else RED}{'PASS' if ok4 else 'FAIL'}{RESET} "
          f"nothing to commit; corpus already has the source")

    httpd.shutdown()
    passed = all([ok1, ok2, ok3, ok3b, ok4])
    print(f"\n{BOLD}{'Every act passed.' if passed else 'Something failed above.'}{RESET}")
    if args.keep:
        print(f"{DIM}workspace kept at {work}{RESET}")
    else:
        shutil.rmtree(work, ignore_errors=True)
    return 0 if passed else 1


if __name__ == "__main__":
    with contextlib.suppress(KeyboardInterrupt):
        sys.exit(main())
    sys.exit(130)
