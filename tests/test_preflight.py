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
