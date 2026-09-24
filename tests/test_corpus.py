"""Registering a post with the corpus, on a main that other writers also move.

The agent is one of two registrars. check-source-edits.yml in the corpus repo runs
every six hours and registers any feed post that sources.json lacks, with the same
{"id", "url"} entry, so between one publish and the next, main can gain entries the
chain's checkout has never seen. These tests put the other registrar's commits on
the remote the way the Action does, from a clone that is not the chain's, and pin
that update_corpus decides against main rather than against its checkout.

The first three are the cases reproduced on 2026-09-18 against the previous code,
which decided from the checkout and reconciled by rebase:
  - the other registrar had added a different post: a rebase conflict, reported as
    a permission problem, and main never got the new post
  - it had added this post: "added, pushed" for a commit that rebased to nothing
  - after a refused corpus push: the re-run read its own unpushed commit, said
    "already in sources.json", and went green with main still missing the entry
"""
import json
import subprocess

import pytest

from conftest import sitemap_xml, write_post


@pytest.fixture
def corpus_remote(corpus, tmp_path):
    """The bare remote the corpus fixture pushes to: the corpus repo's main."""
    return tmp_path / "corpus-remote.git"


def _git(cwd, *args) -> str:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True,
                          check=True).stdout


def _other_registrar_pushes(remote, work, *slugs, site) -> None:
    """What discover.py and check-source-edits.yml do: clone main, append, push."""
    subprocess.run(["git", "clone", "-q", str(remote), str(work)],
                   check=True, capture_output=True)
    for key, value in (("user.email", "action@example.invalid"),
                       ("user.name", "Workflow"),
                       ("commit.gpgsign", "false")):
        _git(work, "config", key, value)
    path = work / "sources.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    for slug in slugs:
        data["sources"].append({"id": slug, "url": f"{site}/blog/{slug}/"})
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    (work / ".source-hashes.json").write_text("{}\n", encoding="utf-8")
    _git(work, "add", "sources.json", ".source-hashes.json")
    _git(work, "commit", "-qm", f"corpus: refresh after source change ({','.join(slugs)}) [skip ci]")
    _git(work, "push", "-q", "origin", "main")


def _move_main_after_fetch(monkeypatch, srv, move, times=1) -> list:
    """Call move(n) right after update_corpus's nth fetch, up to `times` times: main
    moves between the look and the push, as when the six-hourly pass lands
    mid-registration. Returns the list of fetches it fired on."""
    real = srv._corpus_git
    fired = []

    def wrapped(*args, **kwargs):
        result = real(*args, **kwargs)
        if args and args[0] == "fetch" and len(fired) < times:
            fired.append(args)
            move(len(fired))
        return result

    monkeypatch.setattr(srv, "_corpus_git", wrapped)
    return fired


def _live(fake_web, site, slug) -> None:
    fake_web[f"{site}/blog/{slug}/"] = 200
    fake_web[f"{site}/sitemap.xml"] = (200, sitemap_xml(site, slug))
    fake_web[f"{site}/assets/og/{slug}.png"] = 200


def _ids_on_main(remote) -> list:
    return [s["id"] for s in json.loads(_git(remote, "show", "main:sources.json"))["sources"]]


def _subjects_on_main(remote) -> list:
    return _git(remote, "log", "--format=%s", "main").splitlines()


def _ids_in_checkout(corpus) -> list:
    data = json.loads((corpus / "sources.json").read_text(encoding="utf-8"))
    return [s["id"] for s in data["sources"]]


def _corpus_step(result) -> dict:
    return next(s for s in result["trace"] if s["step"] == "update_corpus")


def test_registers_beside_a_post_the_other_registrar_added(
        blog, blog_remote, corpus, corpus_remote, site, srv, fake_web, tmp_path):
    """A post published by hand reached the feed, and the six-hourly pass registered
    it. The agent's next publish lands beside it rather than colliding with it.

    Previously: stopped at update_corpus on a rebase conflict, captioned
    "(corpus repo write permission?)", and main never received the new post."""
    _other_registrar_pushes(corpus_remote, tmp_path / "discover", "by-hand", site=site)
    write_post(blog, "ready", site)
    _live(fake_web, site, "ready")

    result = srv.publish_post("ready", for_real=True)

    assert result["ok"] is True, result
    assert _ids_on_main(corpus_remote) == ["by-hand", "ready"]
    assert "corpus: add ready" in _subjects_on_main(corpus_remote)
    assert _ids_in_checkout(corpus) == ["by-hand", "ready"], "the checkout did not follow main"
    assert "warnings" not in result


