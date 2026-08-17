#!/usr/bin/env bash
set -euo pipefail

# Installs Oh My Bash + the devops-powerline prompt from the vendored trees in
# vendor/, plus the git shortcut functions from bash/git-functions/. Every path
# it reads is inside this package, so the extracted bundle is the whole
# dependency: no network, no package manager, no git, no make. Safe to re-run —
# the managed ~/.bashrc block is replaced in place and ~/.oh-my-bash/custom is
# left alone.
#
# ble.sh (inline autosuggestions) is optional and OFF by default, matching the
# standalone deploy script. Enable with either:
#   WITH_BLESH=1 bash 20-bash.sh
#   bash 20-bash.sh --with-blesh

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DOTFILES_DIR="${DOTFILES_DIR:-$(cd "$SCRIPT_DIR/.." && pwd)}"

THEME_NAME="devops-powerline"
VENDOR_OMB="$DOTFILES_DIR/vendor/oh-my-bash"
VENDOR_BLESH="$DOTFILES_DIR/vendor/blesh"
# Both payloads ship inside the package. The overrides exist for a checkout that
# keeps them somewhere else — they are not needed by the bundle.
THEME_SRC="${BASH_THEME_SRC:-$DOTFILES_DIR/bash/theme/$THEME_NAME/$THEME_NAME.theme.bash}"
GIT_FUNCTIONS_SRC="${BASH_GIT_FUNCTIONS_SRC:-$DOTFILES_DIR/bash/git-functions/git-functions.bash}"

OSH_DIR="$HOME/.oh-my-bash"
BLESH_DIR="$HOME/.local/share/blesh"
BASHRC="$HOME/.bashrc"

BEGIN_MARK='# >>> dotfiles bash (managed) >>>'
END_MARK='# <<< dotfiles bash (managed) <<<'

# First line of the copy dropped into custom/. uninstall.d/20-bash.sh removes the
# file only when it starts with this, so a hand-written file of the same name is
# never deleted.
GIT_FUNCTIONS_DEST="$OSH_DIR/custom/git-functions.sh"
GIT_FUNCTIONS_MARK='# >>> dotfiles git-functions (managed) >>>'

WITH_BLESH="${WITH_BLESH:-0}"
for arg in "$@"; do
  case "$arg" in
    --with-blesh) WITH_BLESH=1 ;;
    --without-blesh) WITH_BLESH=0 ;;
    *) echo "20-bash: unknown argument '$arg'" >&2; exit 2 ;;
  esac
done

[ -d "$VENDOR_OMB" ] || { echo "20-bash: missing vendored tree $VENDOR_OMB" >&2; exit 1; }
[ -f "$VENDOR_OMB/oh-my-bash.sh" ] || { echo "20-bash: $VENDOR_OMB is not an Oh My Bash tree" >&2; exit 1; }
[ -f "$THEME_SRC" ] || { echo "20-bash: theme not found at $THEME_SRC (set BASH_THEME_SRC)" >&2; exit 1; }
[ -f "$GIT_FUNCTIONS_SRC" ] || { echo "20-bash: git functions not found at $GIT_FUNCTIONS_SRC (set BASH_GIT_FUNCTIONS_SRC)" >&2; exit 1; }

