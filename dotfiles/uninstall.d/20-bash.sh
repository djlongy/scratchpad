#!/usr/bin/env bash
set -euo pipefail

# Reverses dotfiles/install.d/20-bash.sh. Removes the managed ~/.bashrc block and
# the framework files this repository put in place, and deliberately does NOT
# touch ~/.oh-my-bash/custom — anything you wrote yourself lives there.

OSH_DIR="$HOME/.oh-my-bash"
BLESH_DIR="$HOME/.local/share/blesh"
BASHRC="$HOME/.bashrc"

BEGIN_MARK='# >>> dotfiles bash (managed) >>>'
END_MARK='# <<< dotfiles bash (managed) <<<'

if [ -f "$BASHRC" ] && grep -qF "$BEGIN_MARK" "$BASHRC"; then
  stripped="$(mktemp)"
  awk -v b="$BEGIN_MARK" -v e="$END_MARK" '
    $0 == b { skip = 1; next }
    $0 == e { skip = 0; next }
    !skip
  ' "$BASHRC" >"$stripped"
  cat "$stripped" >"$BASHRC"
  rm -f "$stripped"
  echo "RC    removed managed block from $BASHRC"
else
  echo "RC    no managed block in $BASHRC"
fi

if [ -d "$OSH_DIR" ]; then
  for entry in "$OSH_DIR"/* "$OSH_DIR"/.[!.]*; do
    [ -e "$entry" ] || continue
    [ "$(basename "$entry")" = "custom" ] && continue
    rm -rf "$entry"
  done
  if [ -d "$OSH_DIR/custom" ]; then
    echo "OMB   removed framework files; kept $OSH_DIR/custom"
  else
    rmdir "$OSH_DIR" 2>/dev/null || true
    echo "OMB   removed $OSH_DIR"
  fi
fi

if [ -d "$BLESH_DIR" ]; then
  rm -rf "$BLESH_DIR"
  echo "BLESH removed $BLESH_DIR"
fi

echo "Open a new shell to drop the prompt."
