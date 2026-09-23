"""The two claims preflight makes about itself, from the note of 2026-09-22.

`tools/preflight.py` is manual-only by the decision of 2026-08-29, because it
asserts against the real machine and the real checkout and CI can reach neither.
**That ruling is narrowed here rather than reversed.** It covers the checks that
read the machine — git, push access, the resolved paths. It does not cover how
the agent-route verdict is composed from its two inputs, which is a pure function
of an importability and an environment variable, both controllable from a test.
Nothing here touches git, the network, or a real checkout.

Both defects were reported from outside this repo and reproduced here by running
the file before either was changed:

  - the usage block required a working directory the code had not needed since
    the commit that anchored every path to `__file__`
  - check_dependencies and check_agent_route each owned half of the agent.py
    condition and each asserted the whole verdict, so with the package missing
    and the key set one run printed "unavailable" and then "available" — and the
    false all-clear printed last
"""
import importlib.util as real_util
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
PREFLIGHT = REPO_ROOT / "tools" / "preflight.py"


@pytest.fixture
def pf():
    """The preflight module, freshly loaded, with its report buffers cleared.

    `problems` and `warnings` are module-level lists that every check appends to,
    so a shared import would let one test's findings show up in the next.
    """
    spec = real_util.spec_from_file_location("rnv_preflight_under_test", PREFLIGHT)
    module = real_util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.problems.clear()
    module.warnings.clear()
    return module


def with_packages_absent(pf, monkeypatch, absent):
    """Make find_spec report `absent` as missing, for this module only.

    Scoped to preflight's own reference rather than to importlib itself, so the
    test does not reach into the interpreter pytest is running on.
    """
    def find_spec(name):
        return None if name in absent else real_util.find_spec(name)

    monkeypatch.setattr(pf, "importlib",
                        SimpleNamespace(util=SimpleNamespace(find_spec=find_spec)))


def verdict_lines(captured):
    """Every line that states whether agent.py can run."""
    return [l for l in captured.splitlines() if "agent.py available" in l
            or "agent.py unavailable" in l]


# --------------------------------------------------------------------------
# Finding 2 — one owner for the verdict, and it names the failing input.

def test_both_inputs_present_reports_available(pf, monkeypatch, capsys):
    with_packages_absent(pf, monkeypatch, set())
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")

    pf.check_agent_route()

    lines = verdict_lines(capsys.readouterr().out)
    assert len(lines) == 1, f"the verdict was stated {len(lines)} times: {lines}"
    assert "agent.py available" in lines[0]
    assert pf.problems == [] and pf.warnings == []


def test_package_missing_with_the_key_set_reports_unavailable(pf, monkeypatch, capsys):
    """The reproduction. This is the state that produced a false all-clear, and
    the `ok` printed last, so it was the verdict a reader carried away."""
    with_packages_absent(pf, monkeypatch, {"anthropic"})
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")

    pf.check_agent_route()

    lines = verdict_lines(capsys.readouterr().out)
    assert len(lines) == 1, f"the verdict was stated {len(lines)} times: {lines}"
    assert "agent.py unavailable" in lines[0]
    assert "anthropic not importable" in lines[0], "the failing input was not named"


def test_key_missing_with_the_package_present_reports_unavailable(pf, monkeypatch, capsys):
    with_packages_absent(pf, monkeypatch, set())
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    pf.check_agent_route()

    lines = verdict_lines(capsys.readouterr().out)
    assert len(lines) == 1
    assert "agent.py unavailable" in lines[0]
    assert "ANTHROPIC_API_KEY unset" in lines[0], "the failing input was not named"


def test_both_missing_names_both(pf, monkeypatch, capsys):
    with_packages_absent(pf, monkeypatch, {"anthropic"})
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    pf.check_agent_route()

    line = verdict_lines(capsys.readouterr().out)[0]
    assert "anthropic not importable" in line and "ANTHROPIC_API_KEY unset" in line, line


@pytest.mark.parametrize("absent, reports", [({"anthropic"}, "anthropic not importable"),
                                             (set(), "anthropic importable")])
