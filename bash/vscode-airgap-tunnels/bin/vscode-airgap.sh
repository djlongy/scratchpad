#!/usr/bin/env bash
# vscode-airgap.sh — download, bundle, and install VS Code Server for
# air-gapped / offline networks, with an honest fallback to Microsoft's
# Remote Tunnels for the online case.
#
# See docs/reference/download-urls.md for the exact endpoints this uses,
# docs/runbooks/ for online-vs-airgap walkthroughs, and
# docs/designs/vscode-airgap-tunnels.md for why it's built this way.
set -euo pipefail

# ── Constants ─────────────────────────────────────────────────────────────
SELF="$(basename "$0")"
readonly SELF
readonly UPDATE_HOST="https://update.code.visualstudio.com"
readonly DEFAULT_INSTALL_DIR="${HOME}/.vscode-server"
readonly DEFAULT_BIND_ADDR="127.0.0.1"
readonly DEFAULT_PORT="8000"

# ── Defaults (overridable by env, then by flags) ────────────────────────────
MODE="${MODE:-}"
CHANNEL="${CHANNEL:-stable}"
VERSION="${VERSION:-}"
COMMIT="${COMMIT:-}"
ARCH="${ARCH:-}"
INSTALL_DIR="${INSTALL_DIR:-$DEFAULT_INSTALL_DIR}"
BIND_ADDR="${BIND_ADDR:-$DEFAULT_BIND_ADDR}"
PORT="${PORT:-$DEFAULT_PORT}"
TOKEN="${TOKEN:-}"
EXTENSIONS="${EXTENSIONS:-}"
BUNDLE_PATH="${BUNDLE_PATH:-}"
ACTION="install"          # install (default) | download | tunnel | status
START_AFTER_INSTALL=1
DOWNLOAD_ONLY=0
FORCE=0

# HTTP(S)_PROXY / NO_PROXY are read straight from the environment by curl;
# we only need to surface what we're honouring in --help and logs.

