"""Parsing edge cases the server's own docstrings claim to handle.

Each of these pins a stated behaviour. They are cheap, and they are the tests that
catch a regex "improvement" six months from now.
"""
from conftest import write_post


def test_apostrophe_in_meta_content_survives(blog, site, srv):
    """The quote backreference exists so a title with an apostrophe still parses."""
    post = write_post(blog, "lazy", site)
    post.write_text(post.read_text(encoding="utf-8").replace(
        'content="lazy"', 'content="Lazy in the Right Way Is Leverage\'s Point"'),
        encoding="utf-8")

    posts = {p["slug"]: p for p in srv.list_posts()}

    assert posts["lazy"]["title"] == "Lazy in the Right Way Is Leverage's Point"


def test_single_quoted_meta_parses(blog, site, srv):
    post = write_post(blog, "single", site)
    post.write_text(post.read_text(encoding="utf-8").replace(
        '<meta property="og:title" content="single">',
        "<meta property='og:title' content='Single Quoted'>"),
        encoding="utf-8")

    posts = {p["slug"]: p for p in srv.list_posts()}

    assert posts["single"]["title"] == "Single Quoted"


def test_commented_out_metadata_does_not_count(blog, site, srv):
    """A field inside an HTML comment is not present, however much it looks it."""
    post = write_post(blog, "commented", site, complete=False)
    post.write_text(post.read_text(encoding="utf-8").replace(
        "</head>",
        '<!-- <meta property="article:published_time" content="2026-01-01T00:00:00Z"> -->\n</head>'),
        encoding="utf-8")

    result = srv.validate_post("commented")

    assert result["ok"] is False
    assert "article:published_time" in result["missing_required"]


def test_commented_out_article_block_does_not_count(blog, site, srv):
    post = write_post(blog, "fake-article", site, complete=False)
    post.write_text(post.read_text(encoding="utf-8").replace(
        "</body>", "<!-- <article><p>not real</p></article> -->\n</body>"),
        encoding="utf-8")

    result = srv.validate_post("fake-article")

    assert "<article> block" in result["missing_required"]


def test_missing_post_reports_the_path_not_a_crash(blog, site, srv):
    result = srv.validate_post("never-written")

    assert result["ok"] is False
    assert "no index.html" in result["error"]


def test_site_url_trailing_slash_does_not_double(monkeypatch, blog, srv, fake_web):
    """SITE_URL with a trailing slash must not produce a // in the polled URL."""
    monkeypatch.setenv("SITE_URL", "https://example.invalid/")
    write_post(blog, "ready", "https://example.invalid")
    fake_web["https://example.invalid/blog/ready/"] = 200

    result = srv.wait_for_live("ready", timeout=1, interval=0, og_timeout=0)

    assert result["url"] == "https://example.invalid/blog/ready/"
    assert "//blog" not in result["url"]
