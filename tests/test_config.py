"""Configuration errors, and where they are allowed to surface.

The bug these pin: an unset BLOG_REPO used to make validate_post report "no
index.html for <slug>" about a post that existed. A configuration defect wore a
post defect's face, and a wrong assertion is worse than an absent one because it
is trusted downstream.

Two separate rules are under test, and they are easy to conflate:

  WHAT IS FATAL  — BLOG_REPO in both modes, because a dry run that cannot read
                   the post has validated nothing and must not report ok.
                   CORPUS_REPO and SITE_URL only for a real run, because a dry
                   run genuinely does not need them.

  WHEN IT IS CHECKED — all three before commit_and_push. The test that earns its
                   keep is the last one here: a real publish with a broken
                   CORPUS_REPO must stop with the git log unchanged. Before this
                   change it committed, pushed, went live, and only then failed.
"""

import subprocess

import pytest

import server
from conftest import write_post


def log(repo):
    out = subprocess.run(["git", "log", "--format=%s"], cwd=repo,
                         capture_output=True, text=True).stdout
    return [l for l in out.splitlines() if l]


# --- what is fatal ---------------------------------------------------------

def test_unresolvable_blog_repo_is_a_config_error_not_a_post_error(blog, site, monkeypatch, tmp_path):
    """The original bug. The post exists on disk; BLOG_REPO points elsewhere."""
    write_post(blog, "ready", site)
    monkeypatch.setenv("BLOG_REPO", str(tmp_path / "nope"))
    r = server.validate_post("ready")

    assert r["ok"] is False
    assert r["error"] == "config", "a bad path must not be reported as a bad post"
    assert "missing_required" not in r, "must not claim anything about the post's fields"
    assert any("BLOG_REPO" in p for p in r["problems"])


def test_a_real_missing_post_still_reports_as_a_post_error(blog, corpus):
    """The other half of the distinction: when we CAN look and the post isn't there."""
    r = server.validate_post("no-such-post")

    assert r["ok"] is False
    assert r.get("error") != "config", "a genuinely missing post is not a config error"


def test_blog_repo_pointing_at_a_folder_without_blog_is_caught(monkeypatch, tmp_path):
    """A plausible mistake: pointing BLOG_REPO one level too deep or too shallow."""
    (tmp_path / "empty").mkdir()
    monkeypatch.setenv("BLOG_REPO", str(tmp_path / "empty"))
    r = server.config_report()

    assert r["fatal"] is True
    assert any("blog/" in p for p in r["problems"])


def test_unset_blog_repo_says_it_was_unset(monkeypatch):
    """The message must name the cause, not just the symptom."""
    monkeypatch.delenv("BLOG_REPO", raising=False)
    r = server.config_report()

    assert r["fatal"] is True
    assert any("unset" in p for p in r["problems"]), \
        "an unset variable falling back to a Codespace default must say so"


# --- dry run vs real run ---------------------------------------------------

def test_broken_corpus_repo_only_warns_on_a_dry_run(blog, site, monkeypatch, tmp_path):
    """A dry run does not need CORPUS_REPO, so it completes and says so."""
    write_post(blog, "ready", site)
    monkeypatch.setenv("CORPUS_REPO", str(tmp_path / "nope"))
    r = server.publish_post("ready")

    assert r["ok"] is True, "a dry run can do its whole job without the corpus"
    assert r["dry_run"] is True
    assert any("CORPUS_REPO" in w for w in r["warnings"]), \
        "but it must warn that a real publish would fail"


def test_broken_corpus_repo_is_fatal_for_a_real_publish(blog, site, monkeypatch, tmp_path):
    write_post(blog, "ready", site)
    monkeypatch.setenv("CORPUS_REPO", str(tmp_path / "nope"))
    r = server.publish_post("ready", for_real=True)

    assert r["ok"] is False
    assert r["stopped_at"] == "config"


