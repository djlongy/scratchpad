#!/usr/bin/env bash
set -euo pipefail

# Nerd Font component installer — fully offline.
#
# Copies the vendored MesloLGS Nerd Font faces out of vendor/fonts/meslolgs-nf.
# Every input ships in this repo, so there is no download and no package manager
# call anywhere below.
#
# Read this before wondering why it so often skips: the glyphs in the powerline
# prompt and the tmux status line are drawn by the TERMINAL, on whatever machine
# you are sitting at. A font on a server you SSH into is never consulted. This
# hook exists for the case where the machine running it IS the machine with the
# screen — a workstation, or a host with a local console — and it deliberately
# does nothing on a headless box. Windows clients are served by
# ../windows/install-nerd-font.ps1 instead.
#
# Run by install.sh, which exports DOTFILES_DIR. The fallback keeps the script
# usable on its own.

DOTFILES_DIR="${DOTFILES_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
FONT_SRC="$DOTFILES_DIR/vendor/fonts/meslolgs-nf"

FONT_FILES=(
  MesloLGSNerdFont-Regular.ttf
  MesloLGSNerdFont-Bold.ttf
  MesloLGSNerdFont-Italic.ttf
  MesloLGSNerdFont-BoldItalic.ttf
)

# Content comparison, for the "already installed" case. cmp ships in diffutils,
# which a minimal image does not always carry, and sha256sum is coreutils on
# Linux but shasum on macOS — so try each and, with none of them, copy rather
# than guess.
same_file() {
  local src="$1" dest="$2" tool
  [ -f "$dest" ] || return 1
  if command -v cmp >/dev/null 2>&1; then
    cmp -s "$src" "$dest"
    return
  fi
  for tool in sha256sum shasum; do
    if command -v "$tool" >/dev/null 2>&1; then
      [ "$("$tool" <"$src" | cut -d' ' -f1)" = "$("$tool" <"$dest" | cut -d' ' -f1)" ]
      return
    fi
  done
  return 1
}

if [ ! -d "$FONT_SRC" ]; then
  echo "SKIP  fonts: nothing vendored at $FONT_SRC"
  exit 0
fi

case "$(uname -s)" in
  Darwin)
    # CoreText reads this directory directly and keeps its own cache, so there
    # is nothing to gate on and nothing to rebuild. fontconfig is optional here
    # and refreshed below only when it happens to be installed.
    FONT_DEST="$HOME/Library/Fonts"
    ;;
  *)
    # No fontconfig means nothing on this host can select a font by name, which
    # in practice means no local display — the fonts would be dead weight.
    if ! command -v fc-cache >/dev/null 2>&1; then
      echo "SKIP  fonts skipped — no fontconfig (headless host); they matter on the CLIENT side"
      exit 0
    fi
    # Its own subdirectory, so the uninstall hook has an unambiguous scope.
    FONT_DEST="$HOME/.local/share/fonts/NerdFonts"
    ;;
esac

mkdir -p "$FONT_DEST"

installed=0
for file in "${FONT_FILES[@]}"; do
  src="$FONT_SRC/$file"
  dest="$FONT_DEST/$file"

  if [ ! -f "$src" ]; then
    echo "SKIP  font $file: not vendored at $src"
    continue
  fi
  # Compare contents so a re-run is silent and a version bump still lands.
  if same_file "$src" "$dest"; then
    echo "SAME  $dest"
    continue
  fi

  cp -f "$src" "$dest"
  chmod 644 "$dest"
  echo "FONT  $dest"
  installed=$((installed + 1))
done

# Rebuild only this directory's cache; a full fc-cache -f is slow and rewrites
# caches this hook has no business touching.
if command -v fc-cache >/dev/null 2>&1; then
  if [ "$installed" -gt 0 ]; then
    fc-cache -f "$FONT_DEST" >/dev/null
    echo "CACHE fc-cache refreshed for $FONT_DEST"
  fi
fi

echo "NOTE  set your terminal font to 'MesloLGS Nerd Font' — the setting lives in the terminal you are looking at, not on the remote host"
