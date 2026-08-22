#!/usr/bin/env python3
"""
migrate_pipeline_fixes.py — one pass over rnv-publishing-agent.

Built from: RNVizion/rnv-publishing-agent @ main, read 20 Aug 2026

Applies four unrelated fixes in one working tree, by explicit instruction
(2026-08-21). AI Engineering Practices rev 2 §1 "Splitting scripts" says
unrelated concerns are separate commits; this bundles them deliberately, and
the relaxation is logged rather than silent. Each step is independently gated
and independently skippable, so the bundle can still be unpicked.

  1  tests/       client_test.py -> tests/test_publish_gate.py, rewritten as a
                  real guard. The old file called insert_card, which is not one
                  of the server's six tools, and printed instead of asserting,
                  so it could never fail.
  2  env          agent.py: forward BLOG_REPO / CORPUS_REPO / SITE_URL to the
                  server subprocess, and launch it with sys.executable.
                  mcp.client.stdio hands the child a filtered environment
                  (HOME, LOGNAME, PATH, SHELL, TERM, USER), so those three
                  documented variables never reached the server.
  3  devcontainer post-create.sh: derive the repo root instead of hardcoding
                  /workspaces/publishing-agent (the repo is now
                  rnv-publishing-agent), and clone the corpus to the path
                  server.py actually defaults to.
  4  gitignore    Add .gitignore and untrack the committed __pycache__.

The script never commits. It leaves the tree for review; the single commit is
yours. Step 4 stages one deletion, because untracking cannot be expressed in
the working tree alone — it says so when it does.

Usage:
    python migrate_pipeline_fixes.py --check     # writes nothing
    python migrate_pipeline_fixes.py
    python migrate_pipeline_fixes.py --only env,gitignore

Exit codes:
    0  clean — every selected step applied or already present
    1  disagreement — an assertion failed, or the base is not what was expected
    2  could not run — wrong directory, git unavailable
    3  ran but incomplete — some steps applied, at least one skipped
"""

import argparse
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path.cwd()
STEPS = ("tests", "env", "devcontainer", "gitignore")

applied, already, skipped, left_alone = [], [], [], []


# ---------------------------------------------------------------- helpers

def say(mark, msg):
    print(f"  {mark} {msg}")


def git(*args):
    return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True)


def sub_once(text, old, new, where):
    """Exact-string replace that refuses ambiguity. §1: an edit matching more
    than once aborts rather than guessing."""
    n = text.count(old)
    if n == 0:
        raise LookupError(f"{where}: anchor not found -> {old[:60]!r}")
    if n > 1:
        raise LookupError(f"{where}: anchor matched {n} times, refusing to guess -> {old[:60]!r}")
    return text.replace(old, new, 1)


# ---------------------------------------------------------------- fingerprint

def fingerprint():
    """Refuse an unexpected base. A half-applied script is worse than a
    refused one."""
    problems = []
    if not (ROOT / "server.py").is_file():
        problems.append("server.py is not at the repo root — wrong directory, or the "
                        "scripts/ move was already applied")
    if not (ROOT / "agent.py").is_file():
        problems.append("agent.py is not at the repo root")
    if not (ROOT / ".devcontainer" / "post-create.sh").is_file():
        problems.append(".devcontainer/post-create.sh is missing")
    if not (ROOT / "requirements.txt").is_file():
        problems.append("requirements.txt is missing")
    if git("rev-parse", "--git-dir").returncode != 0:
        problems.append("not a git repository")
    return problems


# ---------------------------------------------------------------- step 1

