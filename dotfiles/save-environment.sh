#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
OUT_FILE="$SCRIPT_DIR/environment-snapshot.txt"

{
  echo "Environment snapshot"
  echo "Generated: $(date -u '+%Y-%m-%dT%H:%M:%SZ')"
  echo
  echo "System"
  uname -srm || true
  echo
  echo "Tools"
  printf 'bash: ' && bash --version | head -n 1 || true
  printf 'tmux: ' && tmux -V || true
  printf 'git:  ' && git --version || true
  printf 'stow: ' && stow --version | head -n 1 || true
  printf 'fzf:  ' && fzf --version | head -n 1 || true
} > "$OUT_FILE"

echo "Wrote $OUT_FILE"
echo "Review and sanitize if needed before commit."
