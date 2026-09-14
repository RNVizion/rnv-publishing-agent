"""The thesis tests.

The claim this project makes is that an agent should refuse rather than guess, and
that consequential actions are opt-in. These tests exist so that claim is checked by
a build instead of asserted in a README. If any test in this file fails, the project
is no longer doing the thing it says it does.
"""
from conftest import git_log, porcelain, write_post


def test_validate_halts_on_missing_required_field(blog, site, srv):
    """A post missing required metadata does not pass validation."""
    write_post(blog, "half-written", site, complete=False)

    result = srv.validate_post("half-written")

    assert result["ok"] is False
    assert "article:published_time" in result["missing_required"]
    assert "<article> block" in result["missing_required"]


def test_validate_passes_a_complete_post(blog, site, srv):
    result = srv.validate_post("ready")  # not written yet
    assert result["ok"] is False, "a post that does not exist must not validate"

    write_post(blog, "ready", site)
    result = srv.validate_post("ready")

    assert result["ok"] is True
    assert result["missing_required"] == []


def test_missing_recommended_field_does_not_block(blog, site, srv):
    """Recommended is advice, not a gate — the distinction has to hold."""
    post = write_post(blog, "sparse", site)
    post.write_text(post.read_text(encoding="utf-8").replace(
        '<meta property="og:description" content="A post that clears the bar.">', ""),
        encoding="utf-8")

    result = srv.validate_post("sparse")

    assert result["ok"] is True
    assert "og:description" in result["missing_recommended"]


def test_publish_stops_at_validate_and_writes_nothing(blog, site, srv):
    """The whole argument in one test: the chain refuses, and refusing costs nothing."""
    write_post(blog, "half-written", site, complete=False)
    before = git_log(blog)

    result = srv.publish_post("half-written", for_real=True)

    assert result["ok"] is False
    assert result["stopped_at"] == "validate_post"
    assert [step["step"] for step in result["trace"]] == ["validate_post"], \
        "nothing after validation may run once validation fails"
    assert git_log(blog) == before, "a refused publish must not commit"


def test_dry_run_is_the_default_and_writes_nothing(blog, site, srv):
    """for_real defaults to False; a default call must never touch the repo."""
    write_post(blog, "ready", site)
    before = git_log(blog)

    result = srv.publish_post("ready")

    assert result["ok"] is True
    assert result["dry_run"] is True
    assert [step["step"] for step in result["trace"]] == ["validate_post"]
    assert git_log(blog) == before, "a dry run must not commit"
    assert "??" in porcelain(blog) or "ready" in porcelain(blog), \
        "the post should still be sitting uncommitted in the working tree"


def test_commit_dry_run_reports_without_writing(blog, site, srv):
    write_post(blog, "ready", site)
    before = git_log(blog)

    result = srv.commit_and_push("ready", dry_run=True)

    assert result["ok"] is True
    assert result["committed"] is False
    assert result["would_commit"] == ["blog/ready/index.html"]
    assert git_log(blog) == before


def test_every_subprocess_call_declares_its_stdin():
    """Every subprocess in server.py must pass stdin explicitly.

    Bought with an incident, 2026-09-14. `capture_output=True` reads as total and is
    not: it redirects stdout and stderr and says nothing about stdin, which stays
    INHERITED. Under the MCP stdio transport server.py's stdin is the pipe the client
    writes JSON-RPC into, so every git call was handed a live transport pipe as its
    standard input. `git status --porcelain` — a local read — took 278.6 seconds and
    returned only when the parent process was interrupted.

    Why this is a source check rather than a behavioural one. The failure needs a real
    stdio transport to appear at all: the direct route has no pipe, so calling
    publish_post in-process is exactly the configuration that cannot reproduce it, and
    a test that spawned a transport would be timing-dependent and slow. The property
    that actually matters is structural and is decidable by reading the file, so read
    the file. A test asserting "this hangs" would have to wait to find out; this one
    knows immediately.

    `ast` rather than a regex on purpose: a regex over source cannot tell a call from
    the string that describes it, and this module's docstrings discuss `subprocess.run`
    by name. Use and mention, again.
    """
    import ast
    import inspect
    import pathlib

    import server

    source = pathlib.Path(inspect.getfile(server)).read_text(encoding="utf-8")
    tree = ast.parse(source)

    calls = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in {"run", "Popen", "call", "check_call", "check_output"}
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "subprocess"
    ]

    assert calls, "found no subprocess calls in server.py — the check is looking in the wrong place"

    missing = [
        node.lineno for node in calls
        if not any(kw.arg == "stdin" for kw in node.keywords)
    ]

    assert not missing, (
        f"subprocess call(s) at line(s) {missing} in server.py do not pass stdin. "
        "capture_output=True does not cover stdin; an inherited stdin under the MCP "
        "stdio transport is a live pipe and git will block on it indefinitely. "
        "Pass stdin=subprocess.DEVNULL."
    )