GUARD = '''"""Guard: the for_real gate holds, and a dry run writes nothing.

Replaces client_test.py, which called insert_card — a tool the server does not
expose — and printed its results instead of asserting them, so it reported the
same output whether the server was healthy or gone.

What this guards (Publishing Systems principle 7, gate the irreversible):
  1. The server exposes exactly the six expected tools.
  2. publish_post(for_real=False) stops after validate_post. No commit, no
     push, no corpus write appears in the trace.
  3. The blog checkout is byte-unchanged after a dry run.
  4. validate_post fails closed on a slug that does not exist.

Run from the repo root:  python tests/test_publish_gate.py
Reads BLOG_REPO if set; otherwise the server's own default applies.
"""

import asyncio
import os
import subprocess
import sys
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client, get_default_environment

SERVER = Path(__file__).resolve().parent.parent / "server.py"
PASS_THROUGH = ("BLOG_REPO", "CORPUS_REPO", "SITE_URL")

EXPECTED_TOOLS = {
    "list_posts", "validate_post", "commit_and_push",
    "wait_for_live", "update_corpus", "publish_post",
}
WRITE_STEPS = {"commit_and_push", "wait_for_live", "update_corpus"}

failures = []


def check(condition, label):
    print(f"  {'PASS' if condition else 'FAIL'}  {label}")
    if not condition:
        failures.append(label)


def server_env():
    env = get_default_environment()
    for key in PASS_THROUGH:
        value = os.environ.get(key)
        if value:
            env[key] = value
    return env


def blog_repo():
    return Path(os.environ.get("BLOG_REPO", "/workspaces/rnvizion.github.io"))


def tree_state():
    """Porcelain status of the blog checkout, or None if it is not a git tree."""
    repo = blog_repo()
    if not (repo / ".git").exists():
        return None
    out = subprocess.run(["git", "status", "--porcelain"], cwd=repo,
                         capture_output=True, text=True)
    return out.stdout if out.returncode == 0 else None


async def main():
    params = StdioServerParameters(
        command=sys.executable, args=[str(SERVER)], env=server_env()
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()

            names = {t.name for t in (await session.list_tools()).tools}
            check(names == EXPECTED_TOOLS,
                  f"tool surface is the expected six (got {sorted(names)})")

            posts = await session.call_tool("list_posts", {})
            slug = first_slug(posts.content)
            check(bool(slug), f"picked a live slug to test against ({slug or 'none found'})")
            if not slug:
                print("\\n  cannot continue without a post — is BLOG_REPO pointing at "
                      "the site checkout?")
                return

            before = tree_state()
            result = await session.call_tool(
                "publish_post", {"slug": slug, "for_real": False}
            )
            body = result.content[0].text

            check('"dry_run": true' in body.lower(),
                  "publish_post(for_real=False) reports itself as a dry run")
            leaked = sorted(s for s in WRITE_STEPS if f'"step": "{s}"' in body)
            check(not leaked,
                  f"no write step in the dry-run trace (leaked: {leaked or 'none'})")

            after = tree_state()
            check(before == after,
                  "blog checkout unchanged by the dry run"
                  + ("" if before is not None else " (skipped: not a git tree)"))

            missing = await session.call_tool(
                "validate_post", {"slug": "definitely-not-a-real-slug"}
            )
            check('"ok": false' in missing.content[0].text.lower(),
                  "validate_post fails closed on an unknown slug")


def first_slug(blocks):
    """list_posts returns one content block per post, each a JSON object —
    not a single JSON array. Handle both rather than assume."""
    import json
    for block in blocks:
        try:
            data = json.loads(getattr(block, "text", ""))
        except (json.JSONDecodeError, TypeError):
            continue
        if isinstance(data, dict) and data.get("slug"):
            return data["slug"]
        if isinstance(data, list) and data and data[0].get("slug"):
            return data[0]["slug"]
    return ""


if __name__ == "__main__":
    print(f"guard: publish gate  (server: {SERVER})")
    asyncio.run(main())
    print()
    if failures:
        print(f"FAILED: {len(failures)} check(s) -> {failures}")
        sys.exit(1)
    print("all checks passed")
'''


