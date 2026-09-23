"""The publish chain end to end, including the one place it is deliberately lenient."""
import json
import re
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


def test_og_image_that_catches_up_produces_no_warning(blog, blog_remote, corpus, site, srv, fake_web):
    """The image poll keeps looking, so a share card that lands in time is not reported late.

    Counterpart to test_lagging_og_image_warns_but_does_not_fail, and the thing that
    makes the image leniency rather than blindness: a check that gave up
    after one look could never tell 'late' from 'never'.

    The sitemap has had this counterpart since the two-wave deploy was understood.
    The image did not, so the claim that the image check keeps looking rested on the
    sitemap's evidence rather than its own.
    """
    write_post(blog, "ready", site)
    fake_web[f"{site}/blog/ready/"] = 200
    fake_web[f"{site}/sitemap.xml"] = (200, sitemap_xml(site, "ready"))

    image_url = f"{site}/assets/og/ready.png"
    calls = {"n": 0}

    class _Image:
        """404s twice, then 200 — build-og finishing after the page went live."""
        def __init__(self, status):
            self.status = status

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    fake_web[image_url] = 404  # declare the route so a missed patch fails loudly
    real_urlopen = srv.urllib.request.urlopen

    def _urlopen(req, timeout=None):
        url = req if isinstance(req, str) else req.full_url
        if url == image_url:
            calls["n"] += 1
            return _Image(200 if calls["n"] > 2 else 404)
        return real_urlopen(req, timeout)

    srv.urllib.request.urlopen = _urlopen
    try:
        result = srv.publish_post("ready", for_real=True)
    finally:
        srv.urllib.request.urlopen = real_urlopen

    assert result["ok"] is True
    assert calls["n"] > 2, "the image check must poll rather than look once"
    assert "warnings" not in result, f"image caught up; nothing to warn about: {result.get('warnings')}"


def test_a_waiting_poll_announces_itself(blog, corpus, site, srv, fake_web, capfd):
    """A poll that waits says so on stderr, every attempt.

    Bought with an incident, 2026-09-14. publish_post returns one result at the end,
    so across the three polls here — up to 330 seconds at the default budgets — a
    correct slow publish and a dead process are byte-identical from outside: both are
    a blank terminal. That silence was read as a hang. The run was killed four minutes
    after it had already committed, pushed and gone live, and undoing the publish that
    had in fact succeeded cost three hours.

    stderr rather than stdout, and this is not a style choice: stdout is the MCP
    protocol stream and writing to it corrupts the transport. The assertion is
    deliberately on the presence of the waiting, not on its wording — the lines are
    for a human watching a terminal, nothing parses them, and a test that pinned the
    phrasing would make an undeclared protocol out of a diagnostic.
    """
    from conftest import sitemap_xml

    write_post(blog, "slow", site)
    fake_web[f"{site}/blog/slow/"] = 200
    # Live page, but a sitemap that has not caught up and an image that never
    # arrives: wave two lagging, which is exactly when the waiting is long.
    fake_web[f"{site}/sitemap.xml"] = (200, sitemap_xml(site))

    result = srv.wait_for_live("slow", timeout=5, interval=1,
                               og_timeout=3, sitemap_timeout=3)

    assert result["ok"] is True
    assert result["sitemap_listed"] is False
    assert result["og_image_live"] is False

    err = capfd.readouterr().err
    assert "waiting on the sitemap" in err, "a lagging sitemap poll said nothing"
    assert "waiting on the share image" in err, "a lagging image poll said nothing"


def _a_workflow_pushes(remote, tmp_path, name: str, message: str) -> None:
    """Commit to the remote from somewhere that is not the chain's clone.

    This is what build-feed and build-og do after every publish: regenerate the
    index, feed, sitemap and share image, and commit them on top of the post. The
    local clone is behind from that moment on, which makes being behind the normal
    resting state between publishes rather than an anomaly.
    """
    work = tmp_path / name
    subprocess.run(["git", "clone", "-q", str(remote), str(work)],
                   check=True, capture_output=True)
    for key, value in (("user.email", "action@example.invalid"),
                       ("user.name", "Workflow"),
                       ("commit.gpgsign", "false")):
        subprocess.run(["git", "config", key, value], cwd=work,
                       check=True, capture_output=True)
    (work / "generated.txt").write_text(message + "\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=work, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-qm", message], cwd=work, check=True, capture_output=True)
    subprocess.run(["git", "push", "-q", "origin", "main"], cwd=work, check=True, capture_output=True)