def test_the_dependency_check_does_not_state_the_verdict(pf, monkeypatch, capsys,
                                                         absent, reports):
    """**Which line owns the verdict**, which is the half the note says was
    missing: in §3.2.4's own incident the protection was intact and the
    attribution was wrong, and no test noticed.

    Dependencies reports the package as a fact. It may say the package is needed
    by agent.py; it may not say whether agent.py can run, because it cannot see
    the key. Run in the state where the two used to disagree, and again in the
    opposite one — **both branches, because a check that owns nothing owns
    nothing on the way through as well.** The sweep is what asked for the second
    case: a sabotage putting the verdict back on the success branch was caught
    only by the combination test below, which would have made it invisible the
    moment that test was deleted. Principle 16: each layer carries its own.
    """
    with_packages_absent(pf, monkeypatch, absent)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")

    pf.check_dependencies()

    out = capsys.readouterr().out
    assert reports in out, "it stopped reporting the package at all"
    assert verdict_lines(out) == [], \
        "Dependencies stated the agent.py verdict from half the condition"


def test_the_two_checks_agree_in_every_combination(pf, monkeypatch, capsys):
    """The pair, run together the way main() runs them. Whatever each says, the
    run must not contain two different verdicts about the same subject."""
    for absent, key in (({"anthropic"}, "sk-test"), ({"anthropic"}, None),
                        (set(), "sk-test"), (set(), None)):
        pf.problems.clear()
        pf.warnings.clear()
        with_packages_absent(pf, monkeypatch, absent)
        if key:
            monkeypatch.setenv("ANTHROPIC_API_KEY", key)
        else:
            monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

        pf.check_dependencies()
        pf.check_agent_route()

        lines = verdict_lines(capsys.readouterr().out)
        assert len(lines) == 1, f"absent={absent or 'none'} key={bool(key)}: {lines}"
        expected = "available" if (not absent and key) else "unavailable"
        assert f"agent.py {expected}" in lines[0], f"absent={absent or 'none'} key={bool(key)}: {lines[0]}"


# --------------------------------------------------------------------------
# Finding 1 — the usage block asserted a working directory the code never needed.

def test_it_runs_from_a_directory_that_is_not_the_repo(tmp_path):
    """Run for real, from somewhere else, as the note did. Every path comes from
    `__file__`, so the answer must be the same and must name the real repo.

    The exit code is deliberately not asserted: this runs against whatever
    machine the suite is on, and a missing sibling checkout is a legitimate
    non-zero. What is pinned is that the working directory did not decide it.
    """
    run = subprocess.run([sys.executable, str(PREFLIGHT)], cwd=tmp_path,
                         capture_output=True, text=True, timeout=120)

    out = run.stdout
    assert f"agent repo: {REPO_ROOT}" in out, \
        f"resolved the repo from the working directory rather than from __file__:\n{out[:400]}"
    assert "Dependencies" in out, "it did not get past the first section from a foreign cwd"


def test_the_usage_block_does_not_reinstate_the_cwd_claim():
    """The claim itself, not its replacement wording. A rewrite of the block is
    free; asserting a constraint the code does not have is what cost a reader a
    belief the code could never contradict, because obeying it always works."""
    source = PREFLIGHT.read_text(encoding="utf-8")
    header = source.split('"""')[1]

    assert "from the agent repo root" not in header, \
        "the retired cwd constraint is back in the usage block"
    assert "run this from the agent repo" not in source, \
        "a fix line still tells the reader to change directory"


# ==========================================================================
# The three findings of 2026-09-22, found by sweeping the same file after the
# Brand & Corporate Architect's note. Each was reproduced by running before it
# was changed; each reproduction is the test below it.
#
# These use the real git fixtures, so they are slower than the pure-function
# tests above and they widen what this file touches: a git repo and a local
# bare remote, but still no network and no real checkout. The 2026-08-29
# manual-only ruling was narrowed on 2026-09-22 to exclude verdict composition;
# it is narrowed once more here to exclude git behaviour a fixture can stage.
# What stays manual is the part that reads the OPERATOR'S machine — their real
# checkout, their credentials, their interpreter.

import os
from conftest import _run, plant_post_shape, write_post

# A library that actually answers, for the tests that are about a post being
# refused. conftest's default returns [] by design; a test asserting a refusal
# against it would pass for the wrong reason.
ANSWERING_SHAPE = """\
import re


def sibling_refs(html):
    return [m for m in re.findall(r'(?:src|href)="([^"/:#][^":]*)"', html)]


def shown_outside_code(html):
    return []
"""


def paths_of(blog):
    return {"BLOG_REPO": blog, "CORPUS_REPO": None}


def post_with(blog, site, slug, body):
    write_post(blog, slug, site)
    path = blog / "blog" / slug / "index.html"
    path.write_text(path.read_text(encoding="utf-8").replace("</article>", body + "</article>"),
                    encoding="utf-8")
    return path


