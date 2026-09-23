import os
import re
import sys
import json
import time
import subprocess
import tempfile
import importlib.util
import urllib.request
import urllib.error
from pathlib import Path
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("rnv-publishing")

# Config is resolved per call, not at import. Reading these into module constants
# meant a test or a demo run could not point the server anywhere without reimporting
# it, and the default silently won whenever the env var arrived late.
#
# WHERE the values come from lives in rnv_config.py, shared with tools/preflight.py
# so there is one definition rather than two that drift. The order is:
#   environment -> .env beside the repo -> a sibling checkout -> nothing found.
# The sibling rung is why a fresh machine usually needs no configuration at all: the
# three repos are cloned next to each other, and that relationship holds on every
# machine even when the absolute path does not. It retires the hardcoded /workspaces
# defaults, which were correct only in a Codespace and silently wrong everywhere else.
from rnv_config import resolve_path, resolve_site_url, describe, dotenv_problems


def blog_repo() -> Path:
    return resolve_path("BLOG_REPO")[0]


def corpus_repo() -> Path:
    return resolve_path("CORPUS_REPO")[0]


def site_url() -> str:
    return resolve_site_url()[0]


# ---------------------------------------------------------------------------
# Configuration errors must never wear a post error's face.
#
# The chain resolves three paths from the environment and falls back to Codespace
# defaults. Off a Codespace those defaults do not exist, and the failure used to
# surface late and misattributed: an unset BLOG_REPO made validate_post report
# "no index.html for <slug>" about a post sitting right there on disk. The tool
# blamed the post for a defect in the configuration, and a wrong assertion is
# worse than an absent one because it is trusted.
#
# Two rules follow, and they are not the same rule:
#
#   1. WHAT IS FATAL depends on whether the run can honestly do its job without
#      it. BLOG_REPO is fatal in both modes, because validation reads from it and
#      a dry run that cannot read anything has validated nothing; reporting ok
#      there would be a green result that never looked. CORPUS_REPO and SITE_URL
#      are fatal only for a real run: a dry run genuinely does not need them, so
#      it completes and carries a warning that the real run would fail.
#
#   2. WHEN IT IS CHECKED is the part that matters more. All three are checked
#      before commit_and_push, not when each stage reaches them. A broken
#      CORPUS_REPO used to fail at stage 5 — after the commit, after the push,
#      after the post was live — leaving a published post and a failed-looking
#      trace. A run that is knowably doomed must never take the irreversible
#      step. Principle 7, applied to configuration rather than intent.
#
# This is a gate, not an advisory, and the queue/defect test says why: waiting
# fixes a lagging CI job, and waiting never fixes a wrong path.
# ---------------------------------------------------------------------------

def _path_problem(var: str) -> str:
    """Describe what is wrong with a configured path, or '' if nothing is.

    The message names its provenance, because "BLOG_REPO does not resolve" is only
    half a diagnosis; the other half is whether that path came from the shell, from
    .env, or from looking beside the repo. Knowing which one it was is knowing which
    one to fix.
    """
    path, how = resolve_path(var)
    if not path.is_dir():
        return f"{var}: no directory at {path} (source: {how})"
    if var == "BLOG_REPO" and not (path / "blog").is_dir():
        return (f"{var}: {path} contains no blog/ directory (source: {how}); "
                "it should be the site repo root")
    return ""


def _git_problem(var: str) -> str:
    """Describe why a configured path is not a repo the chain can push to, or ''.

    WHY THIS EXISTS, and it is the same sentence as _path_problem's one layer down.
    Until 2026-09-22 the gate checked that each path resolved to a DIRECTORY, when
    what both stages need is a git repository with an `origin` to fetch from and
    push to. It therefore read as closed while looking at the cheaper half of its
    own precondition, and cleared three states that cannot work, all reproduced:
    a corpus that is a plain folder, a corpus that is a repo with no `origin`, and
    a site repo that is a plain folder.

    WHY THE GAP OPENED, which is the part worth keeping. The gate predates
    2026-09-18, when update_corpus began fetching and pushing rather than reading.
    A gate encodes an assumption about a neighbouring system, and when that
    system's contract strengthens the gate is silently wrong with nothing in
    either system pointing at it. The Brand & Corporate Architect's phrasing, kept
    because it is better than this project's was.

    ONE CALL, AND IT DOES NOT CLASSIFY. `git remote get-url origin` fails for both
    causes — 128 for "not a git repository", 2 for "No such remote" — and this
    reports git's own words rather than translating them. A caption that names a
    cause is a caption that can name the wrong one; that defect has now been fixed
    twice in this repo (2026-09-18 in the push hints, 2026-09-22 in preflight) and
    is not being written a third time. Local only: no network, ~2 ms, which is why
    it can sit in a gate that runs on every tool call.

    WHY REACHABILITY IS NOT CHECKED HERE. Whether `origin` answers, and whether it
    accepts a write, cost a network round trip each. config_report runs ahead of
    every tool, including every dry run, and a gate that reaches the network makes
    the cheap path expensive. That check is opt-in and lives in
    `tools/preflight.py --push-check`, which covers both repos as of 2026-09-22.
    """
    path, how = resolve_path(var)
    got = _git_in(path, "remote", "get-url", "origin")
    if got.returncode == 0:
        return ""
    said = _stderr_or(got, "git gave no reason").splitlines()
    return (f"{var}: {path} is not a git repository with an 'origin' remote, so "
            f"the chain cannot fetch or push there — {said[0] if said else ''} "
            f"(source: {how})")


