#!/usr/bin/env bash
set -euo pipefail

# tmux component installer — fully offline.
#
# Copies the vendored plugin trees into ~/.tmux/plugins. Every input ships in
# this repo under vendor/, so there is no download, no package manager, and no
# git call anywhere below.
#
# tmux itself is NOT shipped here: the target hosts provide it, from their OS
# package repositories. This script only reports when it is missing.
#
# Run by install.sh, which exports DOTFILES_DIR. The fallback keeps the script
# usable on its own.

DOTFILES_DIR="${DOTFILES_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
PLUGIN_SRC="$DOTFILES_DIR/vendor/tmux-plugins"
PLUGIN_DEST="$HOME/.tmux/plugins"

PLUGINS=(tpm tmux-sensible tmux-resurrect tmux-continuum tmux-sessionx)

# Dropped into each plugin tree we copy. The uninstall hook removes a directory
# only when it finds this file, so a plugin someone installed by hand survives.
MARKER=".installed-by-dotfiles"

# ---------------------------------------------------------------------------
# Plugins. Copied, not symlinked: tmux runs each plugin's *.tmux file as an
# executable and plugins write state next to themselves, so they want to be
# real directories under $HOME.
#
# These go down whether or not tmux is installed yet — they are inert files,
# and laying them down now means tmux works the first time it is started.
# ---------------------------------------------------------------------------
mkdir -p "$PLUGIN_DEST"
for plugin in "${PLUGINS[@]}"; do
  src="$PLUGIN_SRC/$plugin"
  dest="$PLUGIN_DEST/$plugin"

  if [ ! -d "$src" ]; then
    echo "SKIP  plugin $plugin: nothing vendored at $src"
    continue
  fi
  if [ -e "$dest" ] && [ ! -f "$dest/$MARKER" ]; then
    echo "SKIP  $dest exists and did not come from vendor/ (move it aside to refresh)"
    continue
  fi

  # Replace wholesale so a removed upstream file cannot linger between versions.
  rm -rf "$dest"
  cp -a "$src" "$dest"
  : >"$dest/$MARKER"
  echo "PLUG  $dest"
done

# ---------------------------------------------------------------------------
# tmux itself comes from the OS. Say so once, actionably, if it is absent.
# fzf is deliberately not checked here: .tmux.conf already decides at runtime
# whether to load sessionx, based on whether fzf resolves then.
# ---------------------------------------------------------------------------
if ! command -v tmux >/dev/null 2>&1; then
  echo "NOTE  tmux not found on PATH — install the tmux package from your OS repositories; the plugins above are already in place and will load the first time you start it"
fi