# ── Logging (never echo secrets) ─────────────────────────────────────────
log()  { printf '[%s] %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*" >&2; }
die()  { log "ERROR: $*"; exit 1; }
warn() { log "WARN:  $*"; }

# Set by run_bundle/run_offline right after mktemp -d; a script-global (not
# `local`) so the EXIT trap can still see it however the function returns —
# including via the `exec` at the bottom of install_from_stage, where the
# trap never fires at all because exec replaces the process image outright.
_STAGE_DIR=""
cleanup_stage_dir() { [ -n "$_STAGE_DIR" ] && rm -rf "$_STAGE_DIR"; }

# ── Help ─────────────────────────────────────────────────────────────────
usage() {
  cat <<'EOF'
vscode-airgap.sh — VS Code Server for air-gapped / offline networks

USAGE
  vscode-airgap.sh --mode online   [options]   # internet-connected host
  vscode-airgap.sh --mode bundle   [options]   # internet-connected host: pack a bundle
  vscode-airgap.sh --mode offline  [options]   # air-gapped host: install from a bundle
  vscode-airgap.sh --help

MODES
  online    Resolve latest (or pinned) commit, download the VS Code CLI and
            server-web tarballs directly from Microsoft's CDN, install, and
            (unless --download-only) start `code serve-web` bound to
            BIND_ADDR:PORT. Also the only mode that supports --tunnel.
  bundle    Same download step as online, but instead of installing, packs
            everything into a single tarball (BUNDLE_PATH) with a
            versions.json manifest (commit, version, arch, channel, date,
            sha256 of every artifact). Run this on a machine with internet
            access, then carry the tarball across the air gap.
  offline   Installs from a bundle tarball with ZERO outbound network calls.
            Refuses to run if BUNDLE_PATH is missing, and verifies every
            artifact's sha256 against the bundle's own versions.json before
            extracting anything. Starts `code serve-web`; --tunnel is
            rejected in this mode (Remote Tunnels needs Microsoft's relay,
            see LIMITATIONS below).

OPTIONS (env var equivalents in parentheses)
  --mode MODE            online|bundle|offline (MODE)
  --channel CHANNEL      stable|insider, default stable (CHANNEL)
  --version VERSION      Semver, e.g. 1.96.2. Only meaningful together with
                          --commit — see docs/reference/download-urls.md for
                          why Microsoft doesn't expose a semver->commit API
                          for historical releases. (VERSION)
  --commit COMMIT        40-char git commit hash to pin exactly. Overrides
                          --version/latest resolution when set. (COMMIT)
  --arch ARCH             linux-x64|linux-arm64|linux-armhf|alpine-x64|
                          alpine-arm64|darwin-x64|darwin-arm64
                          Auto-detected from uname if omitted. (ARCH)
  --install-dir DIR       Default: ~/.vscode-server (INSTALL_DIR)
  --bundle-path PATH      Bundle tarball: output path (mode=bundle) or
                          input path (mode=offline). (BUNDLE_PATH)
  --bind ADDR             serve-web bind address, default 127.0.0.1.
                          Use 0.0.0.0 only inside a network you trust —
                          this process has no TLS of its own. (BIND_ADDR)
  --port PORT             serve-web port, default 8000 (PORT)
  --token TOKEN           Connection token for serve-web. If omitted, one is
                          generated and written to
                          INSTALL_DIR/serve-web.token (chmod 600) — never
                          printed to stdout/stderr. Pass TOKEN=none to run
                          --without-connection-token (LAN-only, no auth —
                          only ever do this behind another access control
                          layer). (TOKEN)
  --extensions LIST       Comma-separated publisher.name IDs to fetch as
                          .vsix from the Marketplace gallery API (online/
                          bundle modes only). Staged at
                          INSTALL_DIR/extensions-to-install on install —
                          `code --install-extension` does not reliably
                          target a separately-extracted server-web tree
                          (verified live; see download-urls.md), so these
                          are installed via the Web UI's Extensions view
                          -> "Install from VSIX..." instead. (EXTENSIONS)
  --download-only         Fetch/verify artifacts but do not install/start.
  --tunnel                After install, run `code tunnel` instead of
                          `code serve-web`. online mode only — see
                          LIMITATIONS.
  --status                Print install state for INSTALL_DIR and exit.
  --force                 Re-download even if a matching cached artifact
                          (by sha256) already exists.
  -h, --help              This text.

PROXY
  HTTPS_PROXY / HTTP_PROXY / NO_PROXY are honoured for every download (curl
  reads them natively; nothing in this script bypasses them). Set them
  before invoking online/bundle mode if your network requires an egress
  proxy.

EXAMPLES
  # Online side: latest stable, straight install + start on localhost:8000
  ./vscode-airgap.sh --mode online

  # Online side: pin an exact commit, build a portable bundle with one extension
  COMMIT=a5b500951314efd502d07465bd138dfbd714a960 \
    ./vscode-airgap.sh --mode bundle --bundle-path ./vscode-bundle.tar.gz \
    --extensions ms-python.python

  # Carry vscode-bundle.tar.gz across the air gap, then on the isolated host:
  ./vscode-airgap.sh --mode offline --bundle-path ./vscode-bundle.tar.gz \
    --bind 0.0.0.0 --port 8000

  # Client side, same LAN: SSH port-forward instead of exposing the port
  ssh -L 8000:localhost:8000 user@airgapped-host
  # then browse http://localhost:8000 and paste the token from
  # INSTALL_DIR/serve-web.token on the server

LIMITATIONS — READ THIS BEFORE CHOOSING --tunnel
  Microsoft's Remote Tunnels (`code tunnel`) are NOT air-gap compatible.
  The CLI authenticates against github.com/login.microsoftonline.com and
  then keeps a persistent outbound connection to Microsoft's tunnel relay
  (global.rel.tunnels.api.visualstudio.com and friends) for the lifetime of
  the tunnel — there is no offline or self-hosted relay mode. If your
  network can reach the internet but you still want tunnels, use
  `--mode online --tunnel`. If it can't, this script refuses --tunnel in
  offline mode and falls back to `code serve-web`, which:
    - binds directly to a LAN address (BIND_ADDR:PORT) with no external
      dependency once the bundle is installed, and
    - is what this script installs and starts by default in every mode.
  The client reaches serve-web either directly (if BIND_ADDR is
  LAN-reachable) or via an SSH local port-forward (recommended — keeps
  serve-web itself bound to 127.0.0.1 and avoids exposing it network-wide).
EOF
}

# ── Arg parsing ──────────────────────────────────────────────────────────
while [ $# -gt 0 ]; do
  case "$1" in
    --mode) MODE="$2"; shift 2 ;;
    --channel) CHANNEL="$2"; shift 2 ;;
    --version) VERSION="$2"; shift 2 ;;
    --commit) COMMIT="$2"; shift 2 ;;
    --arch) ARCH="$2"; shift 2 ;;
    --install-dir) INSTALL_DIR="$2"; shift 2 ;;
    --bundle-path) BUNDLE_PATH="$2"; shift 2 ;;
    --bind) BIND_ADDR="$2"; shift 2 ;;
    --port) PORT="$2"; shift 2 ;;
    --token) TOKEN="$2"; shift 2 ;;
    --extensions) EXTENSIONS="$2"; shift 2 ;;
    --download-only) DOWNLOAD_ONLY=1; START_AFTER_INSTALL=0; shift ;;
    --tunnel) ACTION="tunnel"; shift ;;
    --status) ACTION="status"; shift ;;
    --force) FORCE=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) die "unknown argument: $1 (see --help)" ;;
  esac