# --- Finding 1: READY was a claim about scope, not only about outcome. -----

def test_a_post_whose_references_resolve_beside_it_is_refused(pf, blog, site, capsys):
    """The reproduction. Before 2026-09-22 preflight printed READY and handed over
    the for_real command for a post validate_post refuses outright."""
    plant_post_shape(blog, body=ANSWERING_SHAPE)
    post_with(blog, site, "probe", '<img src="diagram.png" alt="x">')

    pf.check_post(paths_of(blog), "probe")

    out = capsys.readouterr().out
    assert "references resolve beside the post" in out, out
    assert pf.problems, "it did not count as a problem, so the banner still says READY"


def test_the_shape_verdict_matches_validate_posts(pf, blog, site, capsys):
    """Agreement is the point: preflight exists to predict validate_post. Both
    answers come from the same library, so this pins that they stay one answer."""
    import server
    plant_post_shape(blog, body=ANSWERING_SHAPE)
    post_with(blog, site, "probe", '<img src="diagram.png" alt="x">')

    pf.check_post(paths_of(blog), "probe")
    capsys.readouterr()

    assert server.validate_post("probe")["error"] == "sibling"
    assert any("beside the post" in p for p in pf.problems), pf.problems


def test_a_clean_post_says_so_and_does_not_warn(pf, blog, site, capsys):
    """The silent half. A check that never goes quiet stops being read."""
    write_post(blog, "probe", site)

    pf.check_post(paths_of(blog), "probe")

    out = capsys.readouterr().out
    assert "no references resolve beside the post" in out, out
    assert pf.problems == [] and pf.warnings == []


def test_a_checkout_without_the_library_warns_rather_than_failing(pf, blog, site, capsys):
    """A site checkout predating 2026-09-20 has no library. That is a gap in this
    check, never a verdict about the post — §3.0.5, and does waiting fix it? A
    pull does."""
    write_post(blog, "probe", site)
    (blog / "scripts" / "post_shape.py").unlink()

    pf.check_post(paths_of(blog), "probe")

    out = capsys.readouterr().out
    assert "post-shape rule not checked" in out, out
    assert pf.problems == [], "a missing library was reported as a bad post"
    assert pf.warnings, "the gap was not reported at all"


def test_a_library_that_exits_on_import_does_not_end_the_run(pf, blog, site, capsys):
    """SystemExit is not an Exception. The site chat's real version of this killed
    a caller over a defect in a different post."""
    write_post(blog, "probe", site)
    plant_post_shape(blog, body="import sys\nsys.exit(3)\n")

    pf.check_post(paths_of(blog), "probe")

    assert "post-shape rule not checked" in capsys.readouterr().out
    assert pf.problems == []


def test_the_run_names_the_rule_it_did_not_check(pf, blog, site, capsys):
    """The allowlist lives in server.py and cannot be imported here. Naming the
    gap is the honest move; copying the allowlist would be a second definition."""
    write_post(blog, "probe", site)

    pf.check_post(paths_of(blog), "probe")

    assert "not checked here" in capsys.readouterr().out


def test_the_shape_rule_is_imported_rather_than_reimplemented():
    """Principle 18. A copy would drift, and the rule's semantics moved twice in
    three days in September."""
    source = PREFLIGHT.read_text(encoding="utf-8")
    assert "post_shape.py" in source and "sibling_refs" in source
    assert "def sibling_refs" not in source, "preflight grew its own copy of the rule"


def test_the_ready_banner_states_its_scope(blog, corpus, site, tmp_path):
    """A banner is a claim about scope as much as about outcome."""
    write_post(blog, "probe", site)
    env = {**os.environ, "BLOG_REPO": str(blog), "SITE_URL": site,
           "CORPUS_REPO": str(corpus), "SITE_URL": site}

    run = subprocess.run([sys.executable, str(PREFLIGHT), "--slug", "probe"],
                         cwd=tmp_path, capture_output=True, text=True,
                         timeout=120, env=env)

    banner = [l for l in run.stdout.splitlines() if l.startswith("READY")]
    assert len(banner) == 1, run.stdout[-400:]
    assert "every check passed" not in banner[0], \
        f"the banner still claims every check passed about a partly-checked post: {banner[0]}"
    assert "passed every check made here" in banner[0], banner[0]
    assert "validate_post is the authority" in run.stdout, run.stdout[-600:]


# --- Finding 2: a claim about what commit_and_push does. ------------------

