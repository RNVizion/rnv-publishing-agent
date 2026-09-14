#!/usr/bin/env python3
"""
preflight.py — prove the machine is ready to publish, before anything is published.

WHY THIS EXISTS
  The publish chain reads BLOG_REPO, CORPUS_REPO and SITE_URL, and falls back to
  Codespace paths when they are unset. On any other machine those paths do not
  exist, and the failure surfaces late and wearing the wrong face: an unset
  BLOG_REPO makes validate_post report "no index.html for <slug>" about a post
  that is sitting right there on disk. The tool blamed the post for a defect in
  the configuration.

  That is a wrong assertion, and a wrong assertion is worse than an absent one
  because it is trusted. This script exists so the diagnosis happens once, up
  front, naming the real cause.

WHAT IT CHECKS
  interpreter -> dependencies -> environment -> git -> post -> agent route
  Each check reports what it found, not merely pass/fail, so a failure is
  actionable without a second round trip.

DELIBERATELY STDLIB-ONLY. It must run BEFORE `pip install -r requirements.txt`,
so it can tell you that is what you still need to do. Importing mcp here would
make the dependency check impossible to fail usefully.

USAGE (from the agent repo root)
  python tools/preflight.py                      # environment only
  python tools/preflight.py --slug my-post       # also check a specific post
  python tools/preflight.py --slug my-post --push-check   # also verify push access

EXIT CODES
  0  ready
  1  something is wrong; every failure prints what to do about it
"""

from __future__ import annotations

import argparse
import importlib.util
import os
import re
import subprocess
import sys
from pathlib import Path

# Mirror server.py's fallbacks. If these drift, preflight lies about what the
# chain would actually do, so they are asserted against server.py below.
FALLBACKS = {
    "BLOG_REPO": "/workspaces/rnvizion.github.io",
    "CORPUS_REPO": "/workspaces/rnv-ask-the-corpus",
    "SITE_URL": "https://rnvizion.dev",
}

OK, WARN, BAD = "  ok  ", " warn ", " FAIL "
problems: list[str] = []
warnings: list[str] = []


def line(status: str, label: str, detail: str = "") -> None:
    print(f"[{status}] {label}" + (f"  {detail}" if detail else ""))


def fail(label: str, detail: str, fix: str) -> None:
    line(BAD, label, detail)
    print(f"         fix: {fix}")
    problems.append(label)


def warn(label: str, detail: str, fix: str = "") -> None:
    line(WARN, label, detail)
    if fix:
        print(f"         note: {fix}")
    warnings.append(label)


def run(cmd: list[str], cwd: Path | None = None) -> tuple[int, str]:
    try:
        p = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=60)
        return p.returncode, (p.stdout + p.stderr).strip()
    except FileNotFoundError:
        return 127, f"{cmd[0]} not found on PATH"
    except subprocess.TimeoutExpired:
        return 124, "timed out"


# --------------------------------------------------------------------------
def check_interpreter() -> None:
    print("\nInterpreter")
    v = sys.version_info
    if (v.major, v.minor) in {(3, 11), (3, 12)}:
        line(OK, f"Python {v.major}.{v.minor}.{v.micro}")
    elif v >= (3, 11):
        warn(f"Python {v.major}.{v.minor}.{v.micro}",
             "CI tests 3.11 and 3.12 only", "probably fine; untested here")
    else:
        fail(f"Python {v.major}.{v.minor}.{v.micro}", "too old",
             "install Python 3.11 or 3.12")

    in_venv = sys.prefix != getattr(sys, "base_prefix", sys.prefix)
    if in_venv:
        line(OK, "virtualenv active", Path(sys.prefix).name)
    else:
        warn("virtualenv not active", "installing into the system interpreter",
             "python -m venv .venv && source .venv/bin/activate"
             "   (Windows/Git Bash: source .venv/Scripts/activate)")


