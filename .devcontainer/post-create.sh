#!/usr/bin/env bash
set -euo pipefail

pip install --upgrade pip
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
pip install -r "$REPO_ROOT/requirements.txt"

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
