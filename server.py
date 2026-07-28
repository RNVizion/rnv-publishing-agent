import os
import re
import sys
import json
import time
import subprocess
import urllib.request
import urllib.error
from pathlib import Path
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("rnv-publishing")

BLOG_REPO = Path(os.environ.get("BLOG_REPO", "/workspaces/rnvizion.github.io"))
SITE_URL = os.environ.get("SITE_URL", "https://rnvizion.dev")
CORPUS_REPO = Path(os.environ.get("CORPUS_REPO", "/workspaces/rnv-ask-the-corpus"))

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

def _git(*args):
    """Run a git command inside the blog repo; returns the CompletedProcess."""
    return subprocess.run(["git", *args], cwd=BLOG_REPO, capture_output=True, text=True)

@mcp.tool()
def list_posts() -> list[dict]:
    """List every published post in the blog with its slug, title, and date."""
    blog_dir = BLOG_REPO / "blog"
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

@mcp.tool()
def validate_post(slug: str) -> dict:
    """Check a post has everything the feed needs before publishing.
    Returns ok=False with the missing items if anything required is absent."""
    path = BLOG_REPO / "blog" / slug / "index.html"
    if not path.exists():
        return {"slug": slug, "ok": False, "error": f"no index.html at blog/{slug}/"}
    body = _strip_comments(path.read_text(encoding="utf-8"))
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
    return {
        "slug": slug,
        "ok": not missing_required,
        "missing_required": missing_required,
        "missing_recommended": missing_recommended,
    }

@mcp.tool()
def commit_and_push(slug: str, message: str = "", dry_run: bool = False) -> dict:
    """Stage and commit the post, then push to the remote.

    Stages only blog/<slug>/index.html — the one file a publish writes. The blog
    index and feed are regenerated and committed by the build-feed GitHub Action
    on this push, and the OG share image by the build-og Action, so none of those
    are committed here. Idempotent: if the post has no changes, it reports nothing
    to commit rather than erroring.
    dry_run=True reports what would be committed without writing or pushing."""
    post = f"blog/{slug}/index.html"
    msg = message or f"Publish: {slug}"

    status = _git("status", "--porcelain", "--", post)
    if status.returncode != 0:
        return {"slug": slug, "ok": False, "error": status.stderr.strip() or "git status failed"}
    pending = [line[3:] for line in status.stdout.splitlines() if line.strip()]
    if not pending:
        return {"slug": slug, "ok": True, "committed": False,
                "reason": "nothing to commit (post already up to date)"}

    if dry_run:
        return {"slug": slug, "ok": True, "committed": False,
                "would_commit": pending, "message": msg}

    add = _git("add", "--", post)
    if add.returncode != 0:
        return {"slug": slug, "ok": False, "error": add.stderr.strip() or "git add failed"}
    commit = _git("commit", "-m", msg)
    if commit.returncode != 0:
        return {"slug": slug, "ok": False, "error": commit.stderr.strip() or "git commit failed"}
    push = _git("push")
    if push.returncode != 0:
        return {"slug": slug, "ok": False, "committed": True, "pushed": False,
                "error": push.stderr.strip() or "git push failed"}
    return {"slug": slug, "ok": True, "committed": True, "pushed": True,
            "message": msg, "files": pending}

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

@mcp.tool()
def wait_for_live(slug: str, timeout: int = 180, interval: int = 10,
                  og_timeout: int = 90) -> dict:
    """Poll the live post URL until it returns HTTP 200, then confirm the post's
    og:image is live too.

    Run after commit_and_push and before re-ingesting, so the RAG never fetches a
    404/403 during the GitHub Pages deploy window.

    Two checks, two roles:
      - The PAGE is the gate. ok=False if it isn't 200 within `timeout` seconds.
      - The og:image is ADVISORY. It's rendered and committed by the build-og
        Action *after* this push, so it legitimately lands a beat later; it gets
        its own `og_timeout` budget starting once the page is live. A missing image
        does NOT fail the publish (the post and corpus are fine without it), but it
        is surfaced loudly as og_image_live=False so a broken share card never
        passes silently — the exact gap that shipped three imageless posts before.

    The image URL is read from the post's own og:image meta, so the check follows
    whatever the post actually claims rather than a hardcoded path."""
    url = f"{SITE_URL}/blog/{slug}/"
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
        time.sleep(interval)

    result = {"slug": slug, "ok": True, "live": True, "status": 200, "url": url}

    # Page is live. Now confirm the og:image the post declares is reachable.
    post_path = BLOG_REPO / "blog" / slug / "index.html"
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
        time.sleep(interval)

