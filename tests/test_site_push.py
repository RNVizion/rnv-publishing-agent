"""Publishing a post to a main that the site's own Actions also move.

build-feed and build-og commit on top of every publish, so this checkout is behind
seconds after each one: being behind is its resting state, not an anomaly. These
tests put those commits on the remote the way the Actions do, from a clone that is
not the chain's, and pin that commit_and_push decides against main rather than
against its local branch.

The first three are the defects reproduced on 2026-09-18 against the previous code,
which decided from the local branch and reconciled by rebase:
  - a refused push left the commit on the local branch; the re-run saw the post
    matching it, reported "nothing to commit" and published: true, and main never
    received the edit
  - a catch-up rebase that emptied the commit still reported pushed
  - the push carried every unpushed commit on the branch, not only the post's
"""
import subprocess

import pytest

from conftest import sitemap_xml, write_post


def _git(cwd, *args) -> str:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True,
                          check=True).stdout


def _an_action_pushes(remote, work, message="chore: rebuild feed, index, sitemap and robots [skip ci]",
                      post=None, body=None) -> None:
    """What build-feed and build-og do: commit generated output on top of main.

    With `post` and `body` it instead lands a post file, which is how another writer
    publishing the same slug is modelled.
    """
    subprocess.run(["git", "clone", "-q", str(remote), str(work)],
                   check=True, capture_output=True)
    for key, value in (("user.email", "action@example.invalid"),
                       ("user.name", "Workflow"),
                       ("commit.gpgsign", "false")):
        _git(work, "config", key, value)
    if post:
        target = work / "blog" / post / "index.html"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body, encoding="utf-8")
    else:
        (work / "generated.txt").write_text(message + "\n", encoding="utf-8")
    _git(work, "add", "-A")
    _git(work, "commit", "-qm", message)
    _git(work, "push", "-q", "origin", "main")


def _refuse_pushes(remote) -> None:
    hook = remote / "hooks" / "pre-receive"
    hook.write_text("#!/bin/sh\necho 'refused by the test hook' >&2\nexit 1\n", encoding="utf-8")
    hook.chmod(0o755)


def _allow_pushes(remote) -> None:
    (remote / "hooks" / "pre-receive").unlink()


def _move_main_after_fetch(monkeypatch, srv, move, times=1) -> list:
    """Call move(n) right after commit_and_push's nth fetch, up to `times` times:
    main moves between the look and the push. Returns the fetches it fired on."""
    real = srv._site_git
    fired = []

    def wrapped(*args, **kwargs):
        result = real(*args, **kwargs)
        if args and args[0] == "fetch" and len(fired) < times:
            fired.append(args)
            move(len(fired))
        return result

    monkeypatch.setattr(srv, "_site_git", wrapped)
    return fired


def _live(fake_web, site, slug) -> None:
    fake_web[f"{site}/blog/{slug}/"] = 200
    fake_web[f"{site}/sitemap.xml"] = (200, sitemap_xml(site, slug))
    fake_web[f"{site}/assets/og/{slug}.png"] = 200


def _subjects_on_main(remote) -> list:
    return _git(remote, "log", "--format=%s", "main").splitlines()


def _post_on_main(remote, slug) -> str:
    return _git(remote, "show", f"main:blog/{slug}/index.html")


def _push_step(result) -> dict:
    return next(s for s in result["trace"] if s["step"] == "commit_and_push")