def config_report(for_real: bool = False) -> dict:
    """Check every path the chain will need, before the chain needs it.

    Returns {"ok", "fatal", "problems", "warnings"}. `fatal` is True when the run
    cannot proceed; `problems` always lists everything found, so one call reports
    every misconfiguration rather than one per round trip.
    """
    problems: list[str] = []
    warnings: list[str] = []

    # A .env line that could not be used is reported and never gates. It does not
    # mean the resolution is wrong — the sibling rung may well be right — only that
    # an answer the operator thought they configured was not the one used. The
    # path checks below gate on what the run actually needs; this explains a
    # surprise rather than causing one. §3.0.5: gate a defect, warn about the rest.
    warnings.extend(f"{m} — resolution fell through to the next rung"
                    for m in dotenv_problems())

    blog = _path_problem("BLOG_REPO")
    if blog:
        problems.append(blog)

    corpus = _path_problem("CORPUS_REPO")
    url, url_how = resolve_site_url()
    site = "" if re.match(r"^https?://[^\s/]+\.[^\s/]+", url) else \
           f"SITE_URL is not a usable origin: {url!r} (source: {url_how})"

    # Only asked when the path itself is sound. Running git inside a directory
    # that does not exist reports "not a git repository" about a path whose real
    # problem is that it is not there — a config defect misattributed to a
    # different config defect, which is this module's own founding bug in
    # miniature.
    blog_git = _git_problem("BLOG_REPO") if not blog else ""
    corpus_git = _git_problem("CORPUS_REPO") if not corpus else ""

    # BOTH git checks are DEFERRED, including the site repo's, and that is a
    # decision rather than an oversight. A dry run reads the post off the
    # filesystem and never touches git, so it can honestly do its job in a plain
    # folder — the dry/real test is whether the run can do its work without the
    # value, not whether the value is important. Putting blog_git in `problems`
    # above would make `blog_fatal` true and stop validate_post from validating a
    # post that is perfectly readable.
    #
    # The two paths get the same DEPTH even though their failures cost different
    # amounts — the corpus dies at stage 5 with the post already live, the site
    # repo dies at commit_and_push with nothing published. You ration a check that
    # costs something, and this one is free. The asymmetry decides where the
    # EXPENSIVE check goes, not whether the cheap one runs.
    deferred = [m for m in (corpus, corpus_git, blog_git, site) if m]
    if for_real:
        problems.extend(deferred)
    else:
        warnings.extend(f"{m} — a dry run does not need it, but a real publish will fail" 
                        for m in deferred)

    # Two fatalities, deliberately separate, because they are checked at
    # different points in the chain:
    #   blog_fatal  - BLOG_REPO. Checked FIRST, before validate_post, because
    #                 validation reads from it and cannot run without it.
    #   fatal       - the whole set. Checked before commit_and_push, so a run
    #                 that will die at stage 5 never takes the irreversible step.
    # Keeping them separate matters: when BLOG_REPO is fine and only the corpus
    # path is wrong, validation IS trustworthy, and a half-written post must
    # still report as a half-written post. A config problem should never mask a
    # post problem it did not actually prevent us from seeing.
    return {"ok": not problems, "fatal": bool(blog) or (for_real and bool(deferred)),
            "blog_fatal": bool(blog),
            "problems": problems, "warnings": warnings,
            "resolved": describe()}


# ---------------------------------------------------------------------------
# The comment allowlist, owned by the site project.
#
# Defined in `_templates/post-template.RULES.md` in rnvizion.github.io: neither the
# template nor a post carries a comment that explains or instructs. Four structural
# markers stay, because they navigate rather than explain. This repo ASSERTS against
# that contract; it does not define it, so the list is copied here only because a
# check needs something to compare against — if the site project adds a fifth marker,
# this list is what goes stale, and a post carrying the new marker fails here first.
#
# **Checked exactly, as an allowlist.** The RULES.md is explicit that this is not a
# rule about brevity: "short comments stay" is a heuristic, and the first four-word
# instruction would defeat it silently.
#
# Why it gates rather than warns, under §3.0.5: does waiting fix it? A stray comment
# never removes itself, so it is the post's own defect, not a queue. It is also
# effectively irreversible once shipped — the comment lands in public git history and
# rides into the feed's content:encoded, which is how a template note reached dev.to.
ALLOWED_COMMENTS = frozenset({
    "<!-- Open Graph -->",
    "<!-- Fonts -->",
    "<!-- Blog-index card teaser. -->",
    "<!-- Standing author bio. -->",
})


def stray_comments(html: str) -> list[str]:
    """Comments in the post that are not on the site project's allowlist."""
    found = re.findall(r"<!--.*?-->", html, flags=re.S)
    return [c.strip() for c in found if c.strip() not in ALLOWED_COMMENTS]


def _strip_comments(html: str) -> str:
    return re.sub(r"<!--.*?-->", "", html, flags=re.S)

def _meta(html: str, attr: str, value: str) -> str:
    """Pull a <meta {attr}="{value}" content="..."> field; '' if absent.
    Backreference on the quote handles apostrophes inside the content."""
    m = re.search(
        rf'<meta\s+{attr}=["\']{re.escape(value)}["\']\s+content=(["\'])(.*?)\1',
        html, flags=re.I | re.S,
    )
    return m.group(2).strip() if m else ""

def _git_env() -> dict:
    """The environment for a git subprocess, with prompting disabled.

    GIT_TERMINAL_PROMPT=0 turns "ask the user" into an immediate error. Under the MCP
    transport there is no user to ask and no terminal to ask on, so a prompt is a hang
    with extra steps. Explicit failure beats silent waiting: the caller can report a
    credential problem, it cannot report a wait.
    """
    env = dict(os.environ)
    env["GIT_TERMINAL_PROMPT"] = "0"
    return env


def _git_in(repo, *args, index: str | None = None):
    """Run git inside `repo`; returns the CompletedProcess.

    The one place this module starts a subprocess, which is what makes the stdin
    rule above a property of the module rather than a habit at each call site.

    `index` points git at a private index file, so a commit can be assembled without
    touching the operator's staging area. Output is decoded as UTF-8 rather than in
    the locale's encoding: git returns file contents here — a post carries curly
    quotes by house style — and a Windows code page would mangle them.
    """
    env = _git_env()
    if index is not None:
        env["GIT_INDEX_FILE"] = index
    return subprocess.run(["git", *args], cwd=repo, capture_output=True,
                          encoding="utf-8", errors="replace",
                          stdin=subprocess.DEVNULL, env=env)


def _stderr_or(proc, default: str) -> str:
    return (proc.stderr or "").strip() or default


# Push stderr meaning "the remote moved since you looked". Both pushes answer it the
# same way: fetch the new main and make the whole decision again, never replay the
# one already made. Anything else is a real failure.
_REMOTE_MOVED = ("fetch first", "non-fast-forward", "behind its remote")