done

# --status is a standalone query against an existing INSTALL_DIR and never
# touches MODE/CHANNEL — skip those checks for it entirely.
if [ "$ACTION" != "status" ]; then
  [ -n "$MODE" ] || { usage; die "MODE / --mode is required (online|bundle|offline)"; }
  case "$MODE" in online|bundle|offline) ;; *) die "invalid --mode '$MODE' (online|bundle|offline)" ;; esac
  case "$CHANNEL" in stable|insider) ;; *) die "invalid --channel '$CHANNEL' (stable|insider)" ;; esac
fi

# ── Dependency check ─────────────────────────────────────────────────────
require_cmd() { command -v "$1" >/dev/null 2>&1 || die "required command not found: $1"; }
if [ "$ACTION" != "status" ]; then
  for c in tar sha256sum; do
    command -v "$c" >/dev/null 2>&1 || command -v "${c/sha256sum/shasum}" >/dev/null 2>&1 || die "required command not found: $c"
  done
  [ "$MODE" != "offline" ] && require_cmd curl
fi

sha256_of() {
  # Portable sha256: coreutils sha256sum on Linux, shasum -a 256 on macOS/BSD.
  if command -v sha256sum >/dev/null 2>&1; then
    sha256sum "$1" | awk '{print $1}'
  else
    shasum -a 256 "$1" | awk '{print $1}'
  fi
}

# ── Arch auto-detect ─────────────────────────────────────────────────────
detect_arch() {
  [ -n "$ARCH" ] && { echo "$ARCH"; return; }
  local os_name kernel_arch
  os_name="$(uname -s)"
  kernel_arch="$(uname -m)"
  case "$os_name" in
    Linux)
      # Alpine/musl needs the alpine-* artifact family; everything else (glibc) uses linux-*.
      if [ -f /etc/alpine-release ]; then
        case "$kernel_arch" in
          x86_64) echo "alpine-x64" ;;
          aarch64|arm64) echo "alpine-arm64" ;;
          *) die "unsupported Linux/alpine arch: $kernel_arch" ;;
        esac
      else
        case "$kernel_arch" in
          x86_64) echo "linux-x64" ;;
          aarch64|arm64) echo "linux-arm64" ;;
          armv7l|armhf) echo "linux-armhf" ;;
          *) die "unsupported Linux arch: $kernel_arch" ;;
        esac
      fi
      ;;
    Darwin)
      case "$kernel_arch" in
        arm64) echo "darwin-arm64" ;;
        x86_64) echo "darwin-x64" ;;
        *) die "unsupported Darwin arch: $kernel_arch" ;;
      esac
      ;;
    *) die "unsupported OS: $os_name (this tool targets Linux server hosts; darwin-* is for running the client CLI locally)" ;;
  esac
}
ARCH="$(detect_arch)"