def check_dependencies() -> None:
    print("\nDependencies")
    if importlib.util.find_spec("mcp") is None:
        fail("mcp not importable", "the server cannot start without it",
             "pip install -r requirements.txt")
    else:
        line(OK, "mcp importable")
    if importlib.util.find_spec("anthropic") is None:
        warn("anthropic not importable", "agent.py unavailable",
             "only needed for the conversational route; the direct route is fine")
    else:
        line(OK, "anthropic importable", "(only needed by agent.py)")


def check_fallbacks_still_match(repo_root: Path) -> None:
    """If server.py's fallbacks drift from this file, preflight starts lying."""
    server = repo_root / "server.py"
    if not server.is_file():
        fail("server.py not found", f"looked in {repo_root}",
             "run this from the agent repo root, not the site checkout")
        return
    text = server.read_text(encoding="utf-8", errors="replace")
    drifted = [k for k, v in FALLBACKS.items() if v not in text]
    if drifted:
        warn("fallback values may have drifted from server.py",
             ", ".join(drifted),
             "update FALLBACKS in tools/preflight.py so this check stays truthful")
    else:
        line(OK, "server.py found; fallback values match")


def check_environment() -> dict[str, Path | None]:
    print("\nEnvironment")
    resolved: dict[str, Path | None] = {}

    for key in ("BLOG_REPO", "CORPUS_REPO"):
        raw = os.environ.get(key)
        using_fallback = raw is None
        value = raw or FALLBACKS[key]
        path = Path(value)

        if path.is_dir():
            if using_fallback:
                warn(key, f"unset; using fallback {value} (exists)",
                     "fine in a Codespace; set it explicitly elsewhere")
            else:
                line(OK, key, f"= {value}")
            resolved[key] = path
        else:
            resolved[key] = None
            if using_fallback:
                fail(key,
                     f"unset, so it falls back to {value}, which does not exist",
                     f'export {key}="C:/Users/<you>/rnv/{Path(value).name}"   '
                     "(Windows: use C:/ form, not /c/)")
            else:
                fail(key, f"= {value}  but that directory does not exist",
                     "check the path; on Windows use C:/Users/... not /c/Users/...")

    raw_url = os.environ.get("SITE_URL")
    url = raw_url or FALLBACKS["SITE_URL"]
    if not re.match(r"^https?://[^\s/]+\.[^\s/]+", url):
        fail("SITE_URL", f"= {url!r} does not look like a full origin",
             'export SITE_URL="https://rnvizion.dev"')
    elif url.endswith("/"):
        warn("SITE_URL", f"= {url} has a trailing slash",
             "wait_for_live joins paths; a trailing slash can double it")
    else:
        line(OK, "SITE_URL", f"= {url}" + ("  (fallback)" if raw_url is None else ""))

    return resolved


def check_git(paths: dict[str, Path | None], push_check: bool) -> None:
    print("\nGit")
    code, out = run(["git", "--version"])
    if code != 0:
        fail("git", out, "install git and make sure it is on PATH")
        return
    line(OK, "git available", out.splitlines()[0] if out else "")

    blog = paths.get("BLOG_REPO")
    if blog is None:
        line(WARN, "skipping repo checks", "BLOG_REPO unresolved")
        warnings.append("git repo checks skipped")
        return

    if not (blog / ".git").exists():
        fail("BLOG_REPO is not a git repository", str(blog),
             "point BLOG_REPO at the cloned site repo, not a plain folder")
        return
    line(OK, "BLOG_REPO is a git repository")

    if not (blog / "blog").is_dir():
        fail("no blog/ directory inside BLOG_REPO", str(blog / "blog"),
             "BLOG_REPO should be the site repo root, which contains blog/")
    else:
        n = len(list((blog / "blog").glob("*/index.html")))
        line(OK, "blog/ present", f"{n} post folder(s)")

    code, out = run(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=blog)
    if code == 0:
        branch = out.strip()
        if branch == "main":
            line(OK, "on branch main")
        else:
            warn("not on main", f"on {branch}",
                 "commit_and_push pushes the current branch")

    code, out = run(["git", "status", "--porcelain"], cwd=blog)
    if code == 0 and out:
        warn("working tree has uncommitted changes",
             f"{len(out.splitlines())} file(s)",
             "commit_and_push stages only the post, but know what else is dirty")
    elif code == 0:
        line(OK, "working tree clean")

    if push_check:
        code, out = run(["git", "push", "--dry-run"], cwd=blog)
        if code == 0:
            line(OK, "push access confirmed", "(dry run)")
        else:
            fail("cannot push to the site repo", out.splitlines()[-1] if out else "",
                 "configure credentials; a Codespace grants this natively, a laptop does not")