def test_an_edit_to_a_live_post_reaches_main_after_a_refusal(
        blog, blog_remote, corpus, site, srv, fake_web):
    """The reproduction of record. Publish, edit the live post, have the push
    refused, then re-run once the remote accepts again.

    Previously: the refused commit stayed on the local branch, so the re-run found
    the post matching it, reported "nothing to commit" and published: true, and the
    edit never reached main. The chain went green on a publish that did not happen.

    This one does not answer to a single sabotage, and that is the finding rather
    than a gap. The defect needed two causes at once — the decision read from the
    branch, and the refused commit left on it — and the fix closes both, so either
    guard alone is enough to keep this green. Restoring one at a time leaves it
    passing; restoring both (the 2026-09-18 sweep's S1 with S14) turns it red, as
    does the pre-fix server.py, which failed here on "the re-run reported nothing to
    do". Tests either side of this one are armed by the single sabotages.
    """
    write_post(blog, "ready", site)
    _live(fake_web, site, "ready")
    assert srv.publish_post("ready", for_real=True)["ok"] is True
    assert "Ready, edited" not in _post_on_main(blog_remote, "ready")

    write_post(blog, "ready", site, title="Ready, edited")
    _refuse_pushes(blog_remote)
    refused = srv.publish_post("ready", for_real=True)

    assert refused["ok"] is False
    assert refused["stopped_at"] == "commit_and_push"
    assert _push_step(refused).get("pushed") is False
    # What the refusal left behind is test_a_refused_push_leaves_nothing_behind's
    # job. Nothing is asserted about it here, so this test runs all the way to the
    # claim in its name rather than stopping at the first symptom of the same cause.

    _allow_pushes(blog_remote)
    rerun = srv.publish_post("ready", for_real=True)

    assert rerun["ok"] is True, rerun
    assert _push_step(rerun)["committed"] is True, "the re-run reported nothing to do"
    assert "Ready, edited" in _post_on_main(blog_remote, "ready"), "the edit never reached main"


def test_a_refused_push_leaves_nothing_behind(blog, blog_remote, site, srv):
    """Nothing may be left on the local branch for a later run to mistake for a
    published post, and the error must not blame access."""
    write_post(blog, "ready", site)
    head_before = _git(blog, "rev-parse", "HEAD")
    _refuse_pushes(blog_remote)

    result = srv.commit_and_push("ready")

    assert result["ok"] is False
    assert "refused by the test hook" in result["error"]
    assert "permission" not in result["error"], "a refusal that is not about access was captioned as one"
    assert _git(blog, "rev-parse", "HEAD") == head_before, "the refused publish left a commit behind"
    assert "Publish: ready" not in _subjects_on_main(blog_remote)


def test_a_publish_does_not_carry_other_unpushed_commits(blog, blog_remote, site, srv):
    """The push sends the publish and only the publish.

    Previously: `git push` sent the whole branch, so anything else committed locally
    — a half-finished draft, an experiment — went public with the post."""
    (blog / "draft-notes.md").write_text("not ready for anyone\n", encoding="utf-8")
    _git(blog, "add", "draft-notes.md")
    _git(blog, "commit", "-qm", "wip: notes I am not publishing")
    write_post(blog, "ready", site)

    result = srv.commit_and_push("ready")

    assert result["ok"] is True, result
    assert "Publish: ready" in _subjects_on_main(blog_remote)
    assert "wip: notes I am not publishing" not in _subjects_on_main(blog_remote)
    tracked = _git(blog_remote, "ls-tree", "-r", "--name-only", "main")
    assert "draft-notes.md" not in tracked
    assert result.get("checkout_warning"), "carrying local commits should be said out loud"


def test_a_post_main_already_has_is_not_republished(blog, blog_remote, site, srv, tmp_path):
    """Another writer published this exact file first. The answer is "nothing to
    commit", decided against main.

    Previously: the rebase emptied the commit and the tool still reported pushed."""
    write_post(blog, "ready", site)
    body = (blog / "blog" / "ready" / "index.html").read_text(encoding="utf-8")
    _an_action_pushes(blog_remote, tmp_path / "other-writer", "Publish: ready",
                      post="ready", body=body)

    result = srv.commit_and_push("ready")

    assert result["ok"] is True, result
    assert result["committed"] is False
    assert "pushed" not in result, "claimed a push for a publish it did not make"
    assert _subjects_on_main(blog_remote).count("Publish: ready") == 1