# Push stderr that names an access problem. The write-permission hint is reserved
# for these. Appended to every push failure, it once captioned a rebase conflict and
# sent the reader to check credentials that were fine. "returned error: 403" rather
# than a bare "403": the message also carries the remote's path, and a bare number
# can turn up in any path.
_PERMISSION_MARKERS = ("permission", "returned error: 403", "authentication failed",
                       "could not read username")


def _commit_file_on(run, base: str, path: str, blob: str, message: str) -> dict:
    """Build a commit on `base` whose one change is `path` becoming `blob`.

    Assembled in a private index, so the checkout's staging area, working tree and
    branch are never part of it: the commit is exactly `base` plus this one file.
    Nothing already staged in the checkout can ride along. The commit is on no
    branch; until a push lands it, it is an object nothing points at, which is what
    lets a failure leave nothing behind.

    Shared by both writers. What each one puts in the blob differs — the corpus
    derives its entry from main, the site carries the author's file through — but
    "main plus exactly one path" is one mechanism and is written once.
    """
    listing = run("ls-tree", base, "--", path).stdout.split()
    mode = listing[0] if listing else "100644"

    with tempfile.TemporaryDirectory() as tmp:
        index = str(Path(tmp) / "index")
        for args in (("read-tree", base),
                     ("update-index", "--add", "--cacheinfo", f"{mode},{blob},{path}")):
            step = run(*args, index=index)
            if step.returncode != 0:
                return {"ok": False, "error": _stderr_or(step, f"git {args[0]} failed")}
        tree = run("write-tree", index=index)
        if tree.returncode != 0:
            return {"ok": False, "error": _stderr_or(tree, "git write-tree failed")}

    commit = run("commit-tree", tree.stdout.strip(), "-p", base, "-m", message)
    if commit.returncode != 0:
        return {"ok": False, "error": _stderr_or(commit, "git commit-tree failed")}
    return {"ok": True, "sha": commit.stdout.strip()}

@mcp.tool()
def list_posts() -> list[dict]:
    """List every published post in the blog with its slug, title, and date."""
    cfg = config_report()
    if cfg["fatal"]:
        return {"ok": False, "error": "config", "problems": cfg["problems"]}
    blog_dir = blog_repo() / "blog"
    if not blog_dir.exists():
        raise ValueError(f"blog dir not found at {blog_dir} — set BLOG_REPO to your blog repo path")
    posts = []
    for index in sorted(blog_dir.glob("*/index.html")):
        html = _strip_comments(index.read_text(encoding="utf-8"))
        slug = index.parent.name
        title = _meta(html, "property", "og:title") or slug
        date = _meta(html, "property", "article:published_time")
        posts.append({"slug": slug, "title": title, "published": date})
    return posts

# ---------------------------------------------------------------------------
# The post-shape library, owned by the site project.
#
# Defined in `scripts/post_shape.py` in rnvizion.github.io, which states its own
# public interface: sibling_refs(html) and shown_outside_code(html), each taking an
# HTML string and returning a list of strings, and touching no filesystem, git or
# network. This repo IMPORTS it rather than reimplementing it, on the site project's
# ruling of 2026-09-20 and its own standing pattern: a second consumer imports the
# existing generator, it does not grow its own copy. A copy of a parser is the same
# defect as a second renderer, one layer down — and the rule moved twice in three
# days, so a copy would have drifted before it was a week old. Under import there is
# nothing to keep in sync, which is why the shared-vector scheme was retired.
#
# WHY BY EXPLICIT PATH RATHER THAN sys.path
#   Nothing is added to the import namespace, so a module in the site's scripts/ can
#   never shadow one of ours, and the path that was tried is reportable when it
#   fails. The same reasoning that keeps the agent from mutating sys.path to find its
#   own config: a search path is a guess, a resolved path is a fact.
#
# WHY IT IS RE-READ ON EVERY CALL
#   The operator pulls the site checkout between publishes. A module cached at first
#   use would keep asserting the rule as it stood when the server started, which is
#   the stale-cache failure this project has now fixed twice elsewhere.
POST_SHAPE_PATH = ("scripts", "post_shape.py")
POST_SHAPE_INTERFACE = ("sibling_refs", "shown_outside_code")


def _load_post_shape():
    """Import the site project's post-shape library.

    Returns the module. Raises RuntimeError naming the path when it is absent, will
    not import, or no longer offers the interface it declares — never a bare
    exception, because the caller has to tell a library problem from a post problem.
    """
    path = blog_repo().joinpath(*POST_SHAPE_PATH)
    if not path.is_file():
        raise RuntimeError(
            f"the site project's post-shape library is not at {path}; a checkout "
            f"predating 2026-09-20 does not carry it — pull the site repo")

    spec = importlib.util.spec_from_file_location("rnv_site_post_shape", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"{path} could not be loaded as a Python module")
    module = importlib.util.module_from_spec(spec)
    # Import without leaving a .pyc behind. Python caches bytecode next to the
    # source, so importing this would write scripts/__pycache__/ into the
    # OPERATOR'S site checkout — a publish is not entitled to create files in
    # somebody else's repo, even ignored ones, and the site project had to add a
    # .gitignore line for exactly this. Found by a test asserting the checkout was
    # clean after a publish, which it then was not.
    wrote_bytecode = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    try:
        spec.loader.exec_module(module)
    except BaseException as exc:
        # BaseException rather than Exception, and that is the whole point of this
        # clause. The failure it catches is a module that calls sys.exit() while
        # being imported, which raises SystemExit — not an Exception subclass — and
        # would otherwise end the publish with no result and no explanation. Not
        # hypothetical: importing the site's tests/test_post_shape.py did exactly
        # that, and it exited over a defect in a DIFFERENT post than the one being
        # published. The library is written not to, and its own CI pins that in a
        # subprocess; this is the half that does not depend on their CI having run.
        raise RuntimeError(
            f"{path} raised while being imported ({type(exc).__name__}: {exc})") from exc
    finally:
        sys.dont_write_bytecode = wrote_bytecode

    missing = [name for name in POST_SHAPE_INTERFACE
               if not callable(getattr(module, name, None))]
    if missing:
        raise RuntimeError(
            f"{path} does not define {', '.join(missing)}; the library's public "
            f"interface has moved and this repo's call has to move with it")
    return module


