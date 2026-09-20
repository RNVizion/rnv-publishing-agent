"""validate_post asking the site project's library whether a post is shaped right.

The rule is the site project's and lives in its `scripts/post_shape.py`. This repo
imports it rather than carrying a copy, on that project's ruling of 2026-09-20 —
the rule's semantics moved twice in three days, and a copy would have reported
`ghost.png`, a file nobody ever wrote, while being wrong about the post's actual
defect.

So what is pinned here is THIS repo's handling of the library's answer: that it is
asked at all, asked with the document unmodified, gated on, re-read rather than
cached, and that every way the library can fail is reported as a library problem
instead of taking the publish down or being blamed on the post. The parser is not
tested here. It is tested where it lives, against its own vectors, and duplicating
that would rebuild the second implementation importing exists to avoid.
"""
import subprocess

import pytest

from conftest import plant_post_shape, sitemap_xml, write_post


RECORDING_STUB = '''\
import pathlib

SEEN = pathlib.Path(__file__).with_name("seen.txt")


def sibling_refs(html):
    SEEN.write_text(html, encoding="utf-8")
    return []


def shown_outside_code(html):
    return []
'''


def returning(refs, shown=()):
    """A library that answers with exactly these, whatever it is handed."""
    return (f"def sibling_refs(html):\n    return {list(refs)!r}\n\n\n"
            f"def shown_outside_code(html):\n    return {list(shown)!r}\n")


def _live(fake_web, site, slug):
    fake_web[f"{site}/blog/{slug}/"] = 200
    fake_web[f"{site}/sitemap.xml"] = (200, sitemap_xml(site, slug))
    fake_web[f"{site}/assets/og/{slug}.png"] = 200


def _subjects(remote):
    return subprocess.run(["git", "log", "--format=%s", "main"], cwd=remote,
                          capture_output=True, text=True, check=True).stdout


def test_a_sibling_reference_stops_the_chain_before_any_write(
        blog, blog_remote, corpus, site, srv, fake_web):
    """The gate. A post referencing something beside itself would go live pointing
    at a file the publish does not carry, so it never gets pushed."""
    write_post(blog, "ready", site)
    plant_post_shape(blog, returning(["diagram.png"]))
    _live(fake_web, site, "ready")

    result = srv.publish_post("ready", for_real=True)

    assert result["ok"] is False
    assert result["stopped_at"] == "validate_post"
    step = result["trace"][0]
    assert step["error"] == "sibling", "a shape defect must be its own kind, not a comment or config one"
    assert step["sibling_refs"] == ["diagram.png"]
    assert "Publish: ready" not in _subjects(blog_remote), "a misshapen post reached main"


def test_a_clean_post_publishes_and_says_nothing_about_shape(
        blog, blog_remote, corpus, site, srv, fake_web):
    """The silent half of the pair. The default plant answers "nothing beside it",
    so a clean post must carry no shape field and no warning at all."""
    write_post(blog, "ready", site)
    _live(fake_web, site, "ready")

    result = srv.publish_post("ready", for_real=True)

    assert result["ok"] is True, result
    assert "warnings" not in result
    assert "sibling_refs" not in result["trace"][0]


def test_the_library_is_handed_the_document_unmodified(blog, site, srv):
    """The library strips comments, cuts <code>/<pre> and neutralises escaped markup
    itself. Handing it something this repo had already normalised would be this repo
    deciding what the rule means, which is the whole thing importing avoids."""
    path = write_post(blog, "ready", site)
    path.write_text(path.read_text(encoding="utf-8")
                    + "\n<!-- a comment -->\n<pre>&lt;img src=\"shown.png\"&gt;</pre>\n",
                    encoding="utf-8")
    plant_post_shape(blog, RECORDING_STUB)

    srv.validate_post("ready")

    seen = (blog / "scripts" / "seen.txt").read_text(encoding="utf-8")
    assert seen == path.read_text(encoding="utf-8"), "the document was altered before the library saw it"


def test_the_wrapper_rule_is_deliberately_not_enforced_here(
        blog, blog_remote, corpus, site, srv, fake_web):
    """shown_outside_code() is the site project's to gate, and their build-feed run
    does. A wrapper mistake breaks nothing a publish carries, so a post that only
    breaks that rule still publishes from here. Skipping it is a decision, and this
    is the test that makes it one rather than an oversight."""
    write_post(blog, "ready", site)
    plant_post_shape(blog, returning([], shown=["&lt;article&gt;"]))
    _live(fake_web, site, "ready")

    result = srv.publish_post("ready", for_real=True)

    assert result["ok"] is True, result
    assert "Publish: ready" in _subjects(blog_remote)