def test_a_missing_post_is_an_error_not_nothing_to_commit(blog, blog_remote, site, srv):
    """`git status` on a path that does not exist and is not tracked reports nothing,
    which the old code read as "already up to date" — this defect's fail-open shape,
    reachable by calling the tool on its own."""
    result = srv.commit_and_push("never-written")

    assert result["ok"] is False
    assert "does not exist" in result["error"]
    assert result.get("committed") is False


def test_the_publish_commit_carries_only_the_post(blog, blog_remote, site, srv):
    """Whatever the checkout has staged stays in the checkout. The commit is main
    plus one file, by construction rather than by remembering to stage narrowly."""
    (blog / "secret.txt").write_text("staged by hand, not mine to publish\n", encoding="utf-8")
    _git(blog, "add", "secret.txt")
    write_post(blog, "ready", site)

    result = srv.commit_and_push("ready")

    assert result["ok"] is True, result
    changed = _git(blog_remote, "show", "--name-only", "--format=", "main").split()
    assert changed == ["blog/ready/index.html"], f"the publish commit carried {changed}"
    assert "secret.txt" not in _git(blog_remote, "ls-tree", "-r", "--name-only", "main")


def test_main_moving_mid_publish_is_decided_again(
        blog, blog_remote, site, srv, tmp_path, monkeypatch):
    """An Action lands between the fetch and the push. The push is refused and the
    whole decision is made again against the new main rather than replayed onto it."""
    write_post(blog, "ready", site)
    _move_main_after_fetch(monkeypatch, srv, lambda n: _an_action_pushes(
        blog_remote, tmp_path / f"action-{n}"))

    result = srv.commit_and_push("ready")

    assert result["ok"] is True, result
    assert result.get("caught_up") is True
    subjects = _subjects_on_main(blog_remote)
    assert "Publish: ready" in subjects
    assert any(s.startswith("chore: rebuild feed") for s in subjects), \
        "the Action's commit was discarded"
    assert subjects.index("Publish: ready") == 0, "the publish should sit on top"
    assert not any(s.startswith("Merge ") for s in subjects), \
        "the history should stay a straight line"


def test_the_same_post_landing_mid_publish_is_not_published_twice(
        blog, blog_remote, site, srv, tmp_path, monkeypatch):
    """Another writer lands this exact post between the fetch and the push. The
    second decision sees it and stands down; a replay would have committed it again."""
    write_post(blog, "ready", site)
    body = (blog / "blog" / "ready" / "index.html").read_text(encoding="utf-8")
    _move_main_after_fetch(monkeypatch, srv, lambda n: _an_action_pushes(
        blog_remote, tmp_path / f"other-{n}", "Publish: ready", post="ready", body=body))

    result = srv.commit_and_push("ready")

    assert result["ok"] is True, result
    assert result["committed"] is False
    assert _subjects_on_main(blog_remote).count("Publish: ready") == 1


def test_a_main_that_never_settles_gives_up_cleanly(
        blog, blog_remote, site, srv, tmp_path, monkeypatch):
    """Bounded: if main moves after every fetch, the publish stops after its
    attempts, says so, and leaves nothing behind for a re-run to trip over."""
    write_post(blog, "ready", site)
    head_before = _git(blog, "rev-parse", "HEAD")
    fired = _move_main_after_fetch(monkeypatch, srv, lambda n: _an_action_pushes(
        blog_remote, tmp_path / f"action-{n}", f"chore: rebuild {n}"), times=99)

    result = srv.commit_and_push("ready")

    assert result["ok"] is False
    assert "moved during each of" in result["error"]
    assert len(fired) == srv._PUBLISH_ATTEMPTS
    assert "Publish: ready" not in _subjects_on_main(blog_remote)
    assert _git(blog, "rev-parse", "HEAD") == head_before


