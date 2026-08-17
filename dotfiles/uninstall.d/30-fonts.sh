#!/usr/bin/env bash
set -euo pipefail

# Undo install.d/30-fonts.sh.
#
# Removes exactly the four files the installer copied, by name, from the
# directory it copied them to. On macOS that directory is ~/Library/Fonts, which
# holds fonts from elsewhere too, so it is never removed — only those four files
# are. On Linux the installer owns ~/.local/share/fonts/NerdFonts outright, so
# that directory goes as well once it is empty.

DOTFILES_DIR="${DOTFILES_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"

FONT_FILES=(
  MesloLGSNerdFont-Regular.ttf
  MesloLGSNerdFont-Bold.ttf
  MesloLGSNerdFont-Italic.ttf
  MesloLGSNerdFont-BoldItalic.ttf
)

case "$(uname -s)" in
  Darwin)
    FONT_DEST="$HOME/Library/Fonts"
    OWN_DIR=0
    CACHE_DIR="$FONT_DEST"
    ;;
  *)
    FONT_DEST="$HOME/.local/share/fonts/NerdFonts"
    OWN_DIR=1
    # The installer's directory is removed below, so the cache is rebuilt one
    # level up — the level that still exists and still lists it.
    CACHE_DIR="$HOME/.local/share/fonts"
    ;;
esac

removed=0
for file in "${FONT_FILES[@]}"; do
  dest="$FONT_DEST/$file"
  [ -e "$dest" ] || continue
  rm -f "$dest"
  echo "RM    $dest"
  removed=$((removed + 1))
done

if [ "$OWN_DIR" -eq 1 ]; then
  rmdir "$FONT_DEST" 2>/dev/null || true
fi

# Leave a stale cache entry behind and fontconfig still offers a font that is
# gone; applications then fall back mid-render instead of at selection time.
if [ "$removed" -gt 0 ] && command -v fc-cache >/dev/null 2>&1 && [ -d "$CACHE_DIR" ]; then
  fc-cache -f "$CACHE_DIR" >/dev/null 2>&1 || true
  echo "CACHE fc-cache refreshed for $CACHE_DIR"
fi