def test_a_post_the_other_registrar_already_added_is_left_alone(
        blog, blog_remote, corpus, corpus_remote, site, srv, fake_web, tmp_path):
    """The six-hourly pass registered this very post first, as after a run that died
    between live and corpus. The answer is "already there", decided against main,
    and nothing is committed.

    Previously: "added: true, pushed: true" for a commit that rebased to nothing and
    never reached main; and a dry run said it would add, having read the checkout."""
    _other_registrar_pushes(corpus_remote, tmp_path / "discover", "ready", site=site)
    write_post(blog, "ready", site)
    _live(fake_web, site, "ready")

    dry = srv.update_corpus("ready", dry_run=True)
    assert dry["ok"] is True and dry["added"] is False, dry
    assert dry.get("reason") == "already in sources.json", "the dry run read the checkout, not main"

    result = srv.publish_post("ready", for_real=True)
    step = _corpus_step(result)

    assert result["ok"] is True, result
    assert step["added"] is False
    assert "pushed" not in step, "claimed a push for a registration it did not make"
    assert _ids_on_main(corpus_remote) == ["ready"]
    assert "corpus: add ready" not in _subjects_on_main(corpus_remote)


def test_a_refused_corpus_push_leaves_nothing_behind(
        blog, blog_remote, corpus, corpus_remote, site, srv, fake_web):
    """The remote refuses the push. Nothing may be left in the checkout to pass for
    a registration later, and the error must not blame access.

    Previously: the refused commit stayed on the checkout's main. The re-run read it,
    said "already in sources.json", and went green while main never received the
    entry; every later registration then collided with it."""
    hook = corpus_remote / "hooks" / "pre-receive"
    hook.write_text("#!/bin/sh\necho 'refused by the test hook' >&2\nexit 1\n", encoding="utf-8")
    hook.chmod(0o755)
    write_post(blog, "ready", site)
    _live(fake_web, site, "ready")
    head_before = _git(corpus, "rev-parse", "HEAD")

    first = srv.publish_post("ready", for_real=True)

    assert first["ok"] is False and first["stopped_at"] == "update_corpus", first
    error = _corpus_step(first)["error"]
    assert "refused by the test hook" in error
    assert "permission" not in error, "a refusal that is not about access was captioned as one"
    assert _git(corpus, "rev-parse", "HEAD") == head_before, "the refused registration left a commit behind"
    assert _git(corpus, "status", "--porcelain") == "", "the refused registration left the checkout dirty"

    hook.unlink()
    second = srv.publish_post("ready", for_real=True)

    assert second["ok"] is True, second
    assert _corpus_step(second)["added"] is True, "the re-run did not register the post"
    assert _ids_on_main(corpus_remote) == ["ready"]


def test_an_access_error_is_named_as_one(blog, corpus, corpus_remote, site, srv, fake_web):
    """The other half of the hint's rule: when the remote does say it is about access,
    the error says so. (This test's name, which lands in the remote's path and so in
    git's message, must not contain the words the hint looks for.)"""
    hook = corpus_remote / "hooks" / "pre-receive"
    hook.write_text("#!/bin/sh\n"
                    "echo 'Permission to RNVizion/rnv-ask-the-corpus.git denied to someone.' >&2\n"
                    "exit 1\n", encoding="utf-8")
    hook.chmod(0o755)
    write_post(blog, "ready", site)
    fake_web[f"{site}/blog/ready/"] = 200

    result = srv.update_corpus("ready")

    assert result["ok"] is False
    assert result["error"].endswith("(corpus repo write permission?)"), result["error"]