def test_without_main_nothing_is_decided(blog, site, srv, tmp_path):
    """If main cannot be read, the tool does not fall back to the local branch. That
    fallback is the stale answer this module exists to stop giving, and a dry run is
    where it would be most tempting."""
    write_post(blog, "ready", site)
    _git(blog, "remote", "set-url", "origin", str(tmp_path / "nowhere.git"))

    result = srv.commit_and_push("ready", dry_run=True)

    assert result["ok"] is False
    assert "could not fetch" in result["error"]


def test_an_access_error_is_named_as_one(blog, blog_remote, site, srv):
    """The other half of the hint's rule: when the remote does say it is about
    access, the error says so."""
    hook = blog_remote / "hooks" / "pre-receive"
    hook.write_text("#!/bin/sh\n"
                    "echo 'Permission to RNVizion/rnvizion.github.io.git denied.' >&2\n"
                    "exit 1\n", encoding="utf-8")
    hook.chmod(0o755)
    write_post(blog, "ready", site)

    result = srv.commit_and_push("ready")

    assert result["ok"] is False
    assert result["error"].endswith("(site repo write permission?)"), result["error"]


def test_a_clean_publish_leaves_the_checkout_clean(blog, blog_remote, corpus, site,
                                                   srv, fake_web, tmp_path):
    """The silent half of the advisory pair, and the ergonomics it buys.

    The publish commit is built on main and never on the operator's branch, so their
    checkout has to be brought along deliberately. After a normal publish — including
    one where an Action had already moved main — the branch is on the published
    commit and `git status` is empty, so the next `git pull` is not a merge conflict
    against their own post.
    """
    _an_action_pushes(blog_remote, tmp_path / "action")
    write_post(blog, "ready", site)
    _live(fake_web, site, "ready")

    result = srv.publish_post("ready", for_real=True)

    assert result["ok"] is True, result
    assert "warnings" not in result, "a clean publish must not warn about anything"
    assert _git(blog, "status", "--porcelain") == "", "the checkout was left dirty"
    assert _git(blog, "rev-parse", "HEAD").strip() == _git(blog_remote, "rev-parse", "main").strip()


def test_a_checkout_with_other_changes_is_not_reset_and_says_so(
        blog, blog_remote, corpus, site, srv, fake_web):
    """The warning half, and the reason the preconditions exist.

    Bringing the branch along is a hard reset, which is lossless only because the
    post's bytes are already in the commit. Tried on 2026-09-18 with an unrelated
    tracked file modified: it published correctly and silently reverted that file.
    So anything else modified means the checkout is left alone and the result says
    what it found.
    """
    (blog / "README.md").write_text("my own uncommitted edit\n", encoding="utf-8")
    write_post(blog, "ready", site)
    _live(fake_web, site, "ready")

    result = srv.publish_post("ready", for_real=True)

    assert result["ok"] is True, result
    assert "Publish: ready" in _subjects_on_main(blog_remote), "the publish did not land"
    assert (blog / "README.md").read_text(encoding="utf-8") == "my own uncommitted edit\n", \
        "the operator's unrelated edit was destroyed"
    warnings = result.get("warnings", [])
    assert any("left where it was" in w for w in warnings), warnings
    assert any("README.md" in w for w in warnings), "the warning should name what it found"


@pytest.mark.parametrize("dirty", [True, False])
def test_untracked_files_are_never_a_reason_to_refuse_or_a_casualty(
        blog, blog_remote, site, srv, dirty):
    """A hard reset does not remove untracked files, confirmed on 2026-09-18, so an
    unrelated draft neither blocks the fast-forward nor is lost to it."""
    (blog / "scratch-draft.md").write_text("a draft I am keeping\n", encoding="utf-8")
    write_post(blog, "ready", site)
    if dirty:
        _git(blog, "add", "scratch-draft.md")

    result = srv.commit_and_push("ready")

    assert result["ok"] is True, result
    assert (blog / "scratch-draft.md").exists(), "an untracked file was destroyed"
    if dirty:
        assert result.get("checkout_warning"), "a staged file should hold the fast-forward"
    else:
        assert not result.get("checkout_warning"), "an untracked file should not hold it"
