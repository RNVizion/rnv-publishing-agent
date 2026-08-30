"""Fixtures that build a real blog repo, a real corpus repo, and a real bare remote.

Nothing here is mocked at the git layer: the tests run actual commits and pushes
against a local bare repository, so the code path under test is the same one that
runs against GitHub. Only the network calls to the live site are substituted, and
those are substituted at the urllib boundary rather than inside the tools.
"""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

COMPLETE_POST = """<!doctype html>
<html lang="en">
<head>
<meta property="og:url" content="{site}/blog/{slug}/">
<meta property="og:title" content="{title}">
<meta property="og:description" content="A post that clears the bar.">
<meta property="og:image" content="{site}/assets/og/{slug}.png">
<meta property="article:published_time" content="2026-08-19T09:00:00Z">
<meta property="article:author" content="Christian Smith">
<meta name="card:summary" content="A post that clears the bar.">
</head>
<body>
<article><p>Body copy.</p></article>
</body>
</html>
"""

# Missing article:published_time and the <article> block: two required fields.
INCOMPLETE_POST = """<!doctype html>
<html lang="en">
<head>
<meta property="og:url" content="{site}/blog/{slug}/">
<meta property="og:title" content="{title}">
</head>
<body>
<p>Body copy with no article wrapper.</p>
</body>
</html>
"""


def _run(cmd, cwd):
    return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, check=True)


def _init_repo(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    _run(["git", "init", "-q", "-b", "main"], path)
    _run(["git", "config", "user.email", "test@example.invalid"], path)
    _run(["git", "config", "user.name", "Test Runner"], path)
    _run(["git", "config", "commit.gpgsign", "false"], path)


def _init_bare(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    _run(["git", "init", "-q", "--bare", "-b", "main"], path)


def write_post(blog: Path, slug: str, site: str, complete: bool = True, title: str = "") -> Path:
    tpl = COMPLETE_POST if complete else INCOMPLETE_POST
    post_dir = blog / "blog" / slug
    post_dir.mkdir(parents=True, exist_ok=True)
    path = post_dir / "index.html"
    path.write_text(tpl.format(site=site, slug=slug, title=title or slug), encoding="utf-8")
    return path


@pytest.fixture
def site(monkeypatch):
    """A stable fake origin for the live site."""
    url = "https://example.invalid"
    monkeypatch.setenv("SITE_URL", url)
    return url


@pytest.fixture
def blog(tmp_path, monkeypatch, site):
    """A git-backed blog repo with a bare remote, wired to BLOG_REPO."""
    repo = tmp_path / "blog-repo"
    remote = tmp_path / "blog-remote.git"
    _init_bare(remote)
    _init_repo(repo)
    (repo / "blog").mkdir()
    (repo / "README.md").write_text("fixture blog\n", encoding="utf-8")
    _run(["git", "add", "-A"], repo)
    _run(["git", "commit", "-qm", "init"], repo)
    _run(["git", "remote", "add", "origin", str(remote)], repo)
    _run(["git", "push", "-q", "-u", "origin", "main"], repo)
    monkeypatch.setenv("BLOG_REPO", str(repo))
    return repo


@pytest.fixture
def blog_remote(tmp_path):
    """The bare repo `blog` pushes to, for asserting a push actually landed."""
    return tmp_path / "blog-remote.git"


@pytest.fixture
def corpus(tmp_path, monkeypatch):
    """A git-backed corpus repo with a sources.json and a bare remote."""
    repo = tmp_path / "corpus-repo"
    remote = tmp_path / "corpus-remote.git"
    _init_bare(remote)
    _init_repo(repo)
    (repo / "sources.json").write_text(json.dumps({"sources": []}, indent=2) + "\n", encoding="utf-8")
    _run(["git", "add", "-A"], repo)
    _run(["git", "commit", "-qm", "init"], repo)
    _run(["git", "remote", "add", "origin", str(remote)], repo)
    _run(["git", "push", "-q", "-u", "origin", "main"], repo)
    monkeypatch.setenv("CORPUS_REPO", str(repo))
    return repo


@pytest.fixture
def srv():
    """The server module, imported after the env fixtures have run."""
    import server
    return server


@pytest.fixture
def fake_web(monkeypatch, srv):
    """Substitute the network at the urllib boundary.

    `routes` maps a URL to an int status or an Exception to raise. Anything not
    listed 404s, so a test that forgets to declare a URL fails loudly rather than
    silently passing against the real internet.
    """
    routes: dict[str, object] = {}

    class _Resp:
        def __init__(self, status):
            self.status = status

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def _urlopen(req, timeout=None):
        url = req if isinstance(req, str) else req.full_url
        outcome = routes.get(url, 404)
        if isinstance(outcome, Exception):
            raise outcome
        return _Resp(outcome)

    # A fake clock, so a poll that is supposed to time out does so instantly instead
    # of spinning for its real 180 seconds. sleep advances the clock; monotonic reads
    # it. Without this, no-op sleep turns every timeout path into a busy loop.
    clock = {"t": 0.0}

    def _sleep(seconds):
        clock["t"] += max(float(seconds), 1.0)

    monkeypatch.setattr(srv.urllib.request, "urlopen", _urlopen)
    monkeypatch.setattr(srv.time, "sleep", _sleep)
    monkeypatch.setattr(srv.time, "monotonic", lambda: clock["t"])
    return routes


def git_log(repo: Path) -> list[str]:
    out = subprocess.run(["git", "log", "--format=%s"], cwd=repo,
                         capture_output=True, text=True, check=True)
    return [l for l in out.stdout.splitlines() if l.strip()]


def porcelain(repo: Path) -> str:
    return subprocess.run(["git", "status", "--porcelain"], cwd=repo,
                          capture_output=True, text=True, check=True).stdout