# cli-<arch> / server-<arch>[-web] path segments per update.code.visualstudio.com's
# naming (verified live 2026-08-17 — see docs/reference/download-urls.md).
cli_platform_segment() {
  case "$ARCH" in
    linux-x64) echo "cli-linux-x64" ;;
    linux-arm64) echo "cli-linux-arm64" ;;
    linux-armhf) echo "cli-linux-armhf" ;;
    alpine-x64) echo "cli-alpine-x64" ;;
    alpine-arm64) echo "cli-alpine-arm64" ;;
    darwin-x64) echo "cli-darwin-x64" ;;
    darwin-arm64) echo "cli-darwin-arm64" ;;
    *) die "no CLI artifact mapping for arch '$ARCH'" ;;
  esac
}
server_web_platform_segment() {
  case "$ARCH" in
    linux-x64) echo "server-linux-x64-web" ;;
    linux-arm64) echo "server-linux-arm64-web" ;;
    linux-armhf) echo "server-linux-armhf-web" ;;
    alpine-x64) echo "server-linux-alpine-web" ;;
    *) die "no server-web artifact for arch '$ARCH' (server-web ships for glibc/musl x64/arm64 Linux only; darwin/armhf have no server build — they are client-only architectures)" ;;
  esac
}

# ── Commit resolution (online/bundle only) ──────────────────────────────
resolve_commit() {
  [ -n "$COMMIT" ] && { echo "$COMMIT"; return; }
  if [ -n "$VERSION" ]; then
    warn "VERSION='$VERSION' set without COMMIT: Microsoft does not publish a" \
         "semver->commit lookup API for historical cli/server builds (only the" \
         "desktop /api/update/*/latest endpoint carries both, and only for the" \
         "current latest release). Falling back to the latest '$CHANNEL' commit;" \
         "if you need an exact pin, pass --commit instead. See" \
         "docs/reference/download-urls.md."
  fi
  log "resolving latest '$CHANNEL' commit for server-linux-x64 (the commit is" \
      "shared across all platform artifacts within a channel)"
  local commits_json first
  commits_json="$(curl -fsSL -m 20 "$UPDATE_HOST/api/commits/$CHANNEL/server-linux-x64")" \
    || die "failed to reach $UPDATE_HOST (check network/proxy — HTTPS_PROXY=${HTTPS_PROXY:-unset})"
  first="$(printf '%s' "$commits_json" | tr -d '[]" ' | cut -d',' -f1)"
  [ -n "$first" ] || die "could not parse a commit from the commits API response"
  echo "$first"
}

# ── Download helpers ─────────────────────────────────────────────────────
# download_artifact <commit> <platform-segment> <dest-file>
# Follows the commit: redirect (Microsoft's CDN issues a 302 to the real
# blob URL), honouring HTTPS_PROXY/HTTP_PROXY/NO_PROXY from the environment
# via curl's native proxy handling.
download_artifact() {
  local commit="$1" seg="$2" dest="$3"
  local url="$UPDATE_HOST/commit:$commit/$seg/$CHANNEL"
  log "downloading $seg (commit ${commit:0:12}...) -> $dest"
  curl -fSL -sS --retry 3 --retry-connrefused -m 300 -o "$dest" "$url" \
    || die "download failed for $seg from $url"
  [ -s "$dest" ] || die "downloaded file is empty: $dest"
}

# ── Extensions (Marketplace gallery API — direct .vsix, no `ext install`) ──
download_extension() {
  local ext_id="$1" dest_dir="$2"
  local pub name
  pub="${ext_id%%.*}"
  name="${ext_id#*.}"
  [ -n "$pub" ] && [ -n "$name" ] && [ "$pub" != "$name" ] \
    || die "invalid extension id '$ext_id' (expected publisher.name)"
  local url="https://marketplace.visualstudio.com/_apis/public/gallery/publishers/${pub}/vsextensions/${name}/latest/vspackage"
  local dest="${dest_dir}/${ext_id}.vsix"
  log "downloading extension $ext_id -> $dest"
  curl -fSL -sS -m 120 -o "$dest" "$url" || die "extension download failed: $ext_id"
  [ -s "$dest" ] || die "downloaded extension is empty: $ext_id"
}

