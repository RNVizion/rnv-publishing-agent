#!/usr/bin/env bash
set -euo pipefail

pip install --upgrade pip
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
pip install -r "$REPO_ROOT/requirements.txt"

# The site repo's build tooling, installed here on purpose rather than by accident.
#
# This Codespace clones rnvizion.github.io below and is where its scripts get run by
# hand. scripts/build_feed.py imports bs4 and parses with the lxml backend explicitly,
# so it cannot start without both; it is the sole generator of feed.xml,
# blog/index.html, sitemap.xml, and robots.txt. build-feed.yml installs them in CI.
#
# Until they arrived here as a side effect: this repo's requirements.txt
# still listed beautifulsoup4 and lxml, though nothing in this repo imports either.
# Removing those unused entries is what surfaced the gap — a hand-run of build_feed.py
# was only ever working because a neighbouring repo's dependency list happened to
# cover it. Same shape as the BLOG_REPO bug in agent.py: configured-looking because an
# unrelated default satisfied it. Named explicitly now, with the consumer stated, so
# the next person can see whose dependency this is and why it lives here.
pip install beautifulsoup4 lxml

clone_or_update () {
  local slug="$1"
  local dir="/workspaces/${slug##*/}"
  if [ -d "$dir/.git" ]; then
    git -C "$dir" fetch --all --prune
    git -C "$dir" checkout main
    git -C "$dir" pull --ff-only origin main
  else
    git clone "https://github.com/${slug}.git" "$dir"
  fi
}

clone_or_update "RNVizion/rnvizion.github.io"
clone_or_update "RNVizion/rnv-ask-the-corpus"

echo "post-create: site and corpus repos ready under /workspaces"
