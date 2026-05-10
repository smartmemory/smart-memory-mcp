#!/usr/bin/env bash
# Install repo git hooks via symlink. Idempotent. Refuses to clobber user-customized hooks.
#
# Usage: ./scripts/install-git-hooks.sh
set -euo pipefail

cd "$(dirname "$0")/.."
REPO_ROOT="$(pwd)"
HOOK_SRC_DIR="$REPO_ROOT/scripts/hooks"
HOOK_DST_DIR="$REPO_ROOT/.git/hooks"

if [ ! -d "$HOOK_DST_DIR" ]; then
  echo "Error: $HOOK_DST_DIR does not exist (not a git checkout?)" >&2
  exit 1
fi

shopt -s nullglob
for src in "$HOOK_SRC_DIR"/*; do
  name="$(basename "$src")"
  dst="$HOOK_DST_DIR/$name"

  if [ -L "$dst" ]; then
    current="$(readlink "$dst")"
    if [ "$current" = "$src" ] || [ "$current" = "../../scripts/hooks/$name" ]; then
      echo "  ✓ $name already installed (symlink)"
      continue
    fi
    echo "  ⚠ $name: existing symlink points elsewhere ($current); leaving in place"
    continue
  fi

  if [ -e "$dst" ] && [ ! -f "$dst.sample" ]; then
    # User-authored or third-party hook present (not a sample). Don't clobber.
    echo "  ⚠ $name: existing hook found at $dst; leaving in place"
    continue
  fi

  if [ -f "$dst" ]; then
    # File exists but only the .sample; safe to replace
    rm -f "$dst"
  fi

  ln -s "../../scripts/hooks/$name" "$dst"
  chmod +x "$src"
  echo "  ✓ $name installed"
done

echo "Done."