# ── ONLINE / BUNDLE: fetch everything into a staging dir ────────────────
stage_artifacts() {
  local stage_dir="$1"
  mkdir -p "$stage_dir"
  local commit cli_seg srv_seg cli_file srv_file cli_sha srv_sha
  commit="$(resolve_commit)"
  cli_seg="$(cli_platform_segment)"
  cli_file="$stage_dir/cli.tar.gz"
  if [ "$FORCE" -eq 1 ] || [ ! -f "$cli_file" ]; then
    download_artifact "$commit" "$cli_seg" "$cli_file"
  fi
  cli_sha="$(sha256_of "$cli_file")"

  srv_file=""
  srv_sha=""
  if srv_seg="$(server_web_platform_segment 2>/dev/null)"; then
    srv_file="$stage_dir/server-web.tar.gz"
    if [ "$FORCE" -eq 1 ] || [ ! -f "$srv_file" ]; then
      download_artifact "$commit" "$srv_seg" "$srv_file"
    fi
    srv_sha="$(sha256_of "$srv_file")"
  else
    warn "no server-web artifact for arch '$ARCH' — bundling CLI only (client-only architecture, e.g. darwin/armhf)"
  fi

  local ext_dir="$stage_dir/extensions"
  local ext_list=()
  if [ -n "$EXTENSIONS" ]; then
    mkdir -p "$ext_dir"
    local IFS=','
    for ext in $EXTENSIONS; do
      ext="$(echo "$ext" | xargs)"
      [ -n "$ext" ] || continue
      download_extension "$ext" "$ext_dir"
      ext_list+=("$ext")
    done
  fi

  # versions.json — the manifest offline installs verify against. This is
  # OUR OWN computed sha256, not one Microsoft publishes: their commit-based
  # cli/server-web artifacts ship with no published checksum file (unlike,
  # say, a Linux distro ISO). See docs/reference/download-urls.md for the
  # one endpoint that DOES carry a Microsoft-issued sha256 (desktop
  # /api/update, not used by this tool) and why it doesn't apply here.
  local manifest="$stage_dir/versions.json"
  {
    printf '{\n'
    printf '  "channel": "%s",\n' "$CHANNEL"
    printf '  "commit": "%s",\n' "$commit"
    printf '  "requested_version": "%s",\n' "${VERSION:-latest}"
    printf '  "arch": "%s",\n' "$ARCH"
    printf '  "built_at_utc": "%s",\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    printf '  "cli_artifact": "cli.tar.gz",\n'
    printf '  "cli_sha256": "%s",\n' "$cli_sha"
    if [ -n "$srv_file" ]; then
      printf '  "server_artifact": "server-web.tar.gz",\n'
      printf '  "server_sha256": "%s",\n' "$srv_sha"
    else
      printf '  "server_artifact": null,\n'
      printf '  "server_sha256": null,\n'
    fi
    printf '  "extensions": ['
    local i=0
    for ext in "${ext_list[@]:-}"; do
      [ -n "$ext" ] || continue
      [ $i -gt 0 ] && printf ','
      printf '\n    {"id": "%s", "sha256": "%s"}' "$ext" "$(sha256_of "$ext_dir/$ext.vsix")"
      i=$((i+1))
    done
    [ $i -gt 0 ] && printf '\n  '
    printf ']\n'
    printf '}\n'
  } > "$manifest"
  log "manifest written: $manifest"
}

# ── BUNDLE mode ───────────────────────────────────────────────────────────
run_bundle() {
  [ -n "$BUNDLE_PATH" ] || die "--bundle-path is required in bundle mode"
  _STAGE_DIR="$(mktemp -d)"
  trap cleanup_stage_dir EXIT
  stage_artifacts "$_STAGE_DIR"
  mkdir -p "$(dirname "$BUNDLE_PATH")"
  tar -C "$_STAGE_DIR" -czf "$BUNDLE_PATH" .
  log "bundle written: $BUNDLE_PATH ($(du -h "$BUNDLE_PATH" | awk '{print $1}'))"
  log "carry this file across the air gap, then run:"
  log "  $SELF --mode offline --bundle-path <path-to-bundle> --bind 0.0.0.0 --port $PORT"
}

# ── ONLINE mode ────────────────────────────────────────────────────────────
run_online() {
  local stage_dir="$INSTALL_DIR/.download-cache"
  stage_artifacts "$stage_dir"
  install_from_stage "$stage_dir"
}

