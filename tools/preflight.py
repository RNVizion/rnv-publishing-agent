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

USAGE (from anywhere)
  python tools/preflight.py                      # environment only
  python tools/preflight.py --slug my-post       # also check a specific post
  python tools/preflight.py --slug my-post --push-check   # also verify push access

  The working directory does not matter. Every path is resolved from this
  file's own location, the same derivation rnv_config uses, so the script can be
  wired into a job that cannot change directory. This block said "from the agent
  repo root" until 2026-09-22, having survived the commit that removed the last
  cwd dependency — a constraint a reader obeys is never contradicted by the code,
  so it costs nothing until someone reads it and concludes the tool is
  unavailable where they need it.

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

# Resolution comes from the module server.py uses, so the two cannot disagree.
# An earlier draft kept a copy of the defaults here plus a guard warning when the
# two drifted; a guard against drift is a confession that there are two
# definitions. rnv_config is stdlib-only, so importing it does not cost this
# script its run-before-pip-install property.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import rnv_config  # noqa: E402

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
    # Reports the package and nothing about whether agent.py can run. That
    # verdict needs the key too, and it belongs to check_agent_route — see the
    # note there. Until 2026-09-22 this line asserted it from half the condition.
    if importlib.util.find_spec("anthropic") is None:
        warn("anthropic not importable", "needed by agent.py",
             "only the conversational route needs it; the direct route is fine")
    else:
        line(OK, "anthropic importable", "(needed by agent.py)")


def check_agent_repo(repo_root: Path) -> None:
    server = repo_root / "server.py"
    if not server.is_file():
        # repo_root comes from __file__, so no working directory can produce
        # this. The only way here is a copy of preflight.py living outside the
        # repo, and changing directory does not fix that.
        fail("server.py not found", f"looked in {repo_root}",
             "preflight.py resolves the repo from its own location; run the copy "
             "inside the agent repo's tools/, not one moved elsewhere")
    else:
        line(OK, "agent repo located", str(repo_root))


def check_environment() -> dict[str, Path | None]:
    """Report what the chain will actually resolve, and from which rung.

    Resolution is environment -> .env -> sibling checkout. The sibling rung means
    a machine with the repos cloned next to each other needs no configuration, so
    an unset variable is not itself a problem; a path that does not exist is.
    """
    print("\nEnvironment")
    resolved: dict[str, Path | None] = {}

    for key in ("BLOG_REPO", "CORPUS_REPO"):
        path, how = rnv_config.resolve_path(key)
        if path.is_dir():
            line(OK, key, f"= {path}   [{how}]")
            resolved[key] = path
        else:
            resolved[key] = None
            fail(key, f"no directory at {path}   [{how}]",
                 f'clone {rnv_config.SIBLING[key]} beside the agent repo, '
                 f'or set {key} explicitly (Windows: C:/ form, not /c/)')

    url, how = rnv_config.resolve_site_url()
    if not re.match(r"^https?://[^\s/]+\.[^\s/]+", url):
        fail("SITE_URL", f"= {url!r} is not a usable origin   [{how}]",
             'export SITE_URL="https://rnvizion.dev"')
    else:
        line(OK, "SITE_URL", f"= {url}   [{how}]")

    if rnv_config.DOTENV_PATH.is_file():
        line(OK, ".env present", str(rnv_config.DOTENV_PATH))

    return resolved


# A ref that does not exist on the remote cannot diverge, so nothing about the
# remote's position can make a push to it fail; creating a branch is a real write,
# so a remote that refuses writes still says no. --dry-run leaves nothing behind,
# and a test asserts the ref is never created.
PROBE_REF = "refs/heads/__rnv_preflight_probe__"