def test_the_branch_warning_does_not_claim_your_branch_is_pushed(pf, blog, capsys):
    """commit_and_push pushed the current branch until 2026-09-18; since then it
    builds its commit on main and pushes <sha>:refs/heads/main regardless. The
    warning said otherwise for four days, in the file whose job is to be right
    about what the publish will do."""
    _run(["git", "checkout", "-qb", "draft"], blog)

    pf.check_git(paths_of(blog), push_check=False)

    out = capsys.readouterr().out
    assert "not on main" in out
    assert "pushes the current branch" not in out, \
        "the retired claim about commit_and_push is back"
    assert "still goes to main" in out, out


# --- Finding 3: the push check asked a question it did not mean. ----------

def test_a_remote_that_moved_is_not_reported_as_a_push_failure(pf, blog, blog_remote, tmp_path, capsys):
    """The reproduction, and this is the pipeline's NORMAL state: the site's own
    Actions push to main on every publish. The old bare `git push --dry-run`
    failed here and captioned it 'configure credentials'."""
    other = tmp_path / "other"
    _run(["git", "clone", "-q", str(blog_remote), str(other)], tmp_path)
    _run(["git", "config", "user.email", "t@t"], other)
    _run(["git", "config", "user.name", "t"], other)
    (other / "OTHER").write_text("bot\n", encoding="utf-8")
    _run(["git", "add", "-A"], other)
    _run(["git", "commit", "-qm", "github-actions[bot]: rebuild derivatives"], other)
    _run(["git", "push", "-q", "origin", "main"], other)

    stale = subprocess.run(["git", "push", "--dry-run"], cwd=blog,
                           capture_output=True, text=True)
    assert stale.returncode != 0, \
        "fixture did not diverge: the old check would not have failed here either"

    pf.check_git(paths_of(blog), push_check=True)

    out = capsys.readouterr().out
    assert "BLOG_REPO write access confirmed" in out, out
    assert pf.problems == [], f"a moved remote was reported as a problem: {pf.problems}"


def test_an_unreachable_remote_is_still_a_push_failure(pf, blog, capsys):
    """The other half. A check that cannot fail is not a check."""
    _run(["git", "remote", "set-url", "origin", "/nonexistent/repo.git"], blog)

    pf.check_git(paths_of(blog), push_check=True)

    out = capsys.readouterr().out
    assert "cannot write to BLOG_REPO" in out, out
    assert pf.problems, "an unreachable remote passed the push check"


def test_the_push_check_does_not_name_credentials_as_the_cause(pf, blog, capsys):
    """The 2026-09-18 ruling, which server.py received and this file did not:
    the write-permission hint is reserved for errors that name access."""
    _run(["git", "remote", "set-url", "origin", "/nonexistent/repo.git"], blog)

    pf.check_git(paths_of(blog), push_check=True)

    out = capsys.readouterr().out
    assert "configure credentials;" not in out, \
        "the unconditional credentials caption is back"
    assert "read it rather than assuming" in out, out


def test_the_push_check_leaves_no_ref_on_the_remote(pf, blog, blog_remote, capsys):
    """It probes by dry-running a NEW ref, which is the only push that cannot
    fail on divergence. --dry-run must mean it."""
    pf.check_git(paths_of(blog), push_check=True)
    capsys.readouterr()

    refs = _run(["git", "for-each-ref", "--format=%(refname)"], blog_remote).stdout
    assert "preflight_probe" not in refs, f"the probe ref was really created:\n{refs}"


def test_the_scope_note_survives_a_not_ready_verdict(blog, corpus, site, tmp_path):
    """Found by the test above while it was still wrong itself. The scope note
    lived inside the READY branch, so a run failing for an unrelated reason said
    nothing about who the authority on the post is — and that is the run someone
    is most likely to be staring at. The fix belonged in the code."""
    write_post(blog, "probe", site)
    env = {**os.environ, "BLOG_REPO": str(blog), "SITE_URL": site,
           "CORPUS_REPO": str(tmp_path / "definitely-not-here")}

    run = subprocess.run([sys.executable, str(PREFLIGHT), "--slug", "probe"],
                         cwd=tmp_path, capture_output=True, text=True,
                         timeout=120, env=env)

    assert "NOT READY" in run.stdout, "fixture did not produce the verdict under test"
    assert "validate_post is the authority" in run.stdout, \
        "a NOT READY run said nothing about what it did and did not check"