def test_a_publish_catches_up_when_a_workflow_has_pushed(blog, blog_remote, corpus,
                                                         site, srv, fake_web, tmp_path):
    """A remote that moved since the last publish does not fail the next one.

    Bought on 2026-09-14. The chain pushed the post, the Actions committed their
    generated output on top, and the next run's push was rejected as non-fast-forward
    — correctly, and uselessly. Refusing here would fail every publish after the
    first until someone ran git pull by hand, and a gate that fires on the normal
    case is a gate people learn to skip.

    The mechanism under this changed on 2026-09-18 and the requirement did not. It
    was a rebase of the local publish commit onto the moved remote; it is now a fetch
    and a commit built on main, which reaches the same place without replaying a
    decision. What this test pins is the behaviour either mechanism owes: the
    workflow's commit survives, the publish sits on top of it, and no merge appears.
    It is deliberately silent on how. Catching up *mid-publish* is
    test_site_push.py's test_main_moving_mid_publish_is_decided_again.
    """
    from conftest import sitemap_xml

    write_post(blog, "ready", site)
    fake_web[f"{site}/blog/ready/"] = 200
    fake_web[f"{site}/sitemap.xml"] = (200, sitemap_xml(site, "ready"))
    fake_web[f"{site}/assets/og/ready.png"] = 200

    workflow = "chore: rebuild feed, index, sitemap and robots [skip ci]"
    _a_workflow_pushes(blog_remote, tmp_path, "site-workflow", workflow)

    result = srv.publish_post("ready", for_real=True)

    assert result["ok"] is True, result
    subjects = _remote_log(blog_remote)
    assert "Publish: ready" in subjects, "the post never reached the remote"
    assert workflow in subjects, "the catch-up discarded the workflow's commit"
    # Newest first: the publish must sit ON TOP of the workflow's commit, replayed
    # rather than merged beside it.
    assert subjects.index("Publish: ready") < subjects.index(workflow)
    assert not any(s.startswith("Merge ") for s in subjects), \
        "caught up with a merge; the history should stay a straight line"


def test_the_corpus_push_catches_up_too(blog, blog_remote, corpus, site, srv,
                                        fake_web, tmp_path):
    """The corpus repo has the same shape of writer, so it has the same exposure.

    On 2026-09-14 this was the half that actually broke: the post went live and the
    corpus write was rejected, leaving the site published and the corpus not — the
    exact out-of-step state the chain's ordering exists to prevent.
    """
    from conftest import sitemap_xml

    write_post(blog, "ready", site)
    fake_web[f"{site}/blog/ready/"] = 200
    fake_web[f"{site}/sitemap.xml"] = (200, sitemap_xml(site, "ready"))
    fake_web[f"{site}/assets/og/ready.png"] = 200

    rebuild = "corpus: refresh after source change"
    _a_workflow_pushes(tmp_path / "corpus-remote.git", tmp_path, "corpus-workflow", rebuild)

    result = srv.publish_post("ready", for_real=True)

    assert result["ok"] is True, result
    corpus_subjects = _remote_log(tmp_path / "corpus-remote.git")
    assert "corpus: add ready" in corpus_subjects, "the corpus entry never reached the remote"
    assert rebuild in corpus_subjects, "the catch-up discarded the rebuild commit"


# ==========================================================================
# The three wait_for_live findings of 2026-09-23. Each reproduced against
# `fd3f03f` before anything changed.
#
# The unifying defect is that this tool reported on the post's metadata without
# having read the post, and on the site without having checked its own config. It
# is the tool whose docstring names the gap it still had: "surfaced loudly as
# og_image_live=False so a broken share card never passes silently — the exact gap
# that shipped three imageless posts before."

def strip_og_image(path):
    path.write_text(re.sub(r'\s*<meta property="og:image"[^>]*>', "",
                           path.read_text(encoding="utf-8")), encoding="utf-8")
    return path


def live_and_listed(fake_web, site, slug):
    fake_web[f"{site}/blog/{slug}/"] = 200
    fake_web[f"{site}/sitemap.xml"] = (200, sitemap_xml(site, slug))


