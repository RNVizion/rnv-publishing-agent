"""
Guard: the for_real gate holds, and a dry run writes nothing.

Replaces client_test.py, which called insert_card — a tool the server does not
expose — and printed its results instead of asserting them, so it reported the
same output whether the server was healthy or gone.

What this guards (Publishing Systems principle 7, gate the irreversible):
  1. The server exposes exactly the six expected tools.
  2. publish_post(for_real=False) stops after validate_post. No commit, no
     push, no corpus write appears in the trace.
  3. The blog checkout is byte-unchanged after a dry run.
  4. validate_post fails closed on a slug that does not exist.

The working directory does not matter; paths are anchored to this file.
Invoke by whatever path reaches it:

    python tests/test_publish_gate.py                 # from the repo root
    python /workspaces/rnv-publishing-agent/tests/test_publish_gate.py

BLOG_REPO is forwarded to the server if set; otherwise the server's default applies. 
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
                print("\n  cannot continue without a post — is BLOG_REPO pointing at "
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