# --- Oh My Bash framework -------------------------------------------------
# Refresh every top-level entry from the vendor tree except custom/, which holds
# user content once the first install has run.
mkdir -p "$OSH_DIR"
for entry in "$VENDOR_OMB"/* "$VENDOR_OMB"/.[!.]*; do
  [ -e "$entry" ] || continue
  name="$(basename "$entry")"
  case "$name" in
    custom|cache|log) continue ;;
  esac
  rm -rf "${OSH_DIR:?}/$name"
  cp -a "$entry" "$OSH_DIR/$name"
done

# custom/ is seeded once from upstream, then never overwritten.
if [ -d "$OSH_DIR/custom" ]; then
  echo "KEEP  $OSH_DIR/custom (existing user content preserved)"
elif [ -d "$VENDOR_OMB/custom" ]; then
  cp -a "$VENDOR_OMB/custom" "$OSH_DIR/custom"
  echo "SEED  $OSH_DIR/custom (from vendor)"
else
  echo "SEED  $OSH_DIR/custom (empty — vendor tree ships no custom/)"
fi

# Runtime scratch dirs. Oh My Bash falls back to XDG paths when these are absent
# or not owned by the caller, so create them explicitly and own them.
mkdir -p "$OSH_DIR/cache" "$OSH_DIR/log" "$OSH_DIR/custom/themes/$THEME_NAME"

# cp -a preserves ownership when the caller is root, which would leave the tree
# owned by whoever owns the repository and defeat Oh My Bash's own -O checks.
chown -R "$(id -u):$(id -g)" "$OSH_DIR" 2>/dev/null || true

echo "OMB   $OSH_DIR (vendored)"

install -m 0644 "$THEME_SRC" "$OSH_DIR/custom/themes/$THEME_NAME/$THEME_NAME.theme.bash"
echo "THEME $OSH_DIR/custom/themes/$THEME_NAME/"

# --- git shortcut functions ----------------------------------------------
# Oh My Bash sources $OSH_CUSTOM/*.{sh,bash} on every interactive start
# (vendor/oh-my-bash/oh-my-bash.sh:140), and does it after the plugins, so the
# functions here win over any same-named plugin alias. Dropping the file in is
# the whole wiring — no extra line in ~/.bashrc.
if [ -f "$GIT_FUNCTIONS_DEST" ] && ! head -n 1 "$GIT_FUNCTIONS_DEST" | grep -qF "$GIT_FUNCTIONS_MARK"; then
  echo "KEEP  $GIT_FUNCTIONS_DEST (already exists without our marker — left untouched)"
  echo "WARN  Move it aside and re-run to install the packaged copy."
else
  {
    printf '%s\n' "$GIT_FUNCTIONS_MARK"
    printf '%s\n' '# Copied by install.d/20-bash.sh from bash/git-functions/. The line'
    printf '%s\n' '# above is what uninstall.d/20-bash.sh matches before removing this file, so'
    printf '%s\n' '# keep it. Edits here are lost on the next install — change the packaged copy.'
    printf '%s\n' ''
    cat "$GIT_FUNCTIONS_SRC"
  } >"$GIT_FUNCTIONS_DEST.tmp"
  chmod 0644 "$GIT_FUNCTIONS_DEST.tmp"
  mv "$GIT_FUNCTIONS_DEST.tmp" "$GIT_FUNCTIONS_DEST"
  echo "GITFN $GIT_FUNCTIONS_DEST"
fi

# --- ble.sh (optional) ----------------------------------------------------
# ble.sh refuses to load unless this exact set of POSIX tools is on PATH, and it
# says so on stderr at EVERY shell start. Since an air-gapped host cannot install
# the missing package, check first and leave ble.sh out rather than wire a broken
# line into ~/.bashrc. Source: _ble_init_posix_command_list in vendor/blesh/ble.sh.
BLESH_REQUIRES="sed date rm mkdir mkfifo sleep stty tty sort awk chmod grep cat wc mv sh od cp ps"

if [ "$WITH_BLESH" = "1" ]; then
  missing=""
  for cmd in $BLESH_REQUIRES; do
    command -v "$cmd" >/dev/null 2>&1 || missing="$missing $cmd"
  done

  if [ -n "$missing" ]; then
    echo "WARN  ble.sh needs these commands and they are not on PATH:$missing"
    echo "WARN  Loading it anyway would print an error at every shell start, so"
    echo "WARN  ble.sh is being SKIPPED. Install the packages providing them"
    echo "WARN  (procps-ng covers 'ps') and re-run with --with-blesh."
    WITH_BLESH=0
  elif [ ! -f "$VENDOR_BLESH/ble.sh" ]; then
    echo "20-bash: --with-blesh but $VENDOR_BLESH/ble.sh is missing" >&2
    exit 1
  else
    mkdir -p "$(dirname "$BLESH_DIR")"
    rm -rf "${BLESH_DIR:?}"
    cp -a "$VENDOR_BLESH" "$BLESH_DIR"
    chown -R "$(id -u):$(id -g)" "$BLESH_DIR" 2>/dev/null || true
    echo "BLESH $BLESH_DIR (vendored prebuilt — nothing compiled)"
  fi
else
  echo "BLESH skipped (pass --with-blesh or WITH_BLESH=1 to enable)"
fi

# --- managed ~/.bashrc block ---------------------------------------------
touch "$BASHRC"
[ -f "$BASHRC.pre-dotfiles" ] || cp "$BASHRC" "$BASHRC.pre-dotfiles"

# An earlier hand-rolled install would source Oh My Bash a second time. Say so
# rather than editing lines this script did not write.
if grep -q 'oh-my-bash\.sh' "$BASHRC" && ! grep -qF "$BEGIN_MARK" "$BASHRC"; then
  echo "WARN  $BASHRC already sources Oh My Bash outside the managed block."
  echo "WARN  Remove that older block by hand or the framework loads twice."
fi

stripped="$(mktemp)"
awk -v b="$BEGIN_MARK" -v e="$END_MARK" '
  $0 == b { skip = 1; next }
  $0 == e { skip = 0; next }
  !skip
' "$BASHRC" >"$stripped"

{
  cat "$stripped"
  printf '%s\n' "$BEGIN_MARK"
  printf '%s\n' '# Written by dotfiles/install.d/20-bash.sh. Edits between the markers are'
  printf '%s\n' '# overwritten on the next run — put your own settings outside them.'
  printf '%s\n' ''
  printf '%s\n' '# Vendored binaries land in ~/.local/bin. Add it here so they resolve in'
  printf '%s\n' '# shells that never read ~/.bash_profile, and guard so repeated sourcing'
  printf '%s\n' '# of this file cannot stack duplicate entries.'
  printf '%s\n' 'case ":$PATH:" in'
  printf '%s\n' '  *":$HOME/.local/bin:"*) ;;'
  printf '%s\n' '  *) PATH="$HOME/.local/bin:$PATH" ;;'
  printf '%s\n' 'esac'
  printf '%s\n' ''

  if [ "$WITH_BLESH" = "1" ]; then
    printf '%s\n' '# ble.sh loads before the framework and attaches at the end of the block.'
    printf '%s\n' '[[ $- == *i* ]] && source -- "$HOME/.local/share/blesh/ble.sh" --attach=none'
    printf '%s\n' ''
  fi

  printf '%s\n' 'export OSH="$HOME/.oh-my-bash"'
  printf '%s\n' "OSH_THEME=\"$THEME_NAME\""
  printf '%s\n' ''
  printf '%s\n' '# Offline install: the framework is vendored, so never look for updates.'
  printf '%s\n' '# DISABLE_AUTO_UPDATE gates tools/check_for_upgrade.sh entirely.'
  printf '%s\n' 'DISABLE_AUTO_UPDATE=true'
  printf '%s\n' '# Belt and braces: if something re-enables the check, never let the'
  printf '%s\n' '# staleness window expire.'
  printf '%s\n' 'UPDATE_OSH_DAYS=100000'
  printf '%s\n' '# DISABLE_UPDATE_PROMPT=true means "upgrade WITHOUT asking", not "stay'
  printf '%s\n' '# quiet". It must remain false on an air-gapped host.'
  printf '%s\n' 'DISABLE_UPDATE_PROMPT=false'
  printf '%s\n' ''
  printf '%s\n' 'plugins=('
  printf '%s\n' '  git'
  printf '%s\n' '  bashmarks'
  printf '%s\n' '  progress'
  printf '%s\n' ')'
  printf '%s\n' ''
  printf '%s\n' 'completions=('
  printf '%s\n' '  git'
  printf '%s\n' '  ssh'
  printf '%s\n' ')'
  printf '%s\n' ''
  printf '%s\n' 'aliases=('
  printf '%s\n' '  general'
  printf '%s\n' ')'
  printf '%s\n' ''
  printf '%s\n' 'OMB_USE_SUDO=true'
  printf '%s\n' 'OMB_PROMPT_SHOW_PYTHON_VENV=false'
  printf '%s\n' ''
  printf '%s\n' '# Set BEFORE the framework loads, and exported on purpose. Oh My Bash runs'
  printf '%s\n' '# env_default LESS -R (lib/misc.sh), and env_default assigns only when the'
  printf '%s\n' '# name is missing from the EXPORTED environment (it tests with env | grep),'
  printf '%s\n' '# so a plain assignment here would be overwritten. Once LESS is set at all,'
  printf '%s\n' '# git stops supplying its own pager flags, and output as short as a'
  printf '%s\n' '# three-line stash list opens a pager that then sits at (END). These are'
  printf '%s\n' '# the flags git itself defaults to: F quits when the output fits on one'
  printf '%s\n' '# screen, R keeps ANSI colour, X leaves the screen uncleared on exit.'
  printf '%s\n' 'export LESS=-FRX'
  printf '%s\n' ''
  printf '%s\n' '[ -r "$OSH/oh-my-bash.sh" ] && source "$OSH/oh-my-bash.sh"'

  if [ "$WITH_BLESH" = "1" ]; then
    printf '%s\n' ''
    printf '%s\n' '[[ ! ${BLE_VERSION-} ]] || ble-attach'
  fi

  printf '%s\n' "$END_MARK"
} >"$BASHRC.new"

mv "$BASHRC.new" "$BASHRC"
rm -f "$stripped"

echo "RC    $BASHRC (managed block refreshed; update checks disabled)"
