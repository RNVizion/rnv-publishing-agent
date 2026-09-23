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


def _parse_dotenv(text: str) -> tuple[dict[str, str], list[str]]:
    """Parse a .env file. Returns (values, problems).

    Hand-rolled on purpose: the alternative is a runtime dependency for twenty
    lines, in a repo that removed two unused ones and parses HTML with `re` by
    choice.

    Handles: comments, blank lines, an optional `export ` prefix, and single or
    double quoted values. Quoting matters more than it looks — a Windows path can
    contain spaces, and an unquoted parser would truncate
    C:/Users/John Smith/rnv/... at the space.

    EVERY SKIPPED LINE IS REPORTED, and that is the fix underneath the three
    defects of 2026-09-22 rather than one of them. Until then this loop `continue`d
    past anything it did not understand, so a .env the operator had written and a
    .env that had been silently discarded produced identical output — resolution
    fell to the sibling rung and reported *that* rung honestly, which is the worst
    possible combination: a confident provenance for an answer the operator did not
    configure. Principle 6, in the one file whose whole job is to say where a value
    came from.

    THE QUOTED-VALUE BUG, kept as a comment because the old shape looks right.
    It asked `value[0] == value[-1]` of the WHOLE remainder, comment included. With
    `BLOG_REPO="/real/site"   # my checkout` the last character is `t`, so the
    quoted branch was not taken, the else-branch stripped the comment, and the
    quotes survived into the value. `Path('"/real/site"')` is not absolute, so it
    was joined onto the agent repo and reported as *"relative to the agent repo"* —
    an absolute path the operator wrote, described back to them as a relative one
    they did not. `.env.example` quotes every example assignment and tells you to
    quote paths with spaces, so uncommenting one and adding a note is the natural
    gesture. A value is now quoted if it STARTS with a quote; the matching close
    ends it and anything after is a comment.

    Escapes are deliberately not supported — no \" inside a double-quoted value —
    and never were. A path needing one has bigger problems; say so rather than
    half-implementing it.
    """
    values: dict[str, str] = {}
    problems: list[str] = []
    for n, raw in enumerate(text.splitlines(), 1):
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        m = _LINE.match(raw)
        if not m:
            problems.append(f"line {n} is not KEY=value and was ignored: "
                            f"{raw.strip()[:40]!r}")
            continue
        key, value = m.group(1), m.group(2).strip()
        if value[:1] in ("'", '"'):
            quote = value[0]
            close = value.find(quote, 1)
            if close == -1:
                problems.append(f"line {n} ({key}) opens a {quote} and never closes "
                                f"it; the line was ignored")
                continue
            value = value[1:close]
        else:
            value = value.split(" #", 1)[0].strip()   # unquoted: trailing comment
        if not value:
            problems.append(f"line {n} ({key}) has an empty value and was ignored")
            continue
        values[key] = value
    return values, problems


def parse_dotenv(text: str) -> dict[str, str]:
    """The values only. `dotenv_problems()` reports what was skipped."""
    return _parse_dotenv(text)[0]


def _read_dotenv() -> tuple[dict[str, str], list[str]]:
    """Read and parse DOTENV_PATH. Returns (values, problems); never raises.

    Read per call rather than cached: the file is tiny, the call count is a handful
    per publish, and a cache would make the module's behaviour depend on import
    order during tests.

    `utf-8-sig` rather than `utf-8`, and it is load-bearing on this operator's
    platform. A UTF-8 BOM is not whitespace to `re` and is not removed by `strip()`,
    so with plain utf-8 the FIRST assignment failed the line regex and was dropped
    while every later line parsed — a .env that half-works, which is the worst shape
    for diagnosis. Notepad and VS Code both offer "UTF-8 with BOM". `utf-8-sig`
    strips a BOM when present and is a no-op when it is not.

    UnicodeDecodeError is caught explicitly because it subclasses ValueError, NOT
    OSError, so the old `except OSError` did not catch it and it escaped through
    the public API — killing `tools/preflight.py`, the stdlib-only diagnostic whose
    entire job is to name the cause, with a traceback instead of a diagnosis.
    PowerShell's `>` and `Out-File` write UTF-16 by default, so this is one
    redirect away on the platform the operator publishes from.
    """
    try:
        if not DOTENV_PATH.is_file():
            return {}, []
        text = DOTENV_PATH.read_text(encoding="utf-8-sig")
    except UnicodeDecodeError as e:
        return {}, [f"{DOTENV_PATH} is not UTF-8 ({e.reason}) and was ignored "
                    f"entirely; PowerShell's > and Out-File write UTF-16 — re-save "
                    f"it as UTF-8"]
    except OSError as e:
        return {}, [f"{DOTENV_PATH} could not be read ({e.strerror or e}) and was "
                    f"ignored entirely"]
    return _parse_dotenv(text)


def dotenv_values() -> dict[str, str]:
    """What the .env configures. {} if it is absent, unreadable, or empty."""
    return _read_dotenv()[0]


def dotenv_problems() -> list[str]:
    """What the .env said that could not be used, in the operator's own words.

    A separate function rather than a second return value, because every caller of
    dotenv_values() wants the values and exactly one caller wants this. It re-reads,
    which costs nothing the per-call decision above has not already accepted.
    """
    return _read_dotenv()[1]


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
    # Anything the .env said that could not be used. Carried here so it travels
    # with the resolution rather than needing its own call: a wrong value and the
    # reason it is wrong belong in the same report.
    problems = dotenv_problems()
    if problems:
        out[".env"] = {"value": str(DOTENV_PATH), "from": "unusable content",
                       "exists": "; ".join(problems)}
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