@mcp.tool()
def validate_post(slug: str) -> dict:
    """Check a post has everything the feed needs before publishing.
    Returns ok=False with the missing items if anything required is absent."""
    cfg = config_report()
    if cfg["fatal"]:
        return {"ok": False, "error": "config", "problems": cfg["problems"]}
    path = blog_repo() / "blog" / slug / "index.html"
    if not path.exists():
        return {"slug": slug, "ok": False, "error": f"no index.html at blog/{slug}/"}
    raw = path.read_text(encoding="utf-8")
    body = _strip_comments(raw)
    # Checked on the RAW html: _strip_comments exists so commented-out metadata does
    # not count as present, which is the opposite of what this needs to see.
    strays = stray_comments(raw)

    # The site project's rule, asked of the site project's library rather than
    # reimplemented here. `raw` is passed unmodified: sibling_refs does its own
    # comment stripping, <code>/<pre> cutting and escaped-markup neutralising, and
    # handing it something already normalised would be this repo deciding what the
    # rule means.
    try:
        siblings = _load_post_shape().sibling_refs(raw)
    except RuntimeError as exc:
        # Structurally a config failure, not a post failure: the post may be
        # perfect and we could not ask. Reporting ok here would be the green
        # result that did not look, and reporting it as a post defect would send
        # the author to fix a file that is fine.
        return {"slug": slug, "ok": False, "error": "config", "problems": [str(exc)]}

    # shown_outside_code() is deliberately not called. The wrapper rule is the site
    # project's, and a wrapper mistake breaks nothing a publish carries — their
    # build-feed guard catches it, and refusing there costs nobody a live post. The
    # library keeps it a separate function precisely so that skipping it is a
    # decision rather than an oversight; this comment is the decision.

    required = {
        "<article> block": bool(re.search(r"<article[^>]*>.*?</article>", body, flags=re.S)),
        "og:url": bool(_meta(body, "property", "og:url")),
        "article:published_time": bool(_meta(body, "property", "article:published_time")),
    }
    recommended = {
        "og:title": bool(_meta(body, "property", "og:title")),
        "og:description": bool(_meta(body, "property", "og:description")),
        "card:summary": bool(_meta(body, "name", "card:summary")),
        "article:author": bool(_meta(body, "property", "article:author")),
        "og:image": bool(_meta(body, "property", "og:image")),
    }
    missing_required = [k for k, ok in required.items() if not ok]
    missing_recommended = [k for k, ok in recommended.items() if not ok]
    out = {
        "slug": slug,
        "ok": not missing_required and not strays and not siblings,
        "missing_required": missing_required,
        "missing_recommended": missing_recommended,
    }
    if strays:
        out["stray_comments"] = strays
        out["error"] = "comment"
    # Gating, not warning, under §3.0.5: does waiting fix it? No workflow writes
    # under blog/<slug>/ — build-feed stages feed.xml, blog/index.html, sitemap.xml
    # and robots.txt, build-og stages assets/ — so nothing will ever arrive to
    # satisfy a reference that resolves beside the post. It is the post's own
    # defect, and a publish carries one path. The og:image is the queue case and is
    # absolute, which is why the library does not return it.
    if siblings:
        out["sibling_refs"] = siblings
        out["error"] = "sibling"
    return out

# The site repo's branch. Pages serves it and both generator Actions trigger on it,
# so a publish that lands on any other branch is one nobody ever sees.
SITE_BRANCH = "main"

# How many times a publish starts over when main moves under it. Each attempt is a
# fresh fetch and a fresh decision, never a replay of the last one.
_PUBLISH_ATTEMPTS = 3


def _site_git(*args, index: str | None = None):
    """Run git inside the site repo; returns the CompletedProcess."""
    return _git_in(blog_repo(), *args, index=index)


def _fast_forward_site_checkout(target: str, post: str) -> str:
    """Move the operator's branch onto the publish that just landed.

    Returns "" once the checkout shows it, or else the reason it was left alone.

    WHY THIS IS A RESET AND WHY THAT IS SAFE
      The post is the one path that differs, and the working tree's copy of it is
      byte-identical to `target`'s, because `target` was built from that exact blob
      moments ago. So moving there discards nothing: the author's bytes are already
      in the commit. `merge --ff-only` and `reset --keep` both refuse anyway — tried
      on 2026-09-18, both stop at "local changes would be overwritten", because they
      read the post as dirty without noticing where those bytes went.

      Which leaves `reset --hard`, and it is only safe under the conditions checked
      below. Tried in the same session: with an unrelated tracked file modified, it
      published correctly and silently reverted that file. So anything else modified
      or staged means this refuses and says what it found. Untracked files are not a
      reason to refuse — `reset --hard` does not remove them, confirmed alongside.
    """
    branch = _site_git("symbolic-ref", "--quiet", "--short", "HEAD")
    on = branch.stdout.strip()
    if branch.returncode != 0 or on != SITE_BRANCH:
        return f"it is on {on or 'a detached HEAD'}, not {SITE_BRANCH}"
    if _site_git("merge-base", "--is-ancestor", "HEAD", target).returncode != 0:
        return f"its {SITE_BRANCH} carries commits the remote's does not"

    dirty = _site_git("status", "--porcelain", "--untracked-files=no")
    if dirty.returncode != 0:
        return _stderr_or(dirty, "git status failed")
    # line[3:] is the path; a rename reads "old -> new" and will not match `post`,
    # so it lands in `others` and this refuses. Failing toward refusing is correct:
    # the cost of refusing is a pull, and the cost of not is somebody's work.
    others = sorted({line[3:].strip() for line in dirty.stdout.splitlines() if line.strip()}
                    - {post})
    if others:
        return f"it has other changes that a reset would discard: {', '.join(others)}"

    reset = _site_git("reset", "--hard", "--quiet", target)
    if reset.returncode != 0:
        return _stderr_or(reset, "git reset failed")
    return ""


