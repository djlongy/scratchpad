# shellcheck shell=bash
# Shared helpers for OTK scripts. Source only; do not execute.

OTK_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
export OTK_ROOT

OTK_CATALOG="${OTK_CATALOG:-$OTK_ROOT/catalog}"
OTK_CONFIG="${OTK_CONFIG:-$OTK_ROOT/config}"
OTK_WORK="${OTK_WORK:-$OTK_ROOT/.work}"
OTK_OUTBOX="${OTK_OUTBOX:-$OTK_ROOT/outbox}"
OTK_INBOX="${OTK_INBOX:-$OTK_ROOT/inbox}"
OTK_SERVE="${OTK_SERVE:-$OTK_ROOT/serve}"
OTK_ACCEPTED="${OTK_ACCEPTED:-$OTK_ROOT/accepted}"
OTK_QUARANTINE="${OTK_QUARANTINE:-$OTK_ROOT/quarantine}"

log() { printf '%s %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*"; }
die() { log "ERROR: $*"; exit 1; }
need_cmd() { command -v "$1" >/dev/null 2>&1 || die "required command not found: $1"; }

# True if file has non-comment, non-blank lines.
file_has_entries() {
  local f="$1"
  [[ -f "$f" ]] || return 1
  grep -vE '^\s*(#|$)' "$f" >/dev/null 2>&1
}

# Galaxy requirements.yml has real collection entries (not only empty list).
galaxy_has_entries() {
  local f="$1"
  [[ -f "$f" ]] || return 1
  # crude but dependency-free: look for "name:" under collections
  grep -E '^\s*-\s*name:' "$f" >/dev/null 2>&1
}

sha256_file() {
  if command -v sha256sum >/dev/null 2>&1; then
    sha256sum "$1" | awk '{print $1}'
  else
    shasum -a 256 "$1" | awk '{print $1}'
  fi
}

# Write SHA256SUMS for all files under dir (relative paths).
# Usage: write_sha256sums ROOT [basename-to-exclude ...]
# Always writes ROOT/SHA256SUMS; always excludes SHA256SUMS and .DS_Store.
write_sha256sums() {
  local root="$1"
  shift
  local out="$root/SHA256SUMS"
  local -a excludes=(SHA256SUMS .DS_Store)
  local e
  for e in "$@"; do
    excludes+=("$e")
  done
  (
    cd "$root" || exit 1
    local -a find_args=(. -type f)
    for e in "${excludes[@]}"; do
      find_args+=(! -name "$e")
    done
    find_args+=(! -path './.git/*' -print0)
    find "${find_args[@]}" \
      | sort -z \
      | while IFS= read -r -d '' f; do
          f="${f#./}"
          if command -v sha256sum >/dev/null 2>&1; then
            sha256sum "$f"
          else
            shasum -a 256 "$f"
          fi
        done
  ) >"$out"
}

# Append one file's digest to an existing SHA256SUMS (path relative to root).
append_sha256sum() {
  local root="$1" rel="$2" sums="${3:-$1/SHA256SUMS}"
  (
    cd "$root" || exit 1
    if command -v sha256sum >/dev/null 2>&1; then
      sha256sum "$rel"
    else
      shasum -a 256 "$rel"
    fi
  ) >>"$sums"
}

verify_sha256sums() {
  local root="$1"
  local sums="${2:-$root/SHA256SUMS}"
  [[ -f "$sums" ]] || die "missing $sums"
  (
    cd "$root" || exit 1
    if command -v sha256sum >/dev/null 2>&1; then
      sha256sum -c SHA256SUMS
    else
      shasum -a 256 -c SHA256SUMS
    fi
  )
}

atomic_copy() {
  local src="$1" dest="$2"
  local dir partial
  dir="$(dirname "$dest")"
  mkdir -p "$dir"
  partial="${dest}.partial.$$"
  cp -a "$src" "$partial"
  mv -f "$partial" "$dest"
}

# ISO8601 compact UTC + optional short git sha (otk-release- prefix)
make_release_id() {
  local ts short prefix
  prefix="${OTK_RELEASE_PREFIX:-otk-release}"
  ts="$(date -u +%Y%m%dT%H%M%SZ)"
  short="nogit"
  if command -v git >/dev/null 2>&1 && git -C "$OTK_ROOT" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
    short="$(git -C "$OTK_ROOT" rev-parse --short=7 HEAD 2>/dev/null || echo nogit)"
  fi
  printf '%s-%s-%s\n' "$prefix" "$ts" "$short"
}