def test_main_moving_mid_registration_is_decided_again(
        blog, corpus, corpus_remote, site, srv, fake_web, tmp_path, monkeypatch):
    """Main moves between the fetch and the push. The push is refused, and the whole
    decision is made again against the new main rather than replayed onto it."""
    write_post(blog, "ready", site)
    fake_web[f"{site}/blog/ready/"] = 200
    _move_main_after_fetch(monkeypatch, srv, lambda n: _other_registrar_pushes(
        corpus_remote, tmp_path / f"discover-{n}", "by-hand", site=site))

    result = srv.update_corpus("ready")

    assert result["ok"] is True, result
    assert result.get("caught_up") is True
    assert _ids_on_main(corpus_remote) == ["by-hand", "ready"]


def test_the_same_post_landing_mid_registration_is_not_added_twice(
        blog, corpus, corpus_remote, site, srv, fake_web, tmp_path, monkeypatch):
    """The other registrar lands this very post between the fetch and the push. The
    second decision sees it and stands down; a replay would have added it twice or
    claimed a push that never happened."""
    write_post(blog, "ready", site)
    fake_web[f"{site}/blog/ready/"] = 200
    _move_main_after_fetch(monkeypatch, srv, lambda n: _other_registrar_pushes(
        corpus_remote, tmp_path / f"discover-{n}", "ready", site=site))

    result = srv.update_corpus("ready")

    assert result["ok"] is True, result
    assert result["added"] is False
    assert _ids_on_main(corpus_remote) == ["ready"]
    assert "corpus: add ready" not in _subjects_on_main(corpus_remote)


def test_a_main_that_never_settles_gives_up_cleanly(
        blog, corpus, corpus_remote, site, srv, fake_web, tmp_path, monkeypatch):
    """Bounded: if main moves after every fetch, the registration stops after its
    attempts, says so, and leaves nothing behind for a re-run to trip over."""
    write_post(blog, "ready", site)
    fake_web[f"{site}/blog/ready/"] = 200
    head_before = _git(corpus, "rev-parse", "HEAD")
    fired = _move_main_after_fetch(monkeypatch, srv, lambda n: _other_registrar_pushes(
        corpus_remote, tmp_path / f"discover-{n}", f"other-{n}", site=site), times=99)

    result = srv.update_corpus("ready")

    assert result["ok"] is False
    assert "moved during each of" in result["error"]
    assert len(fired) == srv._REGISTER_ATTEMPTS
    assert "ready" not in _ids_on_main(corpus_remote)
    assert _git(corpus, "rev-parse", "HEAD") == head_before


def test_without_main_nothing_is_decided(blog, corpus, site, srv, fake_web, tmp_path):
    """If main cannot be read, the tool does not fall back to the checkout's copy.
    That fallback is the stale answer this module exists to stop giving, and a dry
    run is where it would be most tempting."""
    write_post(blog, "ready", site)
    fake_web[f"{site}/blog/ready/"] = 200
    _git(corpus, "remote", "set-url", "origin", str(tmp_path / "nowhere.git"))

    result = srv.update_corpus("ready", dry_run=True)

    assert result["ok"] is False
    assert "could not fetch" in result["error"]


def test_the_registration_commit_carries_sources_json_and_nothing_else(
        blog, corpus, corpus_remote, site, srv, fake_web):
    """Whatever the checkout has staged stays in the checkout. Pins the claim made to
    the corpus chat on 2026-09-18, that the agent never commits chroma/, by
    construction rather than by habit: the commit is main plus one file."""
    (corpus / "chroma").mkdir()
    (corpus / "chroma" / "index.bin").write_text("a hand-built index\n", encoding="utf-8")
    _git(corpus, "add", "chroma/index.bin")
    write_post(blog, "ready", site)
    fake_web[f"{site}/blog/ready/"] = 200

    result = srv.update_corpus("ready")

    assert result["ok"] is True, result
    changed = _git(corpus_remote, "show", "--name-only", "--format=", "main").split()
    assert changed == ["sources.json"], f"the registration commit carried {changed}"
    assert "chroma/index.bin" in _git(corpus, "diff", "--cached", "--name-only"), \
        "the checkout's staged work was disturbed"


