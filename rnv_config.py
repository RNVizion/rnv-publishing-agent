"""
rnv_config.py — where the chain's three paths come from, and how it knows.

WHY THIS IS ITS OWN MODULE
  Two consumers need the same answer: server.py, which uses the paths, and
  tools/preflight.py, which diagnoses them. An earlier draft had preflight keep
  its own copy of the defaults and a guard warning when the two drifted. A guard
  against drift is a confession that there are two definitions. There is now one,
  and both import it.

  It is deliberately STDLIB-ONLY, with no dependency on mcp or anything else, so
  preflight can import it before `pip install -r requirements.txt` has run.

HOW A PATH IS RESOLVED
  1. the environment          — explicit, always wins; the escape hatch for any layout
  2. .env beside this file    — per-machine, gitignored, never committed
  3. a sibling of this repo   — the normal case, and the reason this works untouched
                                on a fresh machine
  4. nothing                  — reported with every path that was tried

  Rung 3 is the important one. The three repos are checked out next to each
  other: /workspaces/{rnv-publishing-agent,rnvizion.github.io,rnv-ask-the-corpus}
  in a Codespace, ~/rnv/{...} on a laptop, anywhere else the same. The absolute
  path differs per machine; the RELATIONSHIP does not. Deriving the siblings from
  this file's own location makes the common case need no configuration at all,
  and retires the hardcoded /workspaces defaults that used to be silently wrong
  on every machine that was not a Codespace.

  This is the same move the repo already makes twice: agent.py resolving
  server.py from __file__, and post-create.sh deriving REPO_ROOT from BASH_SOURCE.

PROVENANCE
  Every resolution reports where the value came from, so a failure can say
  "BLOG_REPO is unset; looked for a sibling at <path> and found nothing" instead
  of naming a path with no account of why it was chosen.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

AGENT_ROOT = Path(__file__).resolve().parent
WORKSPACE_ROOT = AGENT_ROOT.parent

# Module-level so tests can point it somewhere harmless; a developer's local
# .env must never change what the suite asserts.
DOTENV_PATH = AGENT_ROOT / ".env"

# The repo each variable names, as cloned next to this one.
SIBLING = {
    "BLOG_REPO": "rnvizion.github.io",
    "CORPUS_REPO": "rnv-ask-the-corpus",
}
DEFAULT_SITE_URL = "https://rnvizion.dev"

_LINE = re.compile(r"""^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$""")


def parse_dotenv(text: str) -> dict[str, str]:
    """Parse a .env file. Hand-rolled on purpose: the alternative is a runtime
    dependency for twenty lines, in a repo that just removed two unused ones and
    parses HTML with `re` by choice.

    Handles: comments, blank lines, an optional `export ` prefix, and single or
    double quoted values. Quoting matters more than it looks — a Windows path can
    contain spaces, and an unquoted parser would truncate
    C:/Users/John Smith/rnv/... at the space.
    """
    out: dict[str, str] = {}
    for raw in text.splitlines():
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        m = _LINE.match(raw)
        if not m:
            continue
        key, value = m.group(1), m.group(2).strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]          # quoted: take it verbatim
        else:
            value = value.split(" #", 1)[0].strip()   # unquoted: trailing comment
        if value:
            out[key] = value
    return out


def dotenv_values() -> dict[str, str]:
    """Read DOTENV_PATH, or {} if it isn't there. Read per call rather than cached:
    the file is tiny, the call count is a handful per publish, and a cache would
    make the module's behaviour depend on import order during tests."""
    try:
        if DOTENV_PATH.is_file():
            return parse_dotenv(DOTENV_PATH.read_text(encoding="utf-8"))
    except OSError:
        pass
    return {}


def _raw(var: str) -> tuple[str | None, str]:
    """The configured value and where it came from, before any path handling."""
    if os.environ.get(var):
        return os.environ[var], "environment"
    got = dotenv_values().get(var)
    if got:
        return got, f".env ({DOTENV_PATH})"
    return None, ""


def resolve_path(var: str) -> tuple[Path, str]:
    """Resolve one of the repo path variables. Returns (path, provenance).

    A relative value resolves against the agent repo root, never the working
    directory — the same reason agent.py stopped resolving server.py against CWD.
    It also makes a .env portable between machines: `BLOG_REPO=../my-site` means
    the same thing wherever the checkout lives.
    """
    value, how = _raw(var)
    if value:
        p = Path(value)
        if not p.is_absolute():
            p = (AGENT_ROOT / p).resolve()
            how += ", relative to the agent repo"
        return p, how
    return WORKSPACE_ROOT / SIBLING[var], f"sibling of the agent repo in {WORKSPACE_ROOT}"


def resolve_site_url() -> tuple[str, str]:
    value, how = _raw("SITE_URL")
    if value:
        return value.rstrip("/"), how
    return DEFAULT_SITE_URL, "built-in default"


def describe() -> dict[str, dict[str, str]]:
    """Every resolution with its provenance, for diagnostics."""
    out: dict[str, dict[str, str]] = {}
    for var in SIBLING:
        path, how = resolve_path(var)
        out[var] = {"value": str(path), "from": how, "exists": str(path.is_dir())}
    url, how = resolve_site_url()
    out["SITE_URL"] = {"value": url, "from": how, "exists": "n/a"}
    return out


if __name__ == "__main__":
    # A library first, and imported by both server.py and tools/preflight.py, so
    # there is no step in any runbook that "runs rnv_config". But resolution is
    # load-bearing and was otherwise invisible: the only way to ask what a machine
    # would resolve, and from which rung, was to run the whole preflight. This is
    # the smallest possible answer to "where is it actually looking?", and it needs
    # no dependencies, so it works on a bare interpreter before anything is installed.
    import json
    print(f"agent repo    : {AGENT_ROOT}")
    print(f"sibling search: {WORKSPACE_ROOT}")
    print(f".env          : {DOTENV_PATH}"
          f"{'' if DOTENV_PATH.is_file() else '   (not present — fine; siblings are the normal case)'}")
    print()
    print(json.dumps(describe(), indent=2))