def test_the_same_config_is_a_warning_then_an_error_across_the_two_modes(blog, monkeypatch, tmp_path):
    """Pins the asymmetry itself, so neither half can drift alone."""
    monkeypatch.setenv("CORPUS_REPO", str(tmp_path / "nope"))

    assert server.config_report(for_real=False)["fatal"] is False
    assert server.config_report(for_real=True)["fatal"] is True


# --- when it is checked: the one that matters ------------------------------

def test_a_doomed_real_publish_stops_before_anything_is_committed(blog, blog_remote, corpus,
                                                                  site, srv, monkeypatch, tmp_path):
    """The test this whole change exists for.

    CORPUS_REPO is only read by stage 5. Before this change, a real publish with a
    broken corpus path committed, pushed, waited for the page to go live, and only
    then failed — leaving a published post and a trace that said the publish
    failed. The irreversible work happened for a run that could never succeed.
    """
    write_post(blog, "ready", site)
    subprocess.run(["git", "add", "-A"], cwd=blog, capture_output=True)
    subprocess.run(["git", "commit", "-qm", "add post"], cwd=blog, capture_output=True)
    monkeypatch.setenv("CORPUS_REPO", str(tmp_path / "nope"))
    before = log(blog)

    r = server.publish_post("ready", for_real=True,
                            timeout=5, interval=1, og_timeout=2, sitemap_timeout=2)

    assert r["ok"] is False
    assert r["stopped_at"] == "config"
    assert log(blog) == before, "a knowably doomed run must not reach the commit"
    assert not any(s["step"] == "commit_and_push" for s in r["trace"]), \
        "no write step may appear in the trace"


def test_config_report_lists_every_problem_at_once(monkeypatch, tmp_path):
    """One call, one round trip. Reporting one problem per run wastes a cycle each."""
    monkeypatch.setenv("BLOG_REPO", str(tmp_path / "nope"))
    monkeypatch.setenv("CORPUS_REPO", str(tmp_path / "also-nope"))
    monkeypatch.setenv("SITE_URL", "not-a-url")

    r = server.config_report(for_real=True)

    assert len(r["problems"]) == 3, r["problems"]


# --- the happy path stays happy --------------------------------------------

def test_a_good_config_is_silent(blog, corpus, site):
    r = server.config_report(for_real=True)

    assert r["ok"] is True
    assert r["fatal"] is False and r["blog_fatal"] is False
    assert r["problems"] == [] and r["warnings"] == [], \
        "a correct setup must produce no noise in either list"


# --- ordering: a config problem must not mask a post problem ---------------

def test_a_bad_post_still_reports_as_a_bad_post_when_the_corpus_path_is_also_wrong(
        blog, site, monkeypatch, tmp_path):
    """Regression guard for the ordering.

    When BLOG_REPO is fine, validation is trustworthy, so a half-written post must
    report as a half-written post even though CORPUS_REPO is also broken. An
    earlier draft of the config gate ran before validation and masked this, which
    the existing refusal test caught. A config problem may only pre-empt a post
    problem when it actually prevented us from seeing the post.
    """
    write_post(blog, "half-written", site, complete=False)
    monkeypatch.setenv("CORPUS_REPO", str(tmp_path / "nope"))

    r = server.publish_post("half-written", for_real=True)

    assert r["ok"] is False
    assert r["stopped_at"] == "validate_post", \
        "the post's own defect is the more relevant failure and comes first"


def test_publish_post_reports_a_bad_blog_repo_as_a_config_stop(blog, site, monkeypatch, tmp_path):
    """Pins the gate's POSITION, not just that the error is reported somewhere.

    validate_post carries its own config guard, so without this test the gate can
    be deleted from publish_post and nothing notices: the failure still surfaces,
    but as stopped_at="validate_post", which misfiles a configuration defect as a
    post defect at the chain level. Found by sabotage; the earlier draft of this
    file missed it.
    """
    write_post(blog, "ready", site)
    monkeypatch.setenv("BLOG_REPO", str(tmp_path / "nope"))

    r = server.publish_post("ready", for_real=True)

    assert r["ok"] is False
    assert r["stopped_at"] == "config", "the chain must file this as a config stop"
    assert r["error"] == "config"
