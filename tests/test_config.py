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
from conftest import plant_post_shape, write_post


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


# ==========================================================================
# The gate checks what the run will actually need, on BOTH paths.
#
# Until 2026-09-22 config_report checked that each path resolved to a DIRECTORY,
# when both stages fetch from and push to a git repo with an `origin`. It read as
# closed while checking the cheaper half of its own precondition — the same shape
# as the defect its own line-63 comment was written about, one layer up.
#
# The gap opened because the gate predates 2026-09-18, when update_corpus began
# pushing rather than reading. A gate encodes an assumption about a neighbouring
# system, and when that system's contract strengthens the gate is silently wrong
# with nothing in either system pointing at it.

def plain_dir(tmp_path, name, *children):
    d = tmp_path / name
    for c in children:
        (d / c).mkdir(parents=True, exist_ok=True)
    d.mkdir(parents=True, exist_ok=True)
    return d


def test_a_corpus_that_is_not_a_repo_stops_a_real_run(srv, blog, monkeypatch, tmp_path):
    """The reproduction. This one is the expensive failure: the gate cleared it,
    the post went live, and stage 5 died on `fatal: not a git repository`."""
    monkeypatch.setenv("CORPUS_REPO", str(plain_dir(tmp_path, "corpus-plain")))

    report = srv.config_report(for_real=True)

    assert report["fatal"] is True, report
    assert any("CORPUS_REPO" in p and "origin" in p for p in report["problems"]), report


def test_a_site_repo_that_is_not_a_repo_stops_a_real_run(srv, corpus, monkeypatch, tmp_path):
    """The cheaper failure, and it gets the same depth. It dies at
    commit_and_push with nothing published — but you ration a check that costs
    something, and this one is a local git call. The cost asymmetry decides where
    the EXPENSIVE check lives, not whether the cheap one runs on both paths."""
    monkeypatch.setenv("BLOG_REPO", str(plain_dir(tmp_path, "site-plain", "blog")))

    report = srv.config_report(for_real=True)

    assert report["fatal"] is True, report
    assert any("BLOG_REPO" in p and "origin" in p for p in report["problems"]), report


def test_a_repo_with_no_origin_stops_a_real_run(srv, blog, monkeypatch, tmp_path):
    """"Is it a git repo" would still have been too cheap: the chain fetches from
    and pushes to `origin`, and a repo without one clears every weaker check."""
    lonely = tmp_path / "corpus-no-origin"
    lonely.mkdir()
    for cmd in (["git", "init", "-q", "-b", "main", "."],
                ["git", "config", "user.email", "t@t"], ["git", "config", "user.name", "t"]):
        subprocess.run(cmd, cwd=lonely, check=True, capture_output=True)
    (lonely / "sources.json").write_text('{"sources": []}\n', encoding="utf-8")
    monkeypatch.setenv("CORPUS_REPO", str(lonely))

    report = srv.config_report(for_real=True)

    assert report["fatal"] is True, report
    assert any("origin" in p for p in report["problems"]), report


def test_a_dry_run_still_validates_a_post_in_a_plain_folder(srv, monkeypatch, tmp_path, site):
    """The decision that shaped the fix, pinned. A dry run reads the post off the
    filesystem and never touches git, so it can honestly do its job without a
    repo — the dry/real test is whether the run can do its work without the
    value, not whether the value matters. Putting the git check in _path_problem
    would set blog_fatal and stop validate_post from validating a readable post."""
    folder = plain_dir(tmp_path, "site-plain", "blog")
    plant_post_shape(folder)
    write_post(folder, "ready", site)
    monkeypatch.setenv("BLOG_REPO", str(folder))

    assert srv.config_report(for_real=False)["blog_fatal"] is False
    assert srv.validate_post("ready")["ok"] is True, "a readable post stopped validating"


def test_a_missing_path_is_not_reported_as_a_missing_repo(srv, corpus, monkeypatch, tmp_path):
    """No misattribution. Running git inside a directory that is not there reports
    'not a git repository' about a path whose real problem is that it does not
    exist — a config defect blamed on a different config defect, which is this
    module's founding bug in miniature."""
    monkeypatch.setenv("BLOG_REPO", str(tmp_path / "nowhere-at-all"))

    report = srv.config_report(for_real=True)

    said = " ".join(report["problems"])
    assert "no directory at" in said, report
    assert "origin" not in said, f"it also blamed the missing path for having no remote: {report}"


def test_two_healthy_repos_say_nothing(srv, blog, corpus):
    """The silent half. A check that never goes quiet stops being read."""
    report = srv.config_report(for_real=True)
    assert report == {**report, "ok": True, "fatal": False, "problems": [], "warnings": []}


def test_the_message_carries_gits_own_words_and_the_provenance(srv, blog, monkeypatch, tmp_path):
    """It does not classify. `git remote get-url origin` fails for both causes and
    this reports what git said; a caption that names a cause can name the wrong
    one, and that defect has been fixed twice in this repo already."""
    monkeypatch.setenv("CORPUS_REPO", str(plain_dir(tmp_path, "corpus-plain")))

    problem = next(p for p in srv.config_report(for_real=True)["problems"]
                   if "CORPUS_REPO" in p)

    assert "not a git repository" in problem, problem
    assert "source: environment" in problem, "the provenance half of the diagnosis is missing"