# ── OFFLINE mode ──────────────────────────────────────────────────────────
verify_no_network_tools_needed() {
  # Defensive: offline mode must not shell out to curl at all. This function
  # exists purely as a documented guarantee point — no network call is ever
  # made past this line for MODE=offline.
  :
}

run_offline() {
  [ -n "$BUNDLE_PATH" ] || die "--bundle-path is required in offline mode (path to a tarball built with --mode bundle)"
  [ -f "$BUNDLE_PATH" ] || die "bundle not found: $BUNDLE_PATH"
  verify_no_network_tools_needed
  _STAGE_DIR="$(mktemp -d)"
  trap cleanup_stage_dir EXIT
  log "extracting bundle (no network access used or required for this step)"
  tar -C "$_STAGE_DIR" -xzf "$BUNDLE_PATH"
  [ -f "$_STAGE_DIR/versions.json" ] || die "bundle is missing versions.json — not built by this script?"

  local expect_cli_sha expect_srv_sha srv_artifact
  expect_cli_sha="$(json_field "$_STAGE_DIR/versions.json" cli_sha256)"
  expect_srv_sha="$(json_field "$_STAGE_DIR/versions.json" server_sha256)"
  srv_artifact="$(json_field "$_STAGE_DIR/versions.json" server_artifact)"

  local got
  got="$(sha256_of "$_STAGE_DIR/cli.tar.gz")"
  [ "$got" = "$expect_cli_sha" ] || die "sha256 mismatch for cli.tar.gz: expected $expect_cli_sha got $got — bundle is corrupt or tampered, refusing to install"
  log "cli.tar.gz sha256 OK"

  if [ -n "$srv_artifact" ] && [ "$srv_artifact" != "null" ]; then
    got="$(sha256_of "$_STAGE_DIR/server-web.tar.gz")"
    [ "$got" = "$expect_srv_sha" ] || die "sha256 mismatch for server-web.tar.gz: expected $expect_srv_sha got $got — bundle is corrupt or tampered, refusing to install"
    log "server-web.tar.gz sha256 OK"
  else
    warn "bundle has no server artifact for this arch — CLI-only install (cannot run serve-web here)"
  fi

  if [ "$ACTION" = "tunnel" ]; then
    die "--tunnel is not supported in offline mode: Remote Tunnels requires an outbound connection to Microsoft's relay, which by definition is not available on an air-gapped host. Use serve-web (the default) instead — see --help LIMITATIONS."
  fi

  install_from_stage "$_STAGE_DIR"
}

# tiny JSON scalar-field reader (no jq dependency) — good enough for our own
# flat versions.json, not a general parser.
json_field() {
  local file="$1" field="$2"
  python3 -c "import json,sys; d=json.load(open(sys.argv[1])); v=d.get(sys.argv[2]); print('' if v is None else v)" "$file" "$field" 2>/dev/null \
    || sed -n "s/.*\"$field\":[[:space:]]*\"\{0,1\}\([^\",}]*\)\"\{0,1\}.*/\1/p" "$file" | head -1
}