def test_a_checkout_that_cannot_follow_warns_and_the_registration_stands(
        blog, blog_remote, corpus, corpus_remote, site, srv, fake_web):
    """The warning half of an advisory pair. The silent half is every clean publish
    in test_chain.py and above, which asserts no warnings at all.

    Here the checkout carries a stranded registration main does not have, the state
    a refused push used to leave. The new registration must still land, must not
    carry that commit with it, must not rewrite the checkout, and must say the
    checkout was left where it was."""
    path = corpus / "sources.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data["sources"].append({"id": "stranded", "url": f"{site}/blog/stranded/"})
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    _git(corpus, "commit", "-qam", "corpus: add stranded")
    head_before = _git(corpus, "rev-parse", "HEAD")
    write_post(blog, "ready", site)
    _live(fake_web, site, "ready")

    result = srv.publish_post("ready", for_real=True)

    assert result["ok"] is True, result
    assert _ids_on_main(corpus_remote) == ["ready"], "the stranded commit was pushed along"
    assert _git(corpus, "rev-parse", "HEAD") == head_before, "the checkout's branch was rewritten"
    assert any("left where it was" in w for w in result.get("warnings", [])), result.get("warnings")


# ==========================================================================
# The two findings of 2026-09-23, both reproduced against `eb4f528`.
#
# Both are the same shape the 2026-09-18 change was about, surviving in two
# places it did not reach: a claim decided from the wrong source, and a claim
# decided from half its condition.

def seed_main(corpus_remote, tmp_path, entries, name="otherreg"):
    """Put arbitrary entries on the corpus main, the way discover.py would.

    Unlike `_other_registrar_pushes`, the entry is given rather than derived, so a
    test can stage an id and a URL that disagree. That helper always writes
    `{"id": slug, "url": f"{site}/blog/{slug}/"}` — the identical form the agent
    writes — which is exactly why the id-versus-url divergence was never exercised.
    """
    work = tmp_path / name
    subprocess.run(["git", "clone", "-q", str(corpus_remote), str(work)],
                   check=True, capture_output=True)
    for key, value in (("user.email", "action@example.invalid"),
                       ("user.name", "Workflow"), ("commit.gpgsign", "false")):
        _git(work, "config", key, value)
    path = work / "sources.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data["sources"].extend(entries)
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    _git(work, "add", "sources.json")
    _git(work, "commit", "-qm", "discover: add")
    _git(work, "push", "-q", "origin", "main")


def sources_on_main(corpus_remote):
    out = subprocess.run(["git", "--git-dir", str(corpus_remote), "show", "main:sources.json"],
                         capture_output=True, text=True, check=True).stdout
    return json.loads(out)["sources"]


# --- Finding A: half an entry reported as the whole one -------------------

def test_the_slug_under_another_url_is_not_called_registered(blog, corpus, corpus_remote,
                                                             site, srv, fake_web, tmp_path):
    """The reproduction, and the expensive half. The corpus then fetches an address
    the post does not answer at — the stage-5 failure this tool exists to prevent,
    reported as success, and every re-run repeated it."""
    write_post(blog, "a-post", site)
    fake_web[f"{site}/blog/a-post/"] = 200
    seed_main(corpus_remote, tmp_path,
              [{"id": "a-post", "url": "https://rnvizion.dev.old/blog/a-post/"}])

    result = srv.update_corpus("a-post")

    assert result["ok"] is False, "a different entry was reported as this one"
    assert result["error"] == "entry disagreement"
    assert result["wanted"] == {"id": "a-post", "url": f"{site}/blog/a-post/"}
    assert result["found_on_main"] == [{"id": "a-post",
                                        "url": "https://rnvizion.dev.old/blog/a-post/"}]


def test_this_url_under_another_id_is_not_called_registered(blog, corpus, corpus_remote,
                                                            site, srv, fake_web, tmp_path):
    """The other half of the same `or`. The post is retrievable under a name nothing
    else uses."""
    write_post(blog, "a-post", site)
    fake_web[f"{site}/blog/a-post/"] = 200
    seed_main(corpus_remote, tmp_path,
              [{"id": "typo-slug", "url": f"{site}/blog/a-post/"}])

    result = srv.update_corpus("a-post")

    assert result["ok"] is False and result["error"] == "entry disagreement"
    assert result["found_on_main"] == [{"id": "typo-slug", "url": f"{site}/blog/a-post/"}]


