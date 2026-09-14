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

import rnv_config
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


def test_an_unresolved_path_reports_where_it_looked(monkeypatch, tmp_path):
    """The message must name its provenance, not just the path.

    Superseded the older "says it was unset" assertion: with sibling discovery,
    unset is no longer a cause at all. It is a legitimate source, and the useful
    diagnosis is WHICH source produced the path that failed.
    """
    monkeypatch.delenv("BLOG_REPO", raising=False)
    monkeypatch.setattr(rnv_config, "WORKSPACE_ROOT", tmp_path / "empty-workspace")
    r = server.config_report()

    assert r["fatal"] is True
    assert any("source:" in p for p in r["problems"]), r["problems"]
    assert any("sibling" in p for p in r["problems"]), \
        "it must say it went looking beside the repo"


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


# --- the resolution chain: environment -> .env -> sibling ------------------
#
# Four rungs, tested in the order they are tried. The one that matters most is
# the first: an explicit variable must beat a discovered sibling, because that is
# the escape hatch for any layout the sibling rule does not fit, and an escape
# hatch nothing tests is an escape hatch that quietly stops working.

def test_environment_beats_dotenv_and_sibling(monkeypatch, tmp_path):
    """Rung 1. The escape hatch. Whatever else is true, an explicit value wins."""
    wanted = tmp_path / "explicit"
    (wanted / "blog").mkdir(parents=True)
    dotenv = tmp_path / "a.env"
    dotenv.write_text(f'BLOG_REPO="{tmp_path / "from-dotenv"}"\n', encoding="utf-8")

    monkeypatch.setattr(rnv_config, "DOTENV_PATH", dotenv)
    monkeypatch.setenv("BLOG_REPO", str(wanted))

    path, how = rnv_config.resolve_path("BLOG_REPO")
    assert path == wanted
    assert how == "environment"


def test_dotenv_is_used_when_the_environment_is_silent(monkeypatch, tmp_path):
    """Rung 2. Per-machine config without a shell ritual in every new terminal."""
    wanted = tmp_path / "from-dotenv"
    dotenv = tmp_path / "a.env"
    dotenv.write_text(f'BLOG_REPO="{wanted}"\n', encoding="utf-8")

    monkeypatch.setattr(rnv_config, "DOTENV_PATH", dotenv)
    monkeypatch.delenv("BLOG_REPO", raising=False)

    path, how = rnv_config.resolve_path("BLOG_REPO")
    assert path == wanted
    assert ".env" in how


def test_a_sibling_checkout_is_found_with_no_configuration_at_all(monkeypatch, tmp_path):
    """Rung 3, and the reason a fresh machine needs no setup.

    Whatever folder holds the agent repo also holds the site repo. That is true in
    a Codespace (/workspaces), on a laptop (~/rnv), and on a Desktop layout. The
    absolute path differs; the relationship does not.
    """
    workspace = tmp_path / "anywhere-at-all"
    (workspace / "rnvizion.github.io" / "blog").mkdir(parents=True)
    (workspace / "rnv-ask-the-corpus").mkdir(parents=True)

    monkeypatch.setattr(rnv_config, "WORKSPACE_ROOT", workspace)
    monkeypatch.delenv("BLOG_REPO", raising=False)
    monkeypatch.delenv("CORPUS_REPO", raising=False)

    blog, how = rnv_config.resolve_path("BLOG_REPO")
    corpus, _ = rnv_config.resolve_path("CORPUS_REPO")

    assert blog == workspace / "rnvizion.github.io"
    assert corpus == workspace / "rnv-ask-the-corpus"
    assert "sibling" in how


def test_a_relative_value_resolves_against_the_agent_repo_not_the_cwd(monkeypatch, tmp_path):
    """A .env holding `../my-site` must mean the same thing from any directory.

    Same lesson as agent.py resolving server.py from __file__: resolving against
    the working directory makes a value that works from one folder and nowhere else.
    """
    monkeypatch.setattr(rnv_config, "AGENT_ROOT", tmp_path / "agent")
    monkeypatch.setenv("BLOG_REPO", "../site")

    path, how = rnv_config.resolve_path("BLOG_REPO")
    assert path == (tmp_path / "site")
    assert "relative to the agent repo" in how


# --- the .env parser -------------------------------------------------------

def test_dotenv_parser_keeps_windows_paths_with_spaces_intact():
    """The case a naive split would truncate, and the reason values are quoted."""
    got = rnv_config.parse_dotenv('BLOG_REPO="C:/Users/John Smith/rnv/rnvizion.github.io"\n')
    assert got["BLOG_REPO"] == "C:/Users/John Smith/rnv/rnvizion.github.io"


def test_dotenv_parser_handles_the_shapes_people_actually_write():
    text = (
        "# a comment\n"
        "\n"
        "export SITE_URL=https://rnvizion.dev   # trailing comment\n"
        "CORPUS_REPO='/single/quoted'\n"
        "  BLOG_REPO = /padded/with/spaces \n"
        "EMPTY=\n"
        "not a valid line\n"
    )
    got = rnv_config.parse_dotenv(text)

    assert got["SITE_URL"] == "https://rnvizion.dev"
    assert got["CORPUS_REPO"] == "/single/quoted"
    assert got["BLOG_REPO"] == "/padded/with/spaces"
    assert "EMPTY" not in got, "an empty value is not a value"


def test_config_report_carries_the_resolution_and_its_provenance(blog, corpus, site):
    """A report that says what failed but not where the value came from is half a
    diagnosis. Every run carries the full resolution so the next question is
    already answered."""
    r = server.config_report()

    assert set(r["resolved"]) == {"BLOG_REPO", "CORPUS_REPO", "SITE_URL"}
    assert all("from" in v for v in r["resolved"].values())