def test_a_post_declaring_no_image_does_not_pass_silently(blog, blog_remote, corpus,
                                                          site, srv, fake_web):
    """The reproduction, and it is the state the docstring names. publish_post
    promoted the advisory on `og_image_live is False`; a post declaring no image
    sets None, so the warning was built and then dropped. The publish returned ok
    with no warnings key at all."""
    strip_og_image(write_post(blog, "imageless", site))
    live_and_listed(fake_web, site, "imageless")

    result = srv.publish_post("imageless", for_real=True)

    assert result["ok"] is True, "a missing image must not fail the publish"
    assert result.get("warnings"), "the publish reported nothing at all"
    assert any("no og:image" in w for w in result["warnings"]), result["warnings"]


def test_looked_and_found_none_is_not_could_not_look(blog, corpus, site, srv, fake_web):
    """Two states the old message collapsed into one sentence. They send the reader
    to different files, so they are reported differently and `og_image_checked`
    says which happened."""
    strip_og_image(write_post(blog, "imageless", site))
    live_and_listed(fake_web, site, "imageless")

    looked = srv.wait_for_live("imageless", timeout=3, og_timeout=3, sitemap_timeout=3)

    assert looked["og_image_checked"] is True
    assert "declares no og:image" in looked["og_image_warning"]


def test_a_post_this_checkout_cannot_read_is_not_claimed_to_declare_nothing(
        blog, corpus, site, srv, fake_web):
    """The second finding. The page is live — published from another machine, say —
    and this checkout has no copy, so the tool asserted the post's metadata about a
    file it never opened. validate_post, asked about the same slug, says `no
    index.html`. Principle 10: say which question could not be asked."""
    write_post(blog, "real", site)
    live_and_listed(fake_web, site, "ghost")

    result = srv.wait_for_live("ghost", timeout=3, og_timeout=3, sitemap_timeout=3)

    assert result["ok"] is True, "the page is live; this is advisory only"
    assert result["og_image_checked"] is False
    assert "was not checked" in result["og_image_warning"], result["og_image_warning"]
    assert "declares no og:image" not in result["og_image_warning"], \
        "it still claims the post declares nothing"


def test_an_unresolvable_blog_repo_is_named_as_the_reason(blog, corpus, site, srv,
                                                          fake_web, monkeypatch, tmp_path):
    """Same shape one rung up, and the message carries the provenance because half a
    diagnosis is which rung the path came from."""
    live_and_listed(fake_web, site, "real")
    write_post(blog, "real", site)
    monkeypatch.setenv("BLOG_REPO", str(tmp_path / "nowhere-at-all"))

    result = srv.wait_for_live("real", timeout=3, og_timeout=3, sitemap_timeout=3)

    assert result["ok"] is True, "BLOG_REPO is not what this tool needs to do its job"
    assert result["og_image_checked"] is False
    assert "BLOG_REPO" in result["og_image_warning"] and \
           "source: environment" in result["og_image_warning"], result["og_image_warning"]


def test_a_rejected_site_url_reports_as_config_not_as_a_dead_page(blog, corpus, srv,
                                                                  monkeypatch):
    """The third finding. This was the one tool with no config gate, so a SITE_URL
    config_report explicitly rejects produced `not live after Ns` — blaming the site
    for a value that never formed a URL. Principle 16."""
    monkeypatch.setenv("SITE_URL", "rnvizion.dev")

    result = srv.wait_for_live("anything", timeout=2, og_timeout=2, sitemap_timeout=2)

    assert result["ok"] is False
    assert result["error"] == "config", result
    assert "not live" not in json.dumps(result), "it still blames the page"


def test_blog_repo_is_deliberately_not_gated_here(blog, corpus, site, srv, fake_web,
                                                  monkeypatch, tmp_path):
    """The other half of the gate decision. wait_for_live needs BLOG_REPO only for
    the advisory image check, so an unresolvable one costs that check and nothing
    else — the same test the dry/real asymmetry uses. Gating it would turn a live
    page into a failed verification."""
    live_and_listed(fake_web, site, "real")
    write_post(blog, "real", site)
    monkeypatch.setenv("BLOG_REPO", str(tmp_path / "nowhere-at-all"))

    result = srv.wait_for_live("real", timeout=3, og_timeout=3, sitemap_timeout=3)

    assert result["ok"] is True and result["live"] is True
    assert result.get("error") != "config"


def test_the_site_url_rule_has_one_definition(srv):
    """The gate and the tool ask the same question because they call the same
    function. Two copies of a rule are identical only until someone edits one."""
    import inspect
    source = inspect.getsource(srv)
    assert source.count("is not a usable origin") == 1, \
        "a second copy of the SITE_URL rule appeared"
    assert source.count("_site_url_problem()") >= 2, \
        "the shared definition has fewer than two callers"