@mcp.tool()
def update_corpus(slug: str, dry_run: bool = False) -> dict:
    """Register a published post with the RAG corpus and trigger a rebuild.

    Verifies the live post URL returns 200 (refuses to register a dead source),
    appends it to the corpus sources.json if not already present, then commits and
    pushes that change. A GitHub Action in the corpus repo (rebuild-corpus.yml)
    does the actual re-ingest and Hugging Face Space push on that push, so this tool
    stays light and never imports the ML stack.
    dry_run=True reports what it would add without writing or pushing."""
    url = f"{SITE_URL}/blog/{slug}/"

    # Refuse to register a source that isn't live (no broken sources).
    try:
        with urllib.request.urlopen(url, timeout=15) as r:
            if r.status != 200:
                return {"slug": slug, "ok": False,
                        "error": f"{url} returned {r.status}; not registering a dead source (run wait_for_live first)"}
    except Exception as e:
        return {"slug": slug, "ok": False,
                "error": f"{url} not reachable ({e}); run wait_for_live first"}

    sources_path = CORPUS_REPO / "sources.json"
    if not sources_path.exists():
        return {"slug": slug, "ok": False,
                "error": f"sources.json not found at {sources_path} — set CORPUS_REPO to your ask-the-corpus checkout"}

    data = json.loads(sources_path.read_text(encoding="utf-8"))
    sources = data.setdefault("sources", [])
    if any(s.get("url") == url or s.get("id") == slug for s in sources):
        return {"slug": slug, "ok": True, "added": False, "reason": "already in sources.json"}

    if dry_run:
        return {"slug": slug, "ok": True, "added": False, "would_add": {"id": slug, "url": url}}

    sources.append({"id": slug, "url": url})
    sources_path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")

    def _cgit(*args):
        return subprocess.run(["git", *args], cwd=CORPUS_REPO, capture_output=True, text=True)

    add = _cgit("add", "--", "sources.json")
    if add.returncode != 0:
        return {"slug": slug, "ok": False, "added": True, "error": add.stderr.strip() or "git add failed"}
    commit = _cgit("commit", "-m", f"corpus: add {slug}")
    if commit.returncode != 0:
        return {"slug": slug, "ok": False, "added": True, "error": commit.stderr.strip() or "git commit failed"}
    push = _cgit("push")
    if push.returncode != 0:
        return {"slug": slug, "ok": False, "added": True, "pushed": False,
                "error": push.stderr.strip() or "git push failed (corpus repo write permission?)"}
    return {"slug": slug, "ok": True, "added": True, "pushed": True, "url": url,
            "note": "pushed; the rebuild-corpus Action will re-ingest and update the Space"}

@mcp.tool()
def publish_post(slug: str, for_real: bool = False) -> dict:
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
    notice so it never slips by unseen."""
    trace = []
    warnings = []

    def step(name, result):
        entry = {"step": name}
        entry.update(result if isinstance(result, dict) else {"result": result})
        trace.append(entry)
        return result

    v = step("validate_post", validate_post(slug))
    if not v.get("ok"):
        return {"slug": slug, "ok": False, "stopped_at": "validate_post", "trace": trace}

    if not for_real:
        return {"slug": slug, "ok": True, "dry_run": True,
                "note": "dry run — validated only; nothing written or pushed. The index, feed, and image build in CI on a real publish.",
                "trace": trace}

    cp = step("commit_and_push", commit_and_push(slug))
    if not cp.get("ok"):
        return {"slug": slug, "ok": False, "stopped_at": "commit_and_push", "trace": trace}

    live = step("wait_for_live", wait_for_live(slug))
    if not live.get("ok"):
        return {"slug": slug, "ok": False, "stopped_at": "wait_for_live", "trace": trace}
    if live.get("og_image_live") is False:
        warnings.append(live.get("og_image_warning", "og:image not live yet"))

    uc = step("update_corpus", update_corpus(slug))
    if not uc.get("ok"):
        return {"slug": slug, "ok": False, "stopped_at": "update_corpus", "trace": trace}

    result = {"slug": slug, "ok": True, "published": True, "trace": trace}
    if warnings:
        result["warnings"] = warnings
    return result

if __name__ == "__main__":
    print("rnv-publishing MCP server ready", file=sys.stderr, flush=True)
    mcp.run()