def test_the_import_leaves_no_bytecode_in_the_operators_checkout(pf, blog, site, capsys):
    """Found by a sabotage that passed. Python caches bytecode beside the source,
    so importing the site's library writes scripts/__pycache__/ into somebody
    else's repo — a diagnostic has no business creating files there, ignored or
    not. server.py's loader has this guarded; preflight's copy of the mechanism
    did not, which is the cost of the mechanism being local even when the rule
    is imported."""
    write_post(blog, "probe", site)
    scripts = blog / "scripts"

    pf.check_post(paths_of(blog), "probe")
    capsys.readouterr()

    assert not (scripts / "__pycache__").exists(), \
        f"the import wrote into the site checkout: {[p.name for p in scripts.iterdir()]}"


# --- The corpus half, added 2026-09-22 with the gate. ---------------------
# preflight asked BLOG_REPO everything git-shaped and CORPUS_REPO nothing, so it
# printed READY — and the for_real command under it — for a corpus the
# registration could not write to. That failure lands at stage 5, post already live.

def both(blog, corpus):
    return {"BLOG_REPO": blog, "CORPUS_REPO": corpus}


def test_a_corpus_that_is_not_a_repo_is_reported(pf, blog, tmp_path, capsys):
    plain = tmp_path / "corpus-plain"
    plain.mkdir()

    pf.check_git(both(blog, plain), push_check=False)

    out = capsys.readouterr().out
    assert "CORPUS_REPO is not a git repository with an 'origin'" in out, out
    assert pf.problems, "a corpus the chain cannot write to passed preflight"


def test_a_healthy_corpus_is_reported_too(pf, blog, corpus, capsys):
    """The silent half: it says what it found rather than going quiet entirely,
    but it raises no problem."""
    pf.check_git(both(blog, corpus), push_check=False)

    out = capsys.readouterr().out
    assert "CORPUS_REPO is a git repository" in out, out
    assert pf.problems == []


def test_a_bad_blog_repo_no_longer_hides_the_corpus(pf, corpus, tmp_path, capsys):
    """Until 2026-09-22 an unresolved BLOG_REPO returned from the whole function,
    so the corpus was never reached and the operator fixed one path per run.
    config_report reports every misconfiguration in one call; this now matches."""
    pf.check_git({"BLOG_REPO": None, "CORPUS_REPO": corpus}, push_check=False)

    out = capsys.readouterr().out
    assert "skipping site repo checks" in out, out
    assert "CORPUS_REPO is a git repository" in out, \
        "the site repo's problem still swallows the corpus check"


def test_push_check_probes_the_corpus_too(pf, blog, corpus, capsys):
    pf.check_git(both(blog, corpus), push_check=True)

    out = capsys.readouterr().out
    assert "CORPUS_REPO write access confirmed" in out, out
    assert "BLOG_REPO write access confirmed" in out, out


def test_the_corpus_probe_leaves_no_ref_behind(pf, blog, corpus, tmp_path, capsys):
    pf.check_git(both(blog, corpus), push_check=True)
    capsys.readouterr()

    refs = _run(["git", "for-each-ref", "--format=%(refname)"],
                tmp_path / "corpus-remote.git").stdout
    assert "preflight_probe" not in refs, refs


# --- The .env report, added 2026-09-22 with the parser fix. ----------------

def test_an_unusable_dotenv_line_warns_instead_of_reporting_ok(pf, monkeypatch, tmp_path, capsys):
    """Until 2026-09-22 a .env whose lines could not be parsed produced a plain
    `[  ok  ] .env present` while its contents were silently discarded, and the
    resolution above it reported the sibling rung with a straight face."""
    import rnv_config
    path = tmp_path / "planted.env"
    path.write_text('BLOG_REPO="/a\n', encoding="utf-8")
    monkeypatch.setattr(rnv_config, "DOTENV_PATH", path)

    pf.check_environment()

    out = capsys.readouterr().out
    assert ".env line unusable" in out, out
    assert "[  ok  ] .env present" not in out, "it still reported the file as fine"
    assert pf.warnings, "the discarded line raised nothing at all"


def test_a_healthy_dotenv_still_reports_present(pf, monkeypatch, tmp_path, capsys):
    """The silent half."""
    import rnv_config
    path = tmp_path / "planted.env"
    path.write_text("SITE_URL=https://rnvizion.dev\n", encoding="utf-8")
    monkeypatch.setattr(rnv_config, "DOTENV_PATH", path)

    pf.check_environment()

    out = capsys.readouterr().out
    assert ".env present" in out and ".env line unusable" not in out, out