def test_a_missing_library_is_a_config_error_not_a_post_error(blog, site, srv):
    """A checkout predating 2026-09-20 has no library. The post may be perfect and
    we could not ask, so it reports as config and names the path — reporting ok
    would be the green result that did not look, and reporting a post defect would
    send the author to fix a file that is fine."""
    write_post(blog, "ready", site)
    (blog / "scripts" / "post_shape.py").unlink()

    result = srv.validate_post("ready")

    assert result["ok"] is False
    assert result["error"] == "config"
    assert any("post_shape.py" in p for p in result["problems"])
    assert "missing_required" not in result, "a library problem was dressed as a post problem"


def test_a_library_that_exits_on_import_does_not_take_the_publish_with_it(
        blog, blog_remote, corpus, site, srv, fake_web):
    """The sharp one. sys.exit() during import raises SystemExit, which is not an
    Exception and would end the publish with no result and no explanation.

    Not hypothetical: importing the site project's tests/test_post_shape.py did
    exactly this, and it exited over a defect in a DIFFERENT post than the one being
    published. Their library is written not to and their CI pins it in a subprocess;
    this is the half that does not depend on their CI having run.
    """
    write_post(blog, "ready", site)
    plant_post_shape(blog, "import sys\nsys.exit(1)\n")
    _live(fake_web, site, "ready")

    result = srv.publish_post("ready", for_real=True)

    assert result["ok"] is False
    assert result["stopped_at"] == "validate_post"
    assert result["trace"][0]["error"] == "config"
    assert any("raised while being imported" in p for p in result["trace"][0]["problems"])
    assert "Publish: ready" not in _subjects(blog_remote)


def test_a_library_that_raises_on_import_is_reported_not_swallowed(blog, site, srv):
    write_post(blog, "ready", site)
    plant_post_shape(blog, "raise ValueError('half-written')\n")

    result = srv.validate_post("ready")

    assert result["error"] == "config"
    assert any("half-written" in p for p in result["problems"])


@pytest.mark.parametrize("body, gone", [
    ("def shown_outside_code(html):\n    return []\n", "sibling_refs"),
    ("def sibling_refs(html):\n    return []\n", "shown_outside_code"),
])
def test_a_moved_public_interface_is_named(blog, site, srv, body, gone):
    """The library declares two public functions and that renaming either breaks
    this publish, deliberately: a loud break beats a silent divergence. Both are
    checked, including the one this repo does not call — it is part of the declared
    interface, and finding out it moved is worth more than not having needed it."""
    write_post(blog, "ready", site)
    plant_post_shape(blog, body)

    result = srv.validate_post("ready")

    assert result["error"] == "config"
    assert any(gone in p for p in result["problems"])


def test_the_library_is_re_read_on_every_call(blog, site, srv):
    """The operator pulls the site checkout between publishes. A module cached at
    first use would keep asserting the rule as it stood when the server started,
    which is the stale-cache failure this repo has now fixed twice elsewhere."""
    write_post(blog, "ready", site)

    assert srv.validate_post("ready")["ok"] is True

    plant_post_shape(blog, returning(["appeared-after-a-pull.png"]))

    second = srv.validate_post("ready")
    assert second["ok"] is False
    assert second["sibling_refs"] == ["appeared-after-a-pull.png"]


def test_importing_the_library_leaves_nothing_in_the_site_checkout(
        blog, blog_remote, corpus, site, srv, fake_web):
    """Python caches bytecode beside the source, so importing this would write
    scripts/__pycache__/ into the operator's repo. A publish is not entitled to
    create files in somebody else's checkout, even ignored ones. Found by a test
    asserting the checkout was clean after a publish, which it then was not."""
    write_post(blog, "ready", site)
    _live(fake_web, site, "ready")

    assert srv.publish_post("ready", for_real=True)["ok"] is True

    assert not list(blog.rglob("__pycache__")), "the import left bytecode behind"
    dirty = subprocess.run(["git", "status", "--porcelain"], cwd=blog,
                           capture_output=True, text=True, check=True).stdout
    assert dirty == "", f"the checkout was left dirty: {dirty!r}"