def check_pushable(var: str, repo: Path, push_check: bool) -> None:
    """Can the chain fetch from and push to this repo? Asked of BOTH of them.

    Until 2026-09-22 only BLOG_REPO was asked anything git-shaped; CORPUS_REPO got
    resolution and nothing else, so preflight printed READY — and the for_real
    command under it — for a corpus the registration could not write to. That
    failure lands at stage 5, with the post already live.

    The local half mirrors server.py's _git_problem and reports git's own words
    rather than translating them; a caption that names a cause can name the wrong
    one, which this repo has now fixed twice. The network half runs only under
    --push-check, which is the whole reason that flag exists.

    ASK THE QUESTION YOU MEAN. Until 2026-09-22 the site half ran a bare
    `git push --dry-run`, which pushes the current branch to its upstream and so
    fails whenever the remote has moved. The site's own Actions push to main on
    every publish, making "the remote moved" this pipeline's normal steady state
    rather than an edge case — and commit_and_push handles it by design. The old
    check reported NOT READY for a publish that would have succeeded, and
    captioned it "configure credentials".
    """
    code, out = run(["git", "remote", "get-url", "origin"], cwd=repo)
    if code != 0:
        first = (out.splitlines() or [""])[0]
        fail(f"{var} is not a git repository with an 'origin'", first,
             f"the chain fetches from and pushes to it. Point {var} at the "
             f"clone itself, not at a copy of its contents")
        return
    line(OK, f"{var} is a git repository", f"origin -> {out.strip()}")

    if not push_check:
        return
    code, out = run(["git", "push", "--dry-run", "origin", f"HEAD:{PROBE_REF}"],
                    cwd=repo)
    if code == 0:
        line(OK, f"{var} write access confirmed", "(dry run; no ref created)")
    else:
        # git's own words, first line first: its last line is usually a `hint:`
        # pointing at documentation rather than the error.
        said = [l for l in out.splitlines() if l.strip()]
        fail(f"cannot write to {var}", said[0] if said else "",
             "git's message is above. Credentials are the usual cause — a "
             "Codespace grants them natively, a laptop does not — but read "
             "it rather than assuming: this check no longer guesses")


def check_git(paths: dict[str, Path | None], push_check: bool) -> None:
    print("\nGit")
    code, out = run(["git", "--version"])
    if code != 0:
        fail("git", out, "install git and make sure it is on PATH")
        return
    line(OK, "git available", out.splitlines()[0] if out else "")

    # Each repo is reported on its own. Until 2026-09-22 an unresolved or non-git
    # BLOG_REPO returned from this whole function, so CORPUS_REPO was never
    # reached: one path's problem hid the other's and the operator fixed them one
    # run at a time. config_report reports every misconfiguration in a single call
    # for that reason; this now matches it.
    blog = paths.get("BLOG_REPO")
    if blog is None:
        line(WARN, "skipping site repo checks", "BLOG_REPO unresolved")
        warnings.append("site repo checks skipped")
    else:
        check_site_repo(blog, push_check)

    corpus = paths.get("CORPUS_REPO")
    if corpus is None:
        line(WARN, "skipping corpus repo checks", "CORPUS_REPO unresolved")
        warnings.append("corpus repo checks skipped")
    else:
        check_pushable("CORPUS_REPO", corpus, push_check)


def check_site_repo(blog: Path, push_check: bool) -> None:
    check_pushable("BLOG_REPO", blog, push_check)
    if not (blog / ".git").exists():
        return

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
            # Said "commit_and_push pushes the current branch" until
            # 2026-09-22. That was written on 09-13 and stopped being true on
            # 09-18, when the publish began building its commit on main and
            # pushing <sha>:refs/heads/main regardless of where you stand. The
            # branch you are on no longer affects WHAT is published; it affects
            # only whether your checkout gets fast-forwarded onto it afterwards.
            warn("not on main", f"on {branch}",
                 "the publish still goes to main — it builds its commit there. "
                 "Your checkout just will not be moved onto it afterwards")

    code, out = run(["git", "status", "--porcelain"], cwd=blog)
    if code == 0 and out:
        warn("working tree has uncommitted changes",
             f"{len(out.splitlines())} file(s)",
             "commit_and_push stages only the post, but know what else is dirty")
    elif code == 0:
        line(OK, "working tree clean")


def post_shape_refs(blog: Path, html: str) -> list[str] | None:
    """Ask the site project's own library what resolves beside the post.

    Returns the references, [] if none, or None when the library could not be
    used — which is a gap in this check, never a verdict about the post.

    IMPORTED, NOT REIMPLEMENTED — principle 18, and the same module validate_post
    loads, so the two cannot disagree about the rule. What is local here is the
    loading mechanism, not the rule; a path-import helper is not a second
    definition of what a sibling reference is. The library is stdlib-only, so
    this costs preflight nothing of its run-before-pip-install property.
    """
    path = blog / "scripts" / "post_shape.py"
    if not path.is_file():
        return None
    spec = importlib.util.spec_from_file_location("rnv_preflight_post_shape", path)
    if spec is None or spec.loader is None:
        return None
    module = importlib.util.module_from_spec(spec)
    # No .pyc in the operator's site checkout: a preflight is not entitled to
    # create files in somebody else's repo. Same reasoning as server.py's loader.
    wrote = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    try:
        spec.loader.exec_module(module)
        refs = module.sibling_refs(html)
    except BaseException:
        # BaseException, not Exception: a library that calls sys.exit() on import
        # raises SystemExit, which is not an Exception, and must not end a
        # diagnostic run. Same reasoning as server.py's loader.
        return None
    finally:
        sys.dont_write_bytecode = wrote
    return list(refs) if refs is not None else None


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

    # The post-shape rule, asked of the site project's own library. validate_post
    # GATES on this, so a post failing it does not publish; before 2026-09-22
    # preflight said READY about such a post and printed the publish command.
    refs = post_shape_refs(blog, html)
    if refs is None:
        warn("post-shape rule not checked",
             f"no usable {blog / 'scripts' / 'post_shape.py'}",
             "a site checkout predating 2026-09-20 has no library: "
             'git -C "$BLOG_REPO" pull. validate_post will still gate on it')
    elif refs:
        fail("references resolve beside the post", ", ".join(sorted(refs)[:4]),
             "validate_post halts here. A publish carries only index.html, so "
             "these would 404 — move them under /assets/ and link absolutely, "
             "or wrap markup meant to be read in <code>/<pre>")
    else:
        line(OK, "no references resolve beside the post")

    # WHAT THIS DID NOT CHECK. validate_post also gates on the site contract's
    # HTML-comment allowlist, which lives in server.py — unimportable here,
    # because it needs mcp and this script runs before pip install. Copying the
    # allowlist would be a second definition of somebody else's rule, so the
    # honest move is to name the gap rather than close it badly or stay silent
    # about it. Principle 10: when a tool cannot check something, it says so.
    print("         not checked here: the HTML-comment allowlist "
          "(validate_post gates on it; the dry run is the authority)")