@mcp.tool()
def commit_and_push(slug: str, message: str = "", dry_run: bool = False) -> dict:
    """Publish the post: one commit on the site repo's main, carrying one file.

    The commit is blog/<slug>/index.html and nothing else. The blog index, feed,
    sitemap and robots are regenerated and committed by the build-feed Action on this
    push, and the share image by build-og, so none of those are committed here.

    WHY IT DECIDES AGAINST MAIN, NOT THE LOCAL BRANCH
      Being behind is the normal resting state of this checkout: both Actions commit
      on top of every publish, so the clone is out of date seconds after each one. A
      tool that asks the local branch "does this post need publishing?" is asking a
      cache that nothing keeps current. It answered wrong three ways, all reproduced
      on 2026-09-18 against this suite's fixtures:

        - after a refused push, the commit stayed on the local branch; the re-run
          saw the post matching that commit, reported "nothing to commit" and
          "published: true", and main never received it. A new post instead timed
          out in wait_for_live, reported as *not live* when the cause was *never
          pushed*.
        - a catch-up rebase that emptied the commit still reported "pushed".
        - the push sent every unpushed commit on the branch, not only the post's.

      So the question is put to main. `git hash-object` on the working tree's file
      gives the exact blob a publish would carry; main's copy of that path is one
      `rev-parse` away; equal hashes mean the published post is already this file,
      which is the real answer to "is there anything to do".

    WHY IT BUILDS THE COMMIT RATHER THAN COMMITTING AND PUSHING ONE
      The commit is assembled in a private index as main plus that one blob, so
      nothing in the checkout — its branch, its staging area, its other unpushed
      commits — can ride along. Until the push lands it is an object nothing points
      at, which is what lets a failure leave nothing behind. If main moves in
      between, the push is rejected and the whole decision runs again against the
      new main, including whether the post still needs publishing at all.

      This differs from update_corpus in what is rebuilt, and the difference matters.
      A corpus entry is derived, so it can be recomputed from main. A post is
      authored and cannot be; what is re-derived here is only the commit, and the
      author's bytes are carried through untouched.

    Once the push lands, the operator's branch is fast-forwarded onto it when that is
    safe, so their checkout ends clean; when it is not, `checkout_warning` says the
    checkout was left where it was. A warning, not a failure: main is right, and
    nothing decides from the checkout any more.

    Idempotent: a post already published as this exact file reports nothing to
    commit. dry_run=True fetches main and makes the same decision, without writing
    or pushing.
    """
    post = f"blog/{slug}/index.html"
    msg = message or f"Publish: {slug}"
    post_path = blog_repo() / "blog" / slug / "index.html"

    # A missing post is an error, not "nothing to commit". `git status` on a path
    # that does not exist and is not tracked reports nothing, which the old code read
    # as "already up to date" — the fail-open shape of this whole defect, one layer
    # down and reachable by calling this tool on its own.
    if not post_path.is_file():
        return {"slug": slug, "ok": False, "committed": False,
                "error": f"{post} does not exist under {blog_repo()}; there is nothing to publish"}

    tracking = f"refs/remotes/origin/{SITE_BRANCH}"
    for attempt in range(1, _PUBLISH_ATTEMPTS + 1):
        fetch = _site_git("fetch", "--quiet", "origin", f"+refs/heads/{SITE_BRANCH}:{tracking}")
        if fetch.returncode != 0:
            return {"slug": slug, "ok": False, "committed": False, "pushed": False,
                    "error": f"could not fetch the site's {SITE_BRANCH}, so nothing was decided "
                             f"or written: {_stderr_or(fetch, 'git fetch failed')}"}
        base = _site_git("rev-parse", "--verify", "--quiet", f"{tracking}^{{commit}}").stdout.strip()
        if not base:
            return {"slug": slug, "ok": False, "committed": False, "pushed": False,
                    "error": f"the site repo has no {SITE_BRANCH} on its remote to publish onto"}

        # -w writes the blob now, so the thing compared and the thing published are
        # one object rather than two reads of a file that could change in between.
        #
        # FILTERS ARE APPLIED, and the absence of --no-filters here is deliberate.
        # This hash is compared against main's blob for a TRACKED path, and main's
        # blob is what git stored after its own filters ran. On a checkout with
        # core.autocrlf=true — the normal Windows/Git Bash setup, which this
        # operator uses — the working tree holds CRLF while the blob holds LF, so
        # hashing the raw bytes compares two things git never claimed were equal.
        # Measured on 2026-09-20 in a faithful Windows-style clone: git status
        # reported the tree clean, --no-filters mismatched main, and filters
        # applied matched it exactly. With --no-filters this would have decided
        # every unchanged post needed republishing, and committed CRLF over the
        # whole file each time. _corpus_commit_on keeps --no-filters for the
        # opposite reason: it hashes bytes this agent authored, in a temp file
        # outside the repo, where verbatim is the intent and no filter applies.
        blob = _site_git("hash-object", "-w", "--", str(post_path))
        if blob.returncode != 0:
            return {"slug": slug, "ok": False, "committed": False,
                    "error": _stderr_or(blob, "git hash-object failed")}
        authored = blob.stdout.strip()
        published = _site_git("rev-parse", "--verify", "--quiet", f"{base}:{post}").stdout.strip()

        if published == authored:
            return {"slug": slug, "ok": True, "committed": False,
                    "reason": f"nothing to commit ({SITE_BRANCH} already has this exact post)"}
        if dry_run:
            return {"slug": slug, "ok": True, "committed": False,
                    "would_commit": [post], "message": msg,
                    "change": "update" if published else "add"}

        built = _commit_file_on(_site_git, base, post, authored, msg)
        if not built["ok"]:
            return {"slug": slug, "ok": False, "committed": False, "pushed": False,
                    "error": f"could not build the publish commit: {built['error']}"}

        push = _site_git("push", "origin", f"{built['sha']}:refs/heads/{SITE_BRANCH}")
        if push.returncode == 0:
            break
        err = _stderr_or(push, "git push failed")
        if any(m in err for m in _REMOTE_MOVED):
            continue    # main moved since the fetch: decide again, against the new main
        if any(m in err.lower() for m in _PERMISSION_MARKERS):
            err += " (site repo write permission?)"
        return {"slug": slug, "ok": False, "committed": False, "pushed": False, "error": err}
    else:
        return {"slug": slug, "ok": False, "committed": False, "pushed": False,
                "error": f"the site's {SITE_BRANCH} moved during each of {_PUBLISH_ATTEMPTS} "
                         f"attempts, so nothing was pushed; a re-run is safe"}

    out = {"slug": slug, "ok": True, "committed": True, "pushed": True,
           "message": msg, "files": [post], "commit": built["sha"][:7]}
    # Present only when it happened. A field that reads false on almost every run is
    # noise in a trace a human reads, and its absence is already the answer.
    if attempt > 1:
        out["caught_up"] = True
    held = _fast_forward_site_checkout(built["sha"], post)
    if held:
        out["checkout_warning"] = (
            f"published on {SITE_BRANCH}; your local site checkout was left where it was "
            f"because {held}. Nothing is wrong on {SITE_BRANCH}; pull when convenient"
        )
    return out