def step_tests(check_only):
    old = ROOT / "client_test.py"
    new = ROOT / "tests" / "test_publish_gate.py"

    if new.is_file() and not old.exists():
        already.append("tests")
        say("=", "tests/test_publish_gate.py already in place")
        return True
    if not old.is_file():
        skipped.append("tests")
        say("!", "client_test.py not found and no guard present — skipped")
        return False
    if "insert_card" not in old.read_text(encoding="utf-8"):
        skipped.append("tests")
        say("!", "client_test.py does not look like the known dead file — skipped, "
                 "review it by hand")
        return False

    if check_only:
        say("~", f"would move client_test.py -> tests/test_publish_gate.py and rewrite "
                 f"it as a guard ({len(GUARD.splitlines())} lines)")
        applied.append("tests")
        return True

    (ROOT / "tests").mkdir(exist_ok=True)
    if git("ls-files", "--error-unmatch", "client_test.py").returncode == 0:
        git("mv", "client_test.py", "tests/test_publish_gate.py")
    else:
        old.rename(new)
    new.write_text(GUARD, encoding="utf-8")
    compile(new.read_text(encoding="utf-8"), str(new), "exec")  # assert it parses
    applied.append("tests")
    say("+", "client_test.py -> tests/test_publish_gate.py, rewritten as a real guard")
    return True


# ---------------------------------------------------------------- step 2

ENV_BLOCK = '''
# mcp.client.stdio hands the child a filtered environment — HOME, LOGNAME, PATH,
# SHELL, TERM, USER and nothing else — so BLOG_REPO, CORPUS_REPO and SITE_URL
# never reached server.py and its hardcoded defaults always won. BLOG_REPO only
# looked configured because its default matched the Codespace clone path.
# sys.executable rather than "python": under a venv the two are not the same
# interpreter, and the child needs the one that has mcp installed.
PASS_THROUGH = ("BLOG_REPO", "CORPUS_REPO", "SITE_URL")


def server_env():
    env = get_default_environment()
    for key in PASS_THROUGH:
        value = os.environ.get(key)
        if value:
            env[key] = value
    return env

'''


def step_env(check_only):
    path = ROOT / "agent.py"
    text = path.read_text(encoding="utf-8")

    if "def server_env()" in text:
        already.append("env")
        say("=", "agent.py already forwards the publishing env vars")
        return True

    try:
        new = sub_once(
            text,
            "from mcp.client.stdio import stdio_client\n",
            "from mcp.client.stdio import stdio_client, get_default_environment\n",
            "agent.py import",
        )
        new = sub_once(new, "import asyncio\nimport sys\n",
                       "import asyncio\nimport os\nimport sys\n", "agent.py stdlib imports")
        new = sub_once(new, '\nMODEL = "claude-sonnet-4-6"',
                       ENV_BLOCK + '\nMODEL = "claude-sonnet-4-6"', "agent.py env block")
        new = sub_once(
            new,
            'params = StdioServerParameters(command="python", args=["server.py"])',
            'params = StdioServerParameters(\n'
            '        command=sys.executable, args=["server.py"], env=server_env()\n'
            '    )',
            "agent.py launcher",
        )
    except LookupError as exc:
        skipped.append("env")
        say("!", f"{exc} — skipped, agent.py has moved on from the known shape")
        return False

    if check_only:
        say("~", "would add server_env() to agent.py and pass env= / sys.executable")
        applied.append("env")
        return True

    compile(new, str(path), "exec")
    path.write_text(new, encoding="utf-8")
    applied.append("env")
    say("+", "agent.py forwards BLOG_REPO / CORPUS_REPO / SITE_URL, launches via sys.executable")
    return True


# ---------------------------------------------------------------- step 3

def step_devcontainer(check_only):
    path = ROOT / ".devcontainer" / "post-create.sh"
    text = path.read_text(encoding="utf-8")
    changes = []

    if "REPO_ROOT=" in text:
        already.append("devcontainer")
        say("=", "post-create.sh already derives its own repo root")
        return True

    try:
        new = sub_once(
            text,
            "pip install -r /workspaces/publishing-agent/requirements.txt\n",
            'REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"\n'
            'pip install -r "$REPO_ROOT/requirements.txt"\n',
            "post-create.sh requirements path",
        )
        changes.append("requirements path derived, not hardcoded (survives the next rename)")
        new = sub_once(
            new,
            'clone_or_update "RNVizion/ask-the-corpus"',
            'clone_or_update "RNVizion/rnv-ask-the-corpus"',
            "post-create.sh corpus slug",
        )
        changes.append("corpus clones to /workspaces/rnv-ask-the-corpus, matching server.py's default")
    except LookupError as exc:
        skipped.append("devcontainer")
        say("!", f"{exc} — skipped")
        return False

    if check_only:
        say("~", "would fix post-create.sh: " + "; ".join(changes))
        applied.append("devcontainer")
        return True

    path.write_text(new, encoding="utf-8")
    applied.append("devcontainer")
    for c in changes:
        say("+", f"post-create.sh: {c}")
    say("i", "existing Codespaces keep a stale /workspaces/ask-the-corpus until you "
             "rebuild the container or remove it by hand")
    return True


