# rnv-publishing (MCP)

A publishing agent for [rnvizion.dev](https://rnvizion.dev). One instruction ships a blog post end to end: it validates the post, commits and pushes, waits for the page to go live, then refreshes the retrieval assistant that answers questions about the site. The post's index card, RSS feed, and social share image are all rendered separately by CI, so the agent stays light.

A language model decides *whether* to publish. Deterministic tools do the work, in a fixed order, and refuse to ship anything broken.

Built on the Model Context Protocol (FastMCP) and the Anthropic API.

---

## The idea

The hard part of an agent isn't getting a model to call tools; it's keeping the failure modes legible when it does. This one draws a hard line between the two kinds of work:

- **Reasoning is the model's job.** It makes a single judgment from a plain-language request: publish this post, or don't; for real, or as a dry run.
- **Execution is the tools' job.** Every step that touches the filesystem, git, or the network is an ordinary Python function with one responsibility and a typed result. No step improvises.

The orchestration itself is code, not a prompt. `publish_post` runs the chain in sequence and stops at the first step that returns `ok: false`. That's deliberate: an earlier version let the model orchestrate the steps freely, and it occasionally skipped one. Moving the sequence into a tool removed that whole class of error. The model's only decision is the one a model is actually good at.

## The chain

`publish_post(slug, for_real)` runs, in order:

1. `validate_post` — checks the post carries everything the feed needs; **stops the publish if a required field is missing**
2. `commit_and_push` — stages and commits the post, then pushes
3. `wait_for_live` — polls the live URL until it returns 200, so nothing downstream runs against a page that hasn't deployed; then confirms the post's `og:image` is reachable and flags it if not
4. `update_corpus` — registers the post with the RAG corpus and triggers a rebuild

A **dry run** (`for_real=false`, the default) runs step 1 and stops, writing and pushing nothing. It confirms the post clears the required-field bar before any real publish.

The post's index card, RSS feed, and Open Graph share image are **not** the agent's job. Two GitHub Actions in the site repo handle them on the push from step 2: `build-feed` regenerates the blog index and `feed.xml` from the posts, and `build-og` renders the share image with Pillow and commits it back. The agent never writes the index, builds a feed, imports an image library, or stages a PNG.

## Refusal as a design value

The agent would rather publish nothing than publish something half-built.

- `validate_post` separates required fields from recommended ones; a missing required field halts the chain with an exact report of what's absent.
- `update_corpus` refuses to register a URL that isn't live, so the assistant's knowledge base never points at a 404.
- `wait_for_live` gates the corpus rebuild behind a confirmed-live page.

Each tool returns `{ ok, ... }`, so the chain reasons about success structurally instead of parsing prose.

One check is deliberately softer than the rest. `wait_for_live` also confirms the post's social image is live, but that's a **warning, not a gate**: the image is rendered by the `build-og` Action a beat after the push, so a slow CI run shouldn't fail an otherwise-good publish. A missing image surfaces in the result as a warning; the post still publishes and the corpus still ingests. It's the one place where blocking would punish the post for CI's timing rather than for being broken.

## The tools

| Tool | Job |
| --- | --- |
| `list_posts` | Enumerate published posts with slug, title, date |
| `validate_post` | Gate: required vs. recommended fields |
| `commit_and_push` | Stage and commit the post, push |
| `wait_for_live` | Poll until the page serves 200, then confirm the `og:image` (warn-only) |
| `update_corpus` | Register the post and trigger a RAG rebuild |
| `publish_post` | Run the whole chain, stopping at the first failure |

## Usage

Run from a Codespace on the site repo, where the agent has native write access:

```bash
# rehearse — validates the post and stops, writes nothing
python agent.py "Publish blog/<slug> as a dry run."

# ship it — runs the full chain
python agent.py "Publish blog/<slug> for real."
```

After a real publish, give the `build-og` Action a minute or two to finish before sharing the post anywhere that unfurls a link preview (LinkedIn, etc.), so the share card has an image to scrape.

## How it fits together

The agent edits the **site** repo directly and pushes natively. The heavier work runs in CI instead, where it belongs:

- **The index, feed, and share image.** On the push, two Actions in the site repo regenerate the blog index and RSS feed from the posts (`build-feed`) and render the per-post Open Graph image (`build-og`), each committing its output back. Image rendering, font handling, and HTML generation never touch the publishing environment.
- **The RAG rebuild.** `update_corpus` commits a one-line source change to the corpus repo, and a GitHub Action there re-ingests and pushes the vector store to a Hugging Face Space. The heavy ML dependencies and the Hugging Face token stay in CI, never in the publishing environment.

```
agent.py  ──drives──►  server.py (FastMCP: 6 tools)
                            │
        ┌───────────────────┼────────────────────┐
   site repo            live site (poll)     corpus repo
   (Pages)                                   → Action → HF Space
      │
      └─ on push → build-feed (index + feed) + build-og (OG image), committed back
```

## Setup

Environment:

- `ANTHROPIC_API_KEY` — the reasoning model
- `BLOG_REPO` — path to the site checkout (default `/workspaces/rnvizion.github.io`)
- `CORPUS_REPO` — path to the corpus checkout (default `/workspaces/rnv-ask-the-corpus`)
- `SITE_URL` — live origin for `wait_for_live` (default `https://rnvizion.dev`)

Dependencies: the Anthropic SDK and the MCP SDK. Install with `pip install -r requirements.txt`. The agent does no image, feed, or HTML rendering, so Pillow and the like are not dependencies here — that work lives in the site repo's build workflows.

## What this demonstrates

For anyone reading this as work rather than docs:

- **Agentic design that's safe by construction.** The model holds one decision; the pipeline is deterministic code. Failures are typed and stop the chain, not silent.
- **MCP as a tool layer.** A clean FastMCP server with small, single-purpose, idempotent tools that compose.
- **Integrity gating.** "Refuse to ship broken" is enforced in code, at three points, not left to a prompt.
- **The right work in the right place.** Index and feed generation, image rendering, and ML rebuilds all run in CI; the agent stays a light, legible orchestrator. The division is the design.
- **Real automation on a real system.** It extends an existing static site, feed, and retrieval assistant; it isn't a toy built to demo the pattern.

---

Built by Christian Smith ([RNVizion](https://rnvizion.dev)).
