"""The MCP layer itself: tool surface and the gate, over the real stdio transport.

Every other test in this suite imports `server` and calls its functions directly, which
skips the protocol entirely. This one spawns the server as a subprocess and talks to it
the way a client does, so it covers the one layer the rest cannot: registration. A tool
accidentally unregistered, renamed, or dropped from the decorator would pass every other
test in this repo and fail here.

This is the pytest form of the standing publish-gate guard (previously
`tests/test_publish_gate.py`, which pytest could not collect because it was a print-and-
check script rather than a test module — so CI reported green whether or not it ever
ran). The standalone script survives at `tools/publish_gate_check.py`, where it still
does the thing this cannot: run against the real site checkout.
"""
import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client, get_default_environment

from conftest import write_post

SERVER = Path(__file__).resolve().parent.parent / "server.py"
PASS_THROUGH = ("BLOG_REPO", "CORPUS_REPO", "SITE_URL")

EXPECTED_TOOLS = {
    "list_posts", "validate_post", "commit_and_push",
    "wait_for_live", "update_corpus", "publish_post",
}
WRITE_STEPS = {"commit_and_push", "wait_for_live", "update_corpus"}


def _server_env():
    env = get_default_environment()
    for key in PASS_THROUGH:
        value = os.environ.get(key)
        if value:
            env[key] = value
    return env


async def _session(fn):
    params = StdioServerParameters(command=sys.executable, args=[str(SERVER)],
                                   env=_server_env())
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            return await fn(session)


def _text(result):
    return "\n".join(b.text for b in result.content if getattr(b, "type", "") == "text")


@pytest.fixture
def mcp_call(blog, site):
    """Run one coroutine against a live server subprocess pointed at the fixture blog."""
    def run(fn):
        return asyncio.run(_session(fn))
    return run


def test_tool_surface_is_exactly_the_expected_six(mcp_call):
    """Registration is the layer only this test covers."""
    async def go(session):
        return {t.name for t in (await session.list_tools()).tools}

    assert mcp_call(go) == EXPECTED_TOOLS


def test_every_tool_carries_a_description(mcp_call):
    """A tool with no description is a tool a model will misuse."""
    async def go(session):
        return {t.name: (t.description or "").strip() for t in (await session.list_tools()).tools}

    for name, description in mcp_call(go).items():
        assert description, f"{name} has no description for a client to read"


def test_dry_run_over_the_protocol_leaks_no_write_step(mcp_call, blog, site):
    """The gate, asserted through the transport rather than in-process."""
    write_post(blog, "ready", site)
    before = subprocess.run(["git", "status", "--porcelain"], cwd=blog,
                            capture_output=True, text=True, check=True).stdout

    async def go(session):
        return _text(await session.call_tool("publish_post",
                                             {"slug": "ready", "for_real": False}))

    body = mcp_call(go)
    payload = json.loads(body)

    assert payload["dry_run"] is True
    leaked = sorted(s for s in WRITE_STEPS if any(t["step"] == s for t in payload["trace"]))
    assert not leaked, f"write steps present in a dry-run trace: {leaked}"

    after = subprocess.run(["git", "status", "--porcelain"], cwd=blog,
                           capture_output=True, text=True, check=True).stdout
    assert before == after, "the dry run changed the checkout"


def test_validate_fails_closed_on_an_unknown_slug(mcp_call):
    async def go(session):
        return _text(await session.call_tool("validate_post",
                                             {"slug": "definitely-not-a-real-slug"}))

    assert json.loads(mcp_call(go))["ok"] is False
