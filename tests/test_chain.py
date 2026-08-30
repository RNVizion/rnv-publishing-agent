"""The publish chain end to end, including the one place it is deliberately lenient."""
import json
import subprocess

from conftest import git_log, sitemap_xml, write_post


def _remote_log(remote):
    out = subprocess.run(["git", "log", "--format=%s", "main"], cwd=remote,
                         capture_output=True, text=True, check=True)
    return [l for l in out.stdout.splitlines() if l.strip()]


def test_full_publish_runs_every_step_in_order(blog, blog_remote, corpus, site, srv, fake_web):
    write_post(blog, "ready", site)
    fake_web[f"{site}/blog/ready/"] = 200
    fake_web[f"{site}/sitemap.xml"] = (200, sitemap_xml(site, "ready"))
    fake_web[f"{site}/assets/og/ready.png"] = 200

    result = srv.publish_post("ready", for_real=True)

    assert result["ok"] is True
    assert result["published"] is True
    assert [s["step"] for s in result["trace"]] == [
        "validate_post", "commit_and_push", "wait_for_live", "update_corpus"]
    assert "warnings" not in result


def test_a_real_publish_actually_pushes(blog, blog_remote, corpus, site, srv, fake_web):
    """Not a mock: the commit lands in the bare remote."""
    write_post(blog, "ready", site)
    fake_web[f"{site}/blog/ready/"] = 200
    fake_web[f"{site}/sitemap.xml"] = (200, sitemap_xml(site, "ready"))
    fake_web[f"{site}/assets/og/ready.png"] = 200

    srv.publish_post("ready", for_real=True)

    assert "Publish: ready" in _remote_log(blog_remote)
    sources = json.loads((corpus / "sources.json").read_text(encoding="utf-8"))
    assert {"id": "ready", "url": f"{site}/blog/ready/"} in sources["sources"]


def test_lagging_og_image_warns_but_does_not_fail(blog, blog_remote, corpus, site, srv, fake_web):
    """The deliberate leniency, pinned.

    The share image is built by a separate Action that can finish after the publish.
    A missing image must surface loudly and must not stop the chain — the post and
    the corpus are correct without it.
    """
    write_post(blog, "ready", site)
    fake_web[f"{site}/blog/ready/"] = 200
    fake_web[f"{site}/sitemap.xml"] = (200, sitemap_xml(site, "ready"))
    fake_web[f"{site}/assets/og/ready.png"] = 404

    result = srv.publish_post("ready", for_real=True)

    assert result["ok"] is True, "a lagging image must not fail the publish"
    assert result["published"] is True
    assert result["warnings"], "a lagging image must not pass silently either"
    assert any("og:image not live" in w for w in result["warnings"])


def test_page_that_never_goes_live_stops_the_chain(blog, blog_remote, corpus, site, srv, fake_web):
    """The page is the gate, unlike the image."""
    write_post(blog, "ready", site)
    fake_web[f"{site}/blog/ready/"] = 503

    result = srv.publish_post("ready", for_real=True)

    assert result["ok"] is False
    assert result["stopped_at"] == "wait_for_live"
    assert [s["step"] for s in result["trace"]] == [
        "validate_post", "commit_and_push", "wait_for_live"]
    sources = json.loads((corpus / "sources.json").read_text(encoding="utf-8"))
    assert sources["sources"] == [], "a post that never went live must not enter the corpus"


def test_corpus_refuses_a_dead_source(blog, corpus, site, srv, fake_web):
    write_post(blog, "ready", site)
    fake_web[f"{site}/blog/ready/"] = 404

    result = srv.update_corpus("ready")

    assert result["ok"] is False
    assert "not reachable" in result["error"] or "not registering" in result["error"]


def test_corpus_is_idempotent(blog, corpus, site, srv, fake_web):
    write_post(blog, "ready", site)
    fake_web[f"{site}/blog/ready/"] = 200

    first = srv.update_corpus("ready")
    second = srv.update_corpus("ready")

    assert first["added"] is True
    assert second["added"] is False
    assert second["reason"] == "already in sources.json"
    sources = json.loads((corpus / "sources.json").read_text(encoding="utf-8"))
    assert len(sources["sources"]) == 1


