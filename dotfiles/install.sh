#!/usr/bin/env bash
set -euo pipefail

# Fully offline installer. Everything it needs ships in this repo (vendor/),
# so it must never reach for a network, a package manager, or git.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONFIG_PACKAGES=(tmux)

link_package() {
  local package="$1"
  cd "$SCRIPT_DIR/$package"
  find . -type f | while read -r rel; do
    rel="${rel#./}"
    local src="$SCRIPT_DIR/$package/$rel"
    local dest="$HOME/$rel"
    mkdir -p "$(dirname "$dest")"
    if [ -e "$dest" ] && [ ! -L "$dest" ]; then
      echo "SKIP  $dest exists and is not a symlink (move it aside and re-run)"
      continue
    fi
    ln -sfn "$src" "$dest"
    echo "LINK  $dest -> $src"
  done
  cd "$SCRIPT_DIR"
}

# 1) Config packages: GNU Stow when present, plain symlinks otherwise. Both
#    paths produce the same links; stow is a convenience, not a requirement.
for package in "${CONFIG_PACKAGES[@]}"; do
  if command -v stow >/dev/null 2>&1; then
    echo "Applying stow package: $package"
    stow --target "$HOME" --dir "$SCRIPT_DIR" "$package"
  else
    echo "stow not found — linking package '$package' directly."
    link_package "$package"
  fi
done

# 2) Component installers: each install.d/*.sh copies its vendored payload
#    (plugins, frameworks) into place. Ordered by filename.
export DOTFILES_DIR="$SCRIPT_DIR"
for hook in "$SCRIPT_DIR"/install.d/*.sh; do
  [ -f "$hook" ] || continue
  echo "== $(basename "$hook") =="
  bash "$hook"
done

echo "Done. Open a new shell (or: source ~/.bashrc) to pick everything up."