def test_a_disagreement_writes_nothing(blog, corpus, corpus_remote, site, srv,
                                       fake_web, tmp_path):
    """It refuses rather than resolving. The id rule and the URL form belong to the
    corpus project and a second registrar writes the same file, so overwriting
    their entry would be this agent choosing whose form is right."""
    write_post(blog, "a-post", site)
    fake_web[f"{site}/blog/a-post/"] = 200
    theirs = [{"id": "a-post", "url": "https://rnvizion.dev.old/blog/a-post/"}]
    seed_main(corpus_remote, tmp_path, theirs)

    srv.update_corpus("a-post")

    assert sources_on_main(corpus_remote) == theirs, "it rewrote the other registrar's entry"


def test_a_disagreement_is_reported_by_a_dry_run_too(blog, corpus, corpus_remote,
                                                     site, srv, fake_web, tmp_path):
    """A dry run exists to say what a real run would do, so it must not report
    `would_add` for a registration the real run refuses."""
    write_post(blog, "a-post", site)
    fake_web[f"{site}/blog/a-post/"] = 200
    seed_main(corpus_remote, tmp_path,
              [{"id": "a-post", "url": "https://rnvizion.dev.old/blog/a-post/"}])

    result = srv.update_corpus("a-post", dry_run=True)

    assert result["ok"] is False and result["error"] == "entry disagreement"
    assert "would_add" not in result


def test_a_matching_entry_is_still_already_registered(blog, corpus, corpus_remote,
                                                      site, srv, fake_web, tmp_path):
    """The silent half. Both halves matching is the ordinary idempotent case and
    must stay quiet — a check that refuses everything is not a check."""
    write_post(blog, "a-post", site)
    fake_web[f"{site}/blog/a-post/"] = 200
    _other_registrar_pushes(corpus_remote, tmp_path / "other", "a-post", site=site)

    result = srv.update_corpus("a-post")

    assert result["ok"] is True and result["added"] is False
    assert result["reason"] == "already in sources.json"


def test_the_two_halves_of_one_entry_split_across_two_entries_still_disagree(
        blog, corpus, corpus_remote, site, srv, fake_web, tmp_path):
    """The case a per-half check would miss: the id on one entry, the URL on
    another, and neither entry is the one this tool wanted."""
    write_post(blog, "a-post", site)
    fake_web[f"{site}/blog/a-post/"] = 200
    seed_main(corpus_remote, tmp_path,
              [{"id": "a-post", "url": "https://rnvizion.dev.old/blog/a-post/"},
               {"id": "typo-slug", "url": f"{site}/blog/a-post/"}])

    result = srv.update_corpus("a-post")

    assert result["ok"] is False and len(result["found_on_main"]) == 2


# --- Finding B: a precondition asked of the checkout ----------------------

def test_a_checkout_without_sources_json_still_registers(blog, corpus, corpus_remote,
                                                         site, srv, fake_web):
    """The reproduction. `main` has the file, the checkout does not — another
    branch, a sparse clone, a half-finished rebase — and the registration is
    entirely a function of `main`. The refusal blamed CORPUS_REPO, which was set
    correctly, in the tool whose own docstring says the checkout decides nothing."""
    write_post(blog, "a-post", site)
    fake_web[f"{site}/blog/a-post/"] = 200
    (corpus / "sources.json").unlink()

    result = srv.update_corpus("a-post")

    assert result["ok"] is True, result
    assert any(s["id"] == "a-post" for s in sources_on_main(corpus_remote))


def test_a_main_without_sources_json_is_still_refused(blog, corpus, corpus_remote,
                                                      site, srv, fake_web, tmp_path):
    """Nothing replaced the deleted precondition, so this pins that the guard which
    actually mattered is still there — and that it names `main`, not the checkout."""
    write_post(blog, "a-post", site)
    fake_web[f"{site}/blog/a-post/"] = 200
    work = tmp_path / "wipe"
    subprocess.run(["git", "clone", "-q", str(corpus_remote), str(work)],
                   check=True, capture_output=True)
    for key, value in (("user.email", "a@b.invalid"), ("user.name", "W"),
                       ("commit.gpgsign", "false")):
        _git(work, "config", key, value)
    (work / "sources.json").unlink()
    _git(work, "add", "-A")
    _git(work, "commit", "-qm", "drop sources.json")
    _git(work, "push", "-q", "origin", "main")

    result = srv.update_corpus("a-post")

    assert result["ok"] is False
    assert "main" in result["error"] and "sources.json" in result["error"], result["error"]