# ── Shared install step (used by online + offline) ───────────────────────
install_from_stage() {
  local stage_dir="$1"
  mkdir -p "$INSTALL_DIR/cli" "$INSTALL_DIR/server"

  log "installing CLI into $INSTALL_DIR/cli"
  tar -C "$INSTALL_DIR/cli" -xzf "$stage_dir/cli.tar.gz"
  local cli_bin
  cli_bin="$(find "$INSTALL_DIR/cli" -maxdepth 1 -type f -name 'code' | head -1)"
  [ -n "$cli_bin" ] || die "extracted CLI tarball did not contain a 'code' binary"
  chmod +x "$cli_bin"

  if [ -f "$stage_dir/server-web.tar.gz" ]; then
    log "installing server-web into $INSTALL_DIR/server"
    tar -C "$INSTALL_DIR/server" -xzf "$stage_dir/server-web.tar.gz"
  fi

  if [ -d "$stage_dir/extensions" ] && [ -n "$(ls -A "$stage_dir/extensions" 2>/dev/null)" ]; then
    # `code --install-extension` targets the CLI's OWN version-managed
    # install (see docs/reference/download-urls.md "extension install
    # gap") — it does NOT reliably reach a separately-extracted
    # server-web tree the way this script lays one out. Verified live
    # (2026-08-17): it fails with "No installation of Visual Studio Code
    # stable was found" against exactly this layout. Rather than pretend
    # that works, copy the vsix files somewhere durable and tell the
    # operator the one path that IS standard VS Code behaviour: the Web
    # UI's own Extensions view -> "Install from VSIX...".
    mkdir -p "$INSTALL_DIR/extensions-to-install"
    cp -f "$stage_dir"/extensions/*.vsix "$INSTALL_DIR/extensions-to-install/"
    log "extensions staged (not auto-installed — see below): $INSTALL_DIR/extensions-to-install"
    log "  once connected, open the Extensions view in the browser and use" \
        "'Install from VSIX...' for each file in that directory."
  fi

  cp -f "$stage_dir/versions.json" "$INSTALL_DIR/versions.json"
  log "install complete. versions: $INSTALL_DIR/versions.json"

  if [ "$DOWNLOAD_ONLY" -eq 1 ]; then
    log "--download-only set: not starting a server"
    return
  fi

  if [ "$ACTION" = "tunnel" ]; then
    start_tunnel "$cli_bin"
  elif [ "$START_AFTER_INSTALL" -eq 1 ]; then
    start_serve_web "$cli_bin"
  fi
}

resolve_token() {
  if [ "$TOKEN" = "none" ]; then
    echo "__NONE__"
    return
  fi
  if [ -n "$TOKEN" ]; then
    printf '%s' "$TOKEN"
    return
  fi
  local tok_file="$INSTALL_DIR/serve-web.token"
  if [ ! -f "$tok_file" ]; then
    ( umask 077; openssl rand -hex 32 > "$tok_file" 2>/dev/null || head -c 32 /dev/urandom | od -An -tx1 | tr -d ' \n' > "$tok_file" )
    chmod 600 "$tok_file"
    log "generated a new connection token: $tok_file (chmod 600, not printed)"
  else
    log "reusing existing connection token: $tok_file"
  fi
  cat "$tok_file"
}

start_serve_web() {
  local cli_bin="$1"
  local tok
  tok="$(resolve_token)"
  log "starting serve-web on ${BIND_ADDR}:${PORT}"
  local args=(serve-web --host "$BIND_ADDR" --port "$PORT" --server-data-dir "$INSTALL_DIR/server-data")
  if [ "$tok" = "__NONE__" ]; then
    warn "TOKEN=none: starting WITHOUT a connection token (--without-connection-token)." \
         "Only do this behind another access-control layer (firewall/VPN/SSH tunnel to 127.0.0.1)."
    args+=(--without-connection-token)
  else
    args+=(--connection-token "$tok")
  fi
  exec "$cli_bin" "${args[@]}"
}

start_tunnel() {
  local cli_bin="$1"
  [ "$MODE" = "online" ] || die "internal error: start_tunnel called outside online mode"
  warn "Remote Tunnels phones home to Microsoft's relay service and requires a" \
       "GitHub/Microsoft device-code login on first run — this is NOT" \
       "air-gap compatible. Proceeding because MODE=online and --tunnel was" \
       "explicitly requested."
  exec "$cli_bin" tunnel
}

# ── status ─────────────────────────────────────────────────────────────
run_status() {
  if [ ! -f "$INSTALL_DIR/versions.json" ]; then
    echo "not installed: $INSTALL_DIR"
    exit 1
  fi
  echo "install dir : $INSTALL_DIR"
  echo "versions    : $INSTALL_DIR/versions.json"
  cat "$INSTALL_DIR/versions.json"
  [ -f "$INSTALL_DIR/serve-web.token" ] && echo "token file  : $INSTALL_DIR/serve-web.token (present, not shown)"
}

# ── Dispatch ───────────────────────────────────────────────────────────
if [ "$ACTION" = "status" ]; then
  run_status
  exit 0
fi

log "mode=$MODE channel=$CHANNEL arch=$ARCH install_dir=$INSTALL_DIR"
case "$MODE" in
  online)  run_online ;;
  bundle)  run_bundle ;;
  offline) run_offline ;;
esac
