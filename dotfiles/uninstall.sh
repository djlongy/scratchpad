#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if ! command -v stow >/dev/null 2>&1; then
  echo "ERROR: stow is required. Install GNU Stow first."
  exit 1
fi

echo "Removing stow package: tmux"
stow --target "$HOME" --dir "$SCRIPT_DIR" -D tmux
echo "Done."