def test_commit_is_idempotent(blog, blog_remote, site, srv):
    write_post(blog, "ready", site)

    first = srv.commit_and_push("ready")
    before = git_log(blog)
    second = srv.commit_and_push("ready")

    assert first["committed"] is True
    assert second["ok"] is True
    assert second["committed"] is False
    assert "nothing to commit" in second["reason"]
    assert git_log(blog) == before


def test_commit_stages_only_the_post(blog, blog_remote, site, srv):
    """A publish writes one file. Anything else in the tree stays untouched."""
    write_post(blog, "ready", site)
    (blog / "unrelated.txt").write_text("do not commit me\n", encoding="utf-8")

    result = srv.commit_and_push("ready")

    assert result["files"] == ["blog/ready/index.html"]
    tracked = subprocess.run(["git", "ls-files"], cwd=blog, capture_output=True,
                             text=True, check=True).stdout
    assert "unrelated.txt" not in tracked


def test_list_posts_reads_slug_title_and_date(blog, site, srv):
    write_post(blog, "squish", site, title="Squish")
    write_post(blog, "margin", site, title="The Margin, Not the Price")

    posts = srv.list_posts()

    by_slug = {p["slug"]: p for p in posts}
    assert by_slug["squish"]["title"] == "Squish"
    assert by_slug["margin"]["title"] == "The Margin, Not the Price"
    assert by_slug["squish"]["published"] == "2026-08-19T09:00:00Z"


def test_lagging_sitemap_warns_but_does_not_fail(blog, blog_remote, corpus, site, srv, fake_web):
    """Wave two lagging is a warning, not a gate.

    The post page is live, so the post itself deployed. The sitemap has not caught up,
    which means build-feed has not regenerated the index and feed yet. That is another
    process running late, not this post being wrong, so it warns and continues.
    """
    write_post(blog, "ready", site)
    fake_web[f"{site}/blog/ready/"] = 200
    fake_web[f"{site}/sitemap.xml"] = (200, sitemap_xml(site, "some-older-post"))
    fake_web[f"{site}/assets/og/ready.png"] = 200

    result = srv.publish_post("ready", for_real=True)

    assert result["ok"] is True, "a lagging sitemap must not fail the publish"
    assert result["published"] is True
    assert any("does not list" in w for w in result["warnings"])


def test_sitemap_that_catches_up_produces_no_warning(blog, blog_remote, corpus, site, srv, fake_web):
    """The poll keeps looking, so a sitemap that lands in time is not reported late.

    This is the counterpart that makes the leniency leniency rather than blindness:
    if the check gave up after one look it could never tell 'late' from 'never'.
    """
    write_post(blog, "ready", site)
    fake_web[f"{site}/blog/ready/"] = 200
    fake_web[f"{site}/assets/og/ready.png"] = 200

    calls = {"n": 0}
    empty = sitemap_xml(site, "some-older-post")
    listed = sitemap_xml(site, "ready")

    class _Sitemap:
        """Answers without the post twice, then with it — build-feed finishing late."""
        def __init__(self):
            self.status = 200

        def read(self):
            calls["n"] += 1
            return (listed if calls["n"] > 2 else empty).encode("utf-8")

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    fake_web[f"{site}/sitemap.xml"] = 200  # declare the route so it is not a 404
    real_urlopen = srv.urllib.request.urlopen

    def _urlopen(req, timeout=None):
        url = req if isinstance(req, str) else req.full_url
        if url == f"{site}/sitemap.xml":
            return _Sitemap()
        return real_urlopen(req, timeout)

    srv.urllib.request.urlopen = _urlopen
    try:
        result = srv.publish_post("ready", for_real=True)
    finally:
        srv.urllib.request.urlopen = real_urlopen

    assert result["ok"] is True
    assert calls["n"] > 2, "the check must poll rather than look once"
    assert "warnings" not in result, f"sitemap caught up; nothing to warn about: {result.get('warnings')}"


def test_sitemap_unreachable_is_reported_not_swallowed(blog, blog_remote, corpus, site, srv, fake_web):
    write_post(blog, "ready", site)
    fake_web[f"{site}/blog/ready/"] = 200
    fake_web[f"{site}/sitemap.xml"] = 503
    fake_web[f"{site}/assets/og/ready.png"] = 200

    result = srv.publish_post("ready", for_real=True)

    live = next(s for s in result["trace"] if s["step"] == "wait_for_live")
    assert result["ok"] is True
    assert live["sitemap_listed"] is False
    assert live["sitemap_status"] == 503