def _fetch_text(url: str, timeout: int = 15):
    """GET a URL; return (status, body_text). Status is the error string if
    unreachable, and the body is '' whenever there is nothing to read."""
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            raw = r.read()
            if isinstance(raw, bytes):
                raw = raw.decode("utf-8", errors="replace")
            return r.status, raw
    except urllib.error.HTTPError as e:
        return e.code, ""
    except Exception as e:  # connection errors, timeouts, DNS, etc.
        return str(e), ""

def _head_status(url: str, timeout: int = 15):
    """HEAD a URL; return the HTTP status code, or the error string if unreachable."""
    try:
        req = urllib.request.Request(url, method="HEAD")
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code
    except Exception as e:  # connection errors, timeouts, DNS, etc.
        return str(e)

def _progress(what: str, elapsed: float, budget: int, detail: str = "") -> None:
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
def wait_for_live(slug: str, timeout: int = 180, interval: int = 10,
                  og_timeout: int = 90, sitemap_timeout: int = 60) -> dict:
    """Poll the live post URL until it returns HTTP 200, then confirm the post's
    og:image is live too.

    Run after commit_and_push and before re-ingesting, so the RAG never fetches a
    404/403 during the GitHub Pages deploy window.

    Three checks, two roles:
      - The PAGE is the gate. ok=False if it isn't 200 within `timeout` seconds.
      - The SITEMAP is ADVISORY, and it is the check that says the *site* caught up
        rather than just the post. The deploy lands in two waves: this push puts the
        post's own file live (wave one), then the build-feed Action commits the blog
        index, the feed, and the sitemap, which go live a beat later (wave two). A
        200 on the post page only ever proved wave one. Between the waves the post
        loads fine while the index and sitemap do not know it exists, so anything
        reading the site as a whole — a preview scraper, a crawler, a reader landing
        on the blog index — sees a half-updated site. The sitemap is the cheapest
        proof wave two landed, because the post appears in it only after build-feed
        regenerates it. Advisory for the same reason the image is: a lagging sitemap
        is another process catching up, not this post being defective. Gate what you
        are judging; warn about what you are waiting on. (If something downstream
        ever *reads* the sitemap rather than the post URL, this stops being advisory
        and becomes a real dependency — move it above update_corpus and return
        ok=False. Verify the consumer before promoting it.)
      - The og:image is ADVISORY. It's rendered and committed by the build-og
        Action *after* this push, so it legitimately lands a beat later; it gets
        its own `og_timeout` budget starting once the page is live. A missing image
        does NOT fail the publish (the post and corpus are fine without it), but it
        is surfaced loudly as og_image_live=False so a broken share card never
        passes silently — the exact gap that shipped three imageless posts before.

    The image URL is read from the post's own og:image meta, so the check follows
    whatever the post actually claims rather than a hardcoded path."""
    url = f"{site_url()}/blog/{slug}/"
    deadline = time.monotonic() + timeout
    last = None
    while True:
        try:
            with urllib.request.urlopen(url, timeout=15) as r:
                last = r.status
                if r.status == 200:
                    break
        except urllib.error.HTTPError as e:
            last = e.code
        except Exception as e:  # connection errors, timeouts, DNS, etc.
            last = str(e)
        if time.monotonic() >= deadline:
            return {"slug": slug, "ok": False, "live": False, "url": url,
                    "last_status": last, "error": f"not live after {timeout}s (last seen: {last})"}
        _progress("the page", timeout - (deadline - time.monotonic()), timeout,
                  f" (last status: {last})")
        time.sleep(max(interval, 1))

    result = {"slug": slug, "ok": True, "live": True, "status": 200, "url": url}

    # Page is live: wave one finished. Now find out whether wave two did, by asking
    # the sitemap whether it has heard of this post yet. Same poll-and-retry shape as
    # the image check below, pointed at a different target.
    sitemap_url = f"{site_url()}/sitemap.xml"
    result["sitemap_url"] = sitemap_url
    needle = f"/blog/{slug}/"
    sm_deadline = time.monotonic() + sitemap_timeout
    sm_status = None
    while True:
        sm_status, body = _fetch_text(sitemap_url)
        if sm_status == 200 and needle in body:
            result["sitemap_listed"] = True
            break
        if time.monotonic() >= sm_deadline:
            result["sitemap_listed"] = False
            result["sitemap_status"] = sm_status
            result["sitemap_warning"] = (
                f"{sitemap_url} does not list {needle} after {sitemap_timeout}s "
                f"(last status: {sm_status}); the build-feed Action may still be "
                f"running, so the blog index and feed may not show this post yet"
            )
            break
        _progress("the sitemap", sitemap_timeout - (sm_deadline - time.monotonic()),
                  sitemap_timeout, f" (last status: {sm_status})")
        time.sleep(max(interval, 1))

    # Now confirm the og:image the post declares is reachable.
    post_path = blog_repo() / "blog" / slug / "index.html"
    og_image = ""
    if post_path.exists():
        og_image = _meta(_strip_comments(post_path.read_text(encoding="utf-8")),
                         "property", "og:image")
    if not og_image:
        result["og_image_live"] = None
        result["og_image_warning"] = "post declares no og:image meta — nothing to verify"
        return result

    result["og_image_url"] = og_image
    og_deadline = time.monotonic() + og_timeout
    og_last = None
    while True:
        og_last = _head_status(og_image)
        if og_last == 200:
            result["og_image_live"] = True
            result["og_image_status"] = 200
            return result
        if time.monotonic() >= og_deadline:
            result["og_image_live"] = False
            result["og_image_status"] = og_last
            result["og_image_warning"] = (
                f"og:image not live after {og_timeout}s (last seen: {og_last}); "
                f"the build-og Action may still be running, or failed to render {og_image}"
            )
            return result
        _progress("the share image", og_timeout - (og_deadline - time.monotonic()),
                  og_timeout, f" (last status: {og_last})")
        time.sleep(max(interval, 1))

