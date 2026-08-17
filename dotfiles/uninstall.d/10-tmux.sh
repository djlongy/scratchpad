#!/usr/bin/env bash
set -euo pipefail

# Undo install.d/10-tmux.sh.
#
# Conservative by design: it removes a plugin directory only when that directory
# carries the marker file the installer wrote, so anything installed by hand is
# reported and left in place.
#
# Nothing here touches tmux itself — the installer never put it there.

DOTFILES_DIR="${DOTFILES_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"

PLUGIN_DEST="$HOME/.tmux/plugins"

PLUGINS=(tpm tmux-sensible tmux-resurrect tmux-continuum tmux-sessionx)

MARKER=".installed-by-dotfiles"

# Saved sessions live in ~/.local/share/tmux/resurrect (tmux-resurrect's
# default save dir), outside these directories, and are deliberately left alone.
for plugin in "${PLUGINS[@]}"; do
  dest="$PLUGIN_DEST/$plugin"
  if [ ! -e "$dest" ]; then
    continue
  fi
  if [ ! -f "$dest/$MARKER" ]; then
    echo "SKIP  $dest was not installed from vendor/ — leaving it"
    continue
  fi
  rm -rf "$dest"
  echo "RM    $dest"
done

rmdir "$PLUGIN_DEST" "$HOME/.tmux" 2>/dev/null || true