# ---------------------------------------------------------------- step 4

GITIGNORE = """__pycache__/
*.py[cod]
.pytest_cache/
.coverage
htmlcov/
.venv/
venv/
.vs/
.vscode/
.idea/
.DS_Store
"""


def step_gitignore(check_only):
    path = ROOT / ".gitignore"
    tracked = [p for p in git("ls-files").stdout.splitlines()
               if "__pycache__" in p or p.endswith(".pyc")]

    have_ignore = path.is_file() and "__pycache__" in path.read_text(encoding="utf-8")
    if have_ignore and not tracked:
        already.append("gitignore")
        say("=", ".gitignore covers __pycache__ and nothing stale is tracked")
        return True

    if check_only:
        if not have_ignore:
            say("~", f"would write .gitignore ({len(GITIGNORE.splitlines())} entries)")
        if tracked:
            say("~", f"would untrack {len(tracked)} file(s): {tracked}")
        applied.append("gitignore")
        return True

    if not have_ignore:
        path.write_text(GITIGNORE, encoding="utf-8")
        say("+", ".gitignore written")
    if tracked:
        git("rm", "-r", "--cached", "--quiet", "__pycache__")
        say("+", f"staged the untracking of {len(tracked)} file(s): {tracked}")
        say("i", "this is the one staged change — untracking cannot live in the "
                 "working tree alone")
    say("i", "a .gitignore fixes the future and does nothing about history; the "
             "blobs stay in past commits")
    applied.append("gitignore")
    return True


# ---------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--check", action="store_true", help="dry run; writes nothing")
    ap.add_argument("--only", default="", help=f"comma-separated subset of {','.join(STEPS)}")
    args = ap.parse_args()

    selected = [s.strip() for s in args.only.split(",") if s.strip()] or list(STEPS)
    unknown = [s for s in selected if s not in STEPS]
    if unknown:
        print(f"unknown step(s): {unknown}. Choose from {', '.join(STEPS)}.")
        return 2

    print(f"\nrnv-publishing-agent migration{'  [--check, nothing written]' if args.check else ''}")
    print(f"repo: {ROOT}\n")

    problems = fingerprint()
    if problems:
        print("refusing to run — the base is not what this script was written against:")
        for p in problems:
            say("x", p)
        print("\nNothing was written.")
        return 1
    say("=", "base fingerprint matches (server.py, agent.py, post-create.sh, git)")
    print()

    runners = {"tests": step_tests, "env": step_env,
               "devcontainer": step_devcontainer, "gitignore": step_gitignore}
    for name in STEPS:
        if name not in selected:
            left_alone.append(f"{name} (not selected)")
            continue
        print(f"[{name}]")
        runners[name](args.check)
        print()

    # what was deliberately not touched
    left_alone.extend([
        "server.py — unchanged; it is the system, not a script, and stays at the root",
        "agent.py's location — unchanged, same reason",
        "requirements.txt — pytest not added; the guard runs as a plain script",
        "the six tool implementations — no behaviour changed, only how the child is launched",
    ])
    print("left alone, deliberately:")
    for item in left_alone:
        say("-", item)

    print("\nsummary")
    say("+", f"applied: {applied or 'none'}")
    say("=", f"already present: {already or 'none'}")
    say("!", f"skipped: {skipped or 'none'}")
    print("\nNothing was committed. Review with `git status` and `git diff`, then commit.")
    if not args.check:
        print("Then run the guard:  python tests/test_publish_gate.py")

    if skipped:
        return 3
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:  # noqa: BLE001
        print(f"\ncould not run: {type(exc).__name__}: {exc}")
        sys.exit(2)