# The corpus repo's branch. Its workflows fire on pushes to main and nowhere else,
# so a registration that lands on any other branch is one nothing ever reads.
CORPUS_BRANCH = "main"

# How many times a registration starts over when main moves under it. Each attempt
# is a fresh fetch and a fresh decision; three matches ship-index.yml's own loop.
_REGISTER_ATTEMPTS = 3

def _corpus_git(*args, index: str | None = None):
    """Run git inside the corpus repo; returns the CompletedProcess."""
    return _git_in(corpus_repo(), *args, index=index)


def _corpus_commit_on(base: str, content: str, message: str) -> dict:
    """Build a commit on `base` whose one change is sources.json becoming `content`.

    The entry is derived from `base` by the caller, so this only has to get those
    bytes into the object store; _commit_file_on does the rest."""
    with tempfile.TemporaryDirectory() as tmp:
        body = Path(tmp) / "sources.json"
        body.write_bytes(content.encode("utf-8"))   # bytes, so no newline translation
        blob = _corpus_git("hash-object", "-w", "--no-filters", "--", str(body))
        if blob.returncode != 0:
            return {"ok": False, "error": _stderr_or(blob, "git hash-object failed")}
    return _commit_file_on(_corpus_git, base, "sources.json", blob.stdout.strip(), message)


def _bring_corpus_checkout_along(target: str) -> str:
    """Fast-forward the corpus checkout to a registration that just landed on main.

    Returns "" once the checkout shows it, or else the reason it was left alone. Only
    ever a fast-forward of the checkout's own main: never a merge, a reset, or a
    stash. The registration is already on main when this runs, so a checkout that
    cannot follow is out of date, not wrong."""
    branch = _corpus_git("symbolic-ref", "--quiet", "--short", "HEAD")
    on = branch.stdout.strip()
    if branch.returncode != 0 or on != CORPUS_BRANCH:
        return f"it is on {on or 'a detached HEAD'}, not {CORPUS_BRANCH}"
    if _corpus_git("merge-base", "--is-ancestor", "HEAD", target).returncode != 0:
        return f"its {CORPUS_BRANCH} has commits the remote's does not"
    ff = _corpus_git("merge", "--ff-only", "--quiet", target)
    if ff.returncode != 0:
        said = (ff.stderr or ff.stdout or "").strip().splitlines()
        return f"a fast-forward would touch local changes ({said[0] if said else 'merge refused'})"
    return ""


@mcp.tool()
def update_corpus(slug: str, dry_run: bool = False) -> dict:
    """Register a published post with the RAG corpus, on the corpus repo's main.

    Verifies the live post URL returns 200 (refuses to register a dead source), then
    adds {"id": slug, "url": url} to sources.json on main, unless main already lists
    it, and pushes. rebuild-corpus.yml in the corpus repo does everything after that
    push: rebuild the index, commit it, gate it, deploy it. So this tool stays light
    and never imports the ML stack.

    WHY IT DECIDES AGAINST MAIN, NOT THE CHECKOUT
      The agent is not the only registrar. check-source-edits.yml in the corpus repo
      runs every six hours and registers any post in the feed that sources.json lacks,
      writing the same entry. The local checkout is a cache of main, current only as
      of whatever last pulled it, and since 2026-09-17 an Action commits the index,
      which retired the last routine reason to pull it. Deciding from the checkout
      failed three ways, each reproduced on 2026-09-18 against this suite's fixtures:
      a post the other registrar had already added came back "added, pushed" for a
      commit that rebased to nothing; a post it had not added failed on a rebase
      conflict reported as a permission problem; and the re-run after that read the
      tool's own unpushed commit, said "already in sources.json", and went green
      while main never received the entry.

    WHY IT BUILDS THE COMMIT RATHER THAN REBASING ONE
      The entry is a function of main's sources.json and the slug, so it is derived
      again on top of whatever main holds, never replayed onto it. The commit is
      assembled in a private index and pushed as exactly one commit on main: the
      checkout's branch, staging area, working tree and any unpushed work in it are
      not part of it. If main moves between the fetch and the push, the push is
      rejected and the whole decision runs again against the new main, including
      whether the entry is still needed. The corpus's own ship-index.yml treats
      chroma/ the same way: replaced, never merged.

    Nothing is left behind on failure. After the push lands, the checkout is
    fast-forwarded if that is safe; when it is not (another branch, local commits,
    local edits to sources.json), the registration still stands and
    `checkout_warning` says the checkout was left where it was. A warning, not a
    failure: main is right, and a stale checkout no longer decides anything.

    dry_run=True fetches main and makes the same decision, without committing or
    pushing."""
    url = f"{site_url()}/blog/{slug}/"

    # Refuse to register a source that isn't live (no broken sources).
    try:
        with urllib.request.urlopen(url, timeout=15) as r:
            if r.status != 200:
                return {"slug": slug, "ok": False,
                        "error": f"{url} returned {r.status}; not registering a dead source (run wait_for_live first)"}
    except Exception as e:
        return {"slug": slug, "ok": False,
                "error": f"{url} not reachable ({e}); run wait_for_live first"}

    sources_path = corpus_repo() / "sources.json"
    if not sources_path.exists():
        return {"slug": slug, "ok": False,
                "error": f"sources.json not found at {sources_path} — set CORPUS_REPO to your ask-the-corpus checkout"}

    tracking = f"refs/remotes/origin/{CORPUS_BRANCH}"
    for attempt in range(1, _REGISTER_ATTEMPTS + 1):
        fetch = _corpus_git("fetch", "--quiet", "origin", f"+refs/heads/{CORPUS_BRANCH}:{tracking}")
        if fetch.returncode != 0:
            return {"slug": slug, "ok": False,
                    "error": f"could not fetch the corpus's {CORPUS_BRANCH}, so nothing was decided "
                             f"or written: {_stderr_or(fetch, 'git fetch failed')}"}
        base = _corpus_git("rev-parse", "--verify", "--quiet", f"{tracking}^{{commit}}").stdout.strip()
        listed = _corpus_git("show", f"{base}:sources.json") if base else None
        if listed is None or listed.returncode != 0:
            return {"slug": slug, "ok": False,
                    "error": f"the corpus's {CORPUS_BRANCH} has no sources.json to register against"}
        try:
            data = json.loads(listed.stdout)
        except json.JSONDecodeError as e:
            return {"slug": slug, "ok": False,
                    "error": f"sources.json on {CORPUS_BRANCH} is not valid JSON ({e}); not writing over it"}

        sources = data.setdefault("sources", [])
        if any(s.get("url") == url or s.get("id") == slug for s in sources):
            return {"slug": slug, "ok": True, "added": False, "reason": "already in sources.json"}
        if dry_run:
            return {"slug": slug, "ok": True, "added": False, "would_add": {"id": slug, "url": url}}

        sources.append({"id": slug, "url": url})
        commit = _corpus_commit_on(base, json.dumps(data, indent=2) + "\n", f"corpus: add {slug}")
        if not commit["ok"]:
            return {"slug": slug, "ok": False,
                    "error": f"could not build the registration commit: {commit['error']}"}

        push = _corpus_git("push", "origin", f"{commit['sha']}:refs/heads/{CORPUS_BRANCH}")
        if push.returncode == 0:
            break
        err = _stderr_or(push, "git push failed")
        if any(m in err for m in _REMOTE_MOVED):
            continue    # main moved since the fetch: decide again, against the new main
        if any(m in err.lower() for m in _PERMISSION_MARKERS):
            err += " (corpus repo write permission?)"
        return {"slug": slug, "ok": False, "pushed": False, "error": err}
    else:
        return {"slug": slug, "ok": False, "pushed": False,
                "error": f"the corpus's {CORPUS_BRANCH} moved during each of {_REGISTER_ATTEMPTS} "
                         f"attempts, so nothing was pushed; a re-run is safe"}

    out = {"slug": slug, "ok": True, "added": True, "pushed": True, "url": url,
           "commit": commit["sha"][:7],
           "note": f"pushed to {CORPUS_BRANCH}; rebuild-corpus.yml rebuilds, commits, gates and deploys the index from here"}
    # Present only when it happened, as in commit_and_push.
    if attempt > 1:
        out["caught_up"] = True
    held = _bring_corpus_checkout_along(commit["sha"])
    if held:
        out["checkout_warning"] = (
            f"registered on {CORPUS_BRANCH}; the local corpus checkout was left where it was "
            f"because {held}. Nothing is wrong on {CORPUS_BRANCH}; pull when convenient"
        )
    return out