def check_post(paths: dict[str, Path | None], slug: str) -> None:
    print(f"\nPost: {slug}")
    blog = paths.get("BLOG_REPO")
    if blog is None:
        fail("cannot check the post", "BLOG_REPO unresolved",
             "fix BLOG_REPO first — otherwise a missing post and a missing "
             "repo look identical, which is the bug this script exists to prevent")
        return

    post = blog / "blog" / slug / "index.html"
    if not post.is_file():
        siblings = sorted(p.parent.name for p in (blog / "blog").glob("*/index.html"))
        fail("post file not found", str(post),
             "check the slug. Posts present: " + (", ".join(siblings) or "none"))
        return
    line(OK, "post file exists", str(post))

    html = post.read_text(encoding="utf-8", errors="replace")
    stripped = re.sub(r"<!--.*?-->", "", html, flags=re.S)
    checks = {
        "<article> block": bool(re.search(r"<article[\s>]", stripped)),
        "article:published_time": bool(re.search(r"article:published_time", stripped)),
        "og:url": bool(re.search(r"og:url", stripped)),
    }
    for label, present in checks.items():
        if present:
            line(OK, f"{label} present")
        else:
            fail(f"{label} missing", "validate_post would halt here",
                 "add it to the post's <head> (or body, for <article>)")

    if re.search(r"\[[A-Z][^\]]*\]", stripped):
        found = set(re.findall(r"\[[A-Z][^\]]*\]", stripped))
        warn("unfilled template placeholders", ", ".join(sorted(found)[:4]),
             "these would publish verbatim")


def check_agent_route() -> None:
    print("\nAgent route (optional)")
    if os.environ.get("ANTHROPIC_API_KEY"):
        line(OK, "ANTHROPIC_API_KEY set", "agent.py available")
    else:
        warn("ANTHROPIC_API_KEY unset", "agent.py unavailable",
             "not required: the direct route needs no key")


# --------------------------------------------------------------------------
def main() -> int:
    ap = argparse.ArgumentParser(description="Preflight for the RNV publishing agent.")
    ap.add_argument("--slug", help="also check this post is publishable")
    ap.add_argument("--push-check", action="store_true",
                    help="verify push access with git push --dry-run (needs network)")
    args = ap.parse_args()

    repo_root = Path(__file__).resolve().parent.parent
    print("RNV publishing agent — preflight")
    print(f"agent repo: {repo_root}")

    check_interpreter()
    check_dependencies()
    check_fallbacks_still_match(repo_root)
    paths = check_environment()
    check_git(paths, args.push_check)
    if args.slug:
        check_post(paths, args.slug)
    check_agent_route()

    print("\n" + "-" * 60)
    if problems:
        print(f"NOT READY — {len(problems)} problem(s): {', '.join(problems)}")
        print("Fix the FAIL lines above, then run this again.")
        return 1
    if warnings:
        print(f"READY — with {len(warnings)} warning(s): {', '.join(warnings)}")
    else:
        print("READY — every check passed.")
    if args.slug:
        print(f"\nRehearse:  python -c \"import server, json; "
              f"print(json.dumps(server.publish_post('{args.slug}'), indent=2))\"")
        print(f"Publish :  python -c \"import server, json; "
              f"print(json.dumps(server.publish_post('{args.slug}', for_real=True), indent=2))\"")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
