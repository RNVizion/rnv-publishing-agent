"""The comment allowlist, owned by the site project's post-template.RULES.md.

Neither the template nor a post carries a comment that explains or instructs. Four
structural markers stay, because they navigate rather than explain. This repo asserts
against that contract; it does not define it.

Why these tests exist rather than trusting the template: posts are published by
copying the template, and a hand-edit is exactly where a note gets pasted back in.
The contract is clean by construction until someone edits one file by hand, which is
the moment a guard is for.
"""

import server
from conftest import write_post

# Transcribed by hand from the site project's _templates/post-template.RULES.md.
# DELIBERATELY NOT imported from server.ALLOWED_COMMENTS: a test that reads its
# expectations out of the thing it is testing cannot detect a change to that thing.
# An earlier draft looped over server.ALLOWED_COMMENTS, and deleting a marker from
# the source made the loop quietly stop testing it — a sabotage that passed. This
# list is the independent second copy that makes the comparison mean something.
CONTRACT_MARKERS = frozenset({
    "<!-- Open Graph -->",
    "<!-- Fonts -->",
    "<!-- Blog-index card teaser. -->",
    "<!-- Standing author bio. -->",
})


def add_comment(blog, slug, comment):
    p = blog / "blog" / slug / "index.html"
    p.write_text(p.read_text(encoding="utf-8").replace("<article", f"{comment}\n<article"),
                 encoding="utf-8")


def test_a_clean_post_passes(blog, site):
    write_post(blog, "clean", site)
    r = server.validate_post("clean")

    assert r["ok"] is True
    assert "stray_comments" not in r


def test_an_explanatory_comment_fails_the_post(blog, site):
    """The real incident: a template note rode into a published post and then into
    the feed's content:encoded, reaching dev.to."""
    write_post(blog, "leaky", site)
    add_comment(blog, "leaky", "<!-- First paragraph gets the drop-cap automatically. -->")

    r = server.validate_post("leaky")

    assert r["ok"] is False
    assert r["error"] == "comment"
    assert any("drop-cap" in c for c in r["stray_comments"])


def test_the_allowlist_matches_the_site_contract_exactly():
    """The list is the site project's, and drift in either direction is a defect:
    a dropped marker fails good posts, an added one lets an instruction through."""
    assert server.ALLOWED_COMMENTS == CONTRACT_MARKERS, (
        "allowlist has drifted from post-template.RULES.md; if the site project "
        "changed the contract, update both this test and server.ALLOWED_COMMENTS"
    )


def test_each_allowlisted_marker_is_accepted(blog, site):
    """All four, individually, so no single marker can fall off the list unnoticed."""
    for i, marker in enumerate(sorted(CONTRACT_MARKERS)):
        slug = f"marker-{i}"
        write_post(blog, slug, site)
        add_comment(blog, slug, marker)
        assert server.validate_post(slug)["ok"] is True, f"{marker} should be allowed"


def test_the_check_is_an_allowlist_not_a_length_rule(blog, site):
    """RULES.md is explicit that this is not about brevity: 'short comments stay' is
    a heuristic, and the first four-word instruction would defeat it silently."""
    write_post(blog, "terse", site)
    add_comment(blog, "terse", "<!-- keep in sync -->")

    r = server.validate_post("terse")

    assert r["ok"] is False, "a four-word instruction is still an instruction"


def test_a_near_miss_marker_is_not_allowed(blog, site):
    """Checked exactly. A marker that drifts in wording is a new comment, not the
    allowlisted one, and the drift is what the exact check is for."""
    write_post(blog, "drifted", site)
    add_comment(blog, "drifted", "<!-- Open Graph tags -->")

    assert server.validate_post("drifted")["ok"] is False


def test_a_multiline_comment_is_caught(blog, site):
    """The template's own block comment was multiline; a regex that stops at the
    first newline would miss exactly the case that caused this rule."""
    write_post(blog, "block", site)
    add_comment(blog, "block", "<!--\n  Copy this file to blog/<slug>/index.html\n  and fill the placeholders.\n-->")

    r = server.validate_post("block")

    assert r["ok"] is False
    assert len(r["stray_comments"]) == 1


def test_a_stray_comment_stops_the_chain_before_any_write(blog, blog_remote, corpus,
                                                          site, srv):
    """It gates rather than warns, under principle 15: waiting never removes a
    comment, so it is the post's defect. And it is effectively irreversible once
    shipped — the comment lands in public git history and in the feed body."""
    write_post(blog, "leaky", site)
    add_comment(blog, "leaky", "<!-- TODO: fix the ratio before shipping -->")

    r = server.publish_post("leaky", for_real=True)

    assert r["ok"] is False
    assert r["stopped_at"] == "validate_post"
    assert not any(s["step"] == "commit_and_push" for s in r["trace"])