@mcp.tool()
def publish_post(slug: str, for_real: bool = False, timeout: int = 180,
                 interval: int = 10, og_timeout: int = 90,
                 sitemap_timeout: int = 60) -> dict:
    """Run the publish chain for a post in one deterministic sequence and stop at
    the first failure, returning the full trace.

    Order: validate_post -> (if for_real) commit_and_push -> wait_for_live ->
    update_corpus. The blog index, the RSS feed, and the OG image are built and
    committed by GitHub Actions on the push, so they are not steps here.

    for_real=False (the default) is a dry run: it validates the post and stops,
    confirming it clears the bar before anything is pushed. for_real=True runs the
    whole chain for keeps. This is the one call the agent should use to publish;
    the individual tools remain for single-step work.

    A live-but-imageless share card is a warning, not a failure: the post still
    publishes and the corpus still ingests, but `warnings` carries the og:image
    notice so it never slips by unseen.

    timeout, interval and og_timeout are forwarded to wait_for_live. The defaults are
    tuned for a real GitHub Pages deploy; they exist as parameters so a test or a
    demo can exercise the whole chain without waiting three minutes on a poll."""
    trace = []
    warnings = []

    cfg = config_report(for_real=for_real)
    warnings.extend(cfg["warnings"])

    def config_stop():
        return {"slug": slug, "ok": False, "stopped_at": "config",
                "error": "config", "problems": cfg["problems"],
                "trace": trace + [{"step": "config_report", **cfg}]}

    # Gate one: can we read the post at all? Without BLOG_REPO validation
    # cannot run, and a dry run that validated nothing must not report ok.
    if cfg["blog_fatal"]:
        return config_stop()

    def step(name, result):
        entry = {"step": name}
        entry.update(result if isinstance(result, dict) else {"result": result})
        trace.append(entry)
        return result

    v = step("validate_post", validate_post(slug))
    if not v.get("ok"):
        return {"slug": slug, "ok": False, "stopped_at": "validate_post", "trace": trace}

    if not for_real:
        out = {"slug": slug, "ok": True, "dry_run": True,
               "note": "dry run — validated only; nothing written or pushed. The index, feed, and image build in CI on a real publish.",
               "trace": trace}
        if warnings:
            out["warnings"] = warnings
        return out

    # Gate two: will the REST of the chain work? Checked here, before the one
    # irreversible step, rather than when stage 5 finally reaches CORPUS_REPO.
    if cfg["fatal"]:
        return config_stop()

    cp = step("commit_and_push", commit_and_push(slug))
    if not cp.get("ok"):
        return {"slug": slug, "ok": False, "stopped_at": "commit_and_push", "trace": trace}
    if cp.get("checkout_warning"):
        warnings.append(cp["checkout_warning"])

    live = step("wait_for_live", wait_for_live(slug, timeout=timeout, interval=interval,
                                               og_timeout=og_timeout,
                                               sitemap_timeout=sitemap_timeout))
    if not live.get("ok"):
        return {"slug": slug, "ok": False, "stopped_at": "wait_for_live", "trace": trace}
    if live.get("sitemap_listed") is False:
        warnings.append(live.get("sitemap_warning", "sitemap does not list the post yet"))
    if live.get("og_image_live") is False:
        warnings.append(live.get("og_image_warning", "og:image not live yet"))

    uc = step("update_corpus", update_corpus(slug))
    if not uc.get("ok"):
        return {"slug": slug, "ok": False, "stopped_at": "update_corpus", "trace": trace}
    if uc.get("checkout_warning"):
        warnings.append(uc["checkout_warning"])

    result = {"slug": slug, "ok": True, "published": True, "trace": trace}
    if warnings:
        result["warnings"] = warnings
    return result

if __name__ == "__main__":
    print("rnv-publishing MCP server ready", file=sys.stderr, flush=True)
    mcp.run()
