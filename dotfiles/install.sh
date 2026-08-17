#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PACKAGE="tmux"

# Prefer GNU Stow when present. On distros where stow is not in the base
# repos (e.g. RHEL without EPEL), fall back to plain symlinks so the scripts
# in .local/bin still land — without them, the cheatsheet keybind and tmx-dev
# have nothing to run.
if command -v stow >/dev/null 2>&1; then
  echo "Applying stow package: $PACKAGE"
  stow --target "$HOME" --dir "$SCRIPT_DIR" "$PACKAGE"
  echo "Done."
  exit 0
fi

echo "stow not found — linking files directly instead."
cd "$SCRIPT_DIR/$PACKAGE"
find . -type f | while read -r rel; do
  rel="${rel#./}"
  src="$SCRIPT_DIR/$PACKAGE/$rel"
  dest="$HOME/$rel"
  mkdir -p "$(dirname "$dest")"
  if [ -e "$dest" ] && [ ! -L "$dest" ]; then
    echo "SKIP  $dest exists and is not a symlink (move it aside and re-run)"
    continue
  fi
  ln -sfn "$src" "$dest"
  echo "LINK  $dest -> $src"
done
echo "Done (symlink fallback)."