def check_agent_route() -> None:
    """The one place that decides whether agent.py can run.

    agent.py needs BOTH the anthropic package and ANTHROPIC_API_KEY. Until
    2026-09-22 two checks each owned half of that condition and each asserted the
    whole verdict: check_dependencies said "agent.py unavailable" from the import
    alone, this said "agent.py available" from the key alone. With the package
    missing and the key set, one run printed both — and the wrong one printed
    last, so it is the one a reader carries away.

    The failure direction was the bad one. A missing key with the package present
    warned, which is conservative. A present key with the package missing read
    `ok`, a false all-clear about the one route this section exists to assess.

    So the verdict has one owner, it reads both inputs, and it names which one
    failed. Both inputs are read here rather than handed in: the duplicated read
    is of a fact, not of a judgement, and a signature that threads it through
    would make the two functions agree by construction at the cost of letting a
    caller supply the answer. §3.2.4, in the file whose own docstring is about a
    tool reporting a defect in the wrong place.
    """
    print("\nAgent route (optional)")
    have_package = importlib.util.find_spec("anthropic") is not None
    have_key = bool(os.environ.get("ANTHROPIC_API_KEY"))

    if have_package and have_key:
        line(OK, "agent.py available", "anthropic importable, ANTHROPIC_API_KEY set")
        return

    missing = []
    if not have_package:
        missing.append("anthropic not importable")
    if not have_key:
        missing.append("ANTHROPIC_API_KEY unset")
    warn("agent.py unavailable", "; ".join(missing),
         "not required: the direct route needs neither")


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
    check_agent_repo(repo_root)
    paths = check_environment()
    check_git(paths, args.push_check)
    if args.slug:
        check_post(paths, args.slug)
    check_agent_route()

    print("\n" + "-" * 60)
    # Whichever verdict follows, a run that looked at a post says who the
    # authority on that post is. This sat inside the READY branch for the first
    # hour of its life, so a run that went NOT READY for an unrelated reason —
    # an unresolved CORPUS_REPO, say — said nothing about scope at all, which is
    # exactly when someone is iterating and most likely to read the banner as the
    # whole story. Caught by its own test; the fix was the code, not the test.
    scope_note = ("           validate_post is the authority on the post; "
                  "the rehearsal below runs it." if args.slug else "")

    if problems:
        print(f"NOT READY — {len(problems)} problem(s): {', '.join(problems)}")
        print("Fix the FAIL lines above, then run this again.")
        if scope_note:
            print(scope_note)
        return 1
    scope = "the machine is ready" if not args.slug else \
            "the machine is ready, and the post passed every check made here"
    if warnings:
        print(f"READY — {scope}, with {len(warnings)} warning(s): "
              f"{', '.join(warnings)}")
    else:
        print(f"READY — {scope}.")
    if args.slug:
        # Said "READY — every check passed" until 2026-09-22, about a post it had
        # only partly checked: it cleared one carrying a stray comment and a
        # sibling reference that validate_post refuses. A banner is a claim about
        # scope as much as about outcome.
        print(scope_note)
        print(f"\nRehearse:  python -c \"import server, json; "
              f"print(json.dumps(server.publish_post('{args.slug}'), indent=2))\"")
        print(f"Publish :  python -c \"import server, json; "
              f"print(json.dumps(server.publish_post('{args.slug}', for_real=True), indent=2))\"")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
