#!/usr/bin/env bash
set -euo pipefail

# Mirror image of install.sh: drop the config symlinks, then let each
# uninstall.d/*.sh undo what the matching install.d/*.sh put in place.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONFIG_PACKAGES=(tmux)

# Fallback for hosts without stow. Removes only symlinks that point back into
# this repo, so a real file at the same path is never touched.
unlink_package() {
  local package="$1"
  cd "$SCRIPT_DIR/$package"
  find . -type f | while read -r rel; do
    rel="${rel#./}"
    local src="$SCRIPT_DIR/$package/$rel"
    local dest="$HOME/$rel"
    if [ -L "$dest" ] && [ "$(readlink "$dest")" = "$src" ]; then
      rm -f "$dest"
      echo "UNLINK $dest"
    elif [ -e "$dest" ]; then
      echo "SKIP   $dest is not a symlink into this repo"
    fi
  done
  cd "$SCRIPT_DIR"
}

# 1) Config packages: GNU Stow when present, plain unlinking otherwise.
for package in "${CONFIG_PACKAGES[@]}"; do
  if command -v stow >/dev/null 2>&1; then
    echo "Removing stow package: $package"
    stow --target "$HOME" --dir "$SCRIPT_DIR" -D "$package"
  else
    echo "stow not found — unlinking package '$package' directly."
    unlink_package "$package"
  fi
done

# 2) Component uninstallers, ordered by filename like install.d/.
export DOTFILES_DIR="$SCRIPT_DIR"
for hook in "$SCRIPT_DIR"/uninstall.d/*.sh; do
  [ -f "$hook" ] || continue
  echo "== $(basename "$hook") =="
  bash "$hook"
done

echo "Done."
