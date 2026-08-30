import asyncio
import os
import sys
from pathlib import Path
from anthropic import Anthropic
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client, get_default_environment

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


MODEL = "claude-sonnet-4-6"   # the publish chain is short but multi-step; Sonnet keeps the one decision reliable
llm = Anthropic()             # reads ANTHROPIC_API_KEY from the environment

SYSTEM = (
    "You are a publishing assistant for a blog. To publish a post, call "
    "publish_post(slug, for_real). Use for_real=false (a dry run) by default; use "
    "for_real=true ONLY when the user explicitly says to publish for real. "
    "publish_post runs the full chain itself (validate, and on a real run: commit "
    "and push, wait for live, update the RAG corpus) and stops at the first failure; "
    "read back its result and trace in plain language. The blog index, RSS feed, and "
    "OG image are built by GitHub Actions on the push, not by the agent. The "
    "individual tools (validate_post, commit_and_push, wait_for_live, update_corpus, "
    "list_posts) remain available if the user asks for a single step."
)

def to_anthropic(mcp_tools):
    return [{"name": t.name, "description": t.description or "", "input_schema": t.inputSchema}
            for t in mcp_tools.tools]

async def run(request):
    # Absolute path: the child is spawned with the caller's cwd, so a relative
    # "server.py" only resolves when the agent happens to be run from the repo root.
    server_path = str(Path(__file__).resolve().parent / "server.py")
    params = StdioServerParameters(
        command=sys.executable, args=[server_path], env=server_env()
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = to_anthropic(await session.list_tools())
            messages = [{"role": "user", "content": request}]

            while True:
                resp = llm.messages.create(
                    model=MODEL, max_tokens=1024, system=SYSTEM,
                    tools=tools, messages=messages,
                )
                for block in resp.content:
                    if block.type == "text":
                        print("\nCLAUDE:", block.text)
                if resp.stop_reason != "tool_use":
                    break
                messages.append({"role": "assistant", "content": resp.content})
                results = []
                for block in resp.content:
                    if block.type == "tool_use":
                        print(f"  → {block.name}({block.input})")
                        out = await session.call_tool(block.name, block.input)
                        text = "\n".join(b.text for b in out.content if getattr(b, "type", "") == "text")
                        results.append({"type": "tool_result", "tool_use_id": block.id, "content": text})
                messages.append({"role": "user", "content": results})

if __name__ == "__main__":
    request = sys.argv[1] if len(sys.argv) > 1 else "Publish the post at blog/squish/ as a dry run."
    asyncio.run(run(request))
