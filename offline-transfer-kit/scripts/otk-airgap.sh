#!/usr/bin/env bash
# otk-airgap.sh — vscode-airgap-shaped dispatcher for pip / RPM / Galaxy / OCI.
#
# Low side (internet):  --mode pull | --mode bundle
# High side (air-gap):  --mode ingest   (zero outbound public-internet calls)
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
OTK_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
# shellcheck source=lib/common.sh
source "$SCRIPT_DIR/lib/common.sh"

MODE="${MODE:-}"
CATALOG="${OTK_CATALOG:-$OTK_ROOT/catalog}"
DROP="${OTK_DROP:-$OTK_ROOT/drop}"
BUNDLE="${OTK_BUNDLE:-}"
REQUIRE_COMPONENTS="${OTK_REQUIRE_COMPONENTS:-}"
PYTHON="${OTK_PYTHON:-python3}"

usage() {
  cat <<'EOF'
otk-airgap.sh — pull listed pip/RPM/Galaxy/OCI resources, bundle them, ingest on the high side

Reads SEPARATE catalog lists (never hardcoded package names):
  <catalog>/pypi/requirements.txt     pip pins (full transitive tree on pull)
  <catalog>/rpm/*.txt + repos.yml     RPM names + repo defs (dnf --resolve)
  <catalog>/galaxy/requirements.yml   Ansible Galaxy collections
  <catalog>/images/images.txt         container refs (skopeo oci-archive)

USAGE
  otk-airgap.sh --mode pull    [--catalog DIR] [--drop DIR]
  otk-airgap.sh --mode bundle  [--catalog DIR] [--drop DIR] [--bundle FILE]
  otk-airgap.sh --mode ingest  [--drop DIR | --bundle FILE]
  otk-airgap.sh --help

MODES
  pull     Low side. Download pip packages and the full transitive
           dependency tree (host + linux CPython 3.9–3.12), fail if any
           target cannot install --no-index from the drop; RPMs + resolved
           deps; Galaxy collections; listed container images. Writes
           MANIFEST.json, pypi-tree.json, and per-file SHA-256.
  bundle   Low side. Same as pull, then packs DROP into a transferable
           tarball (BUNDLE). Default: <drop>.tar.gz
  ingest   High side. Verify SHA-256, then either:
             PULP_URL set   → import into Pulp (optional FOREMAN_URL /
                              OTK_HARBOR_URL). Galaxy tarballs use a
                              Katello file repository when FOREMAN_URL
                              is set (collection upload is sync-only).
             PULP_URL unset → write a static HTTP tree under OTK_SERVE
                              (PEP 503 simple + yum dirs + Galaxy tarballs).
           Makes ZERO outbound public-internet calls.

OPTIONS
  --mode MODE           pull | bundle | ingest   (MODE)
  --catalog DIR         Catalog root with pypi/ rpm/ galaxy/ images/ lists
                        (OTK_CATALOG, default: ./catalog)
  --drop DIR            Drop/bundle working directory (OTK_DROP)
  --bundle FILE         Tarball path for bundle/ingest (OTK_BUNDLE)
  --serve DIR           Static ingest document root (OTK_SERVE)
  --require LIST        Comma list that must be non-empty in the catalog
                        (e.g. pypi,rpm,galaxy,oci). Used by the fixture proof.
  -h, --help            This help

ENVIRONMENT (high side)
  PULP_URL / PULP_USER / PULP_PASSWORD / PULP_REPO_PREFIX / PULP_OCI_PREFIX
  OTK_HARBOR_URL / OTK_HARBOR_USER / OTK_HARBOR_PASSWORD / SKIP_HARBOR
  FOREMAN_URL / FOREMAN_USER / FOREMAN_PASSWORD / FOREMAN_ORG
  FOREMAN_PRODUCT   (optional Katello product name, default OTK)
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --mode) MODE="$2"; shift 2 ;;
    --catalog) CATALOG="$2"; shift 2 ;;
    --drop) DROP="$2"; shift 2 ;;
    --bundle) BUNDLE="$2"; shift 2 ;;
    --serve) OTK_SERVE="$2"; shift 2 ;;
    --require) REQUIRE_COMPONENTS="$2"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) die "unknown arg: $1 (see --help)" ;;
  esac
done

[[ -n "$MODE" ]] || { usage; die "--mode is required (pull|bundle|ingest)"; }

need_cmd "$PYTHON"
OTK_CATALOG="$CATALOG"
OTK_DROP="$DROP"
export OTK_CATALOG OTK_DROP OTK_ROOT

guard_drop() {
  [[ -n "$DROP" && "$DROP" != / && "$DROP" != "$HOME" ]] || die "refusing DROP=$DROP"
}

run_pull() {
  [[ -d "$CATALOG" ]] || die "catalog dir missing: $CATALOG"
  if [[ -n "$REQUIRE_COMPONENTS" ]]; then
    "$PYTHON" "$SCRIPT_DIR/lib/catalog_parse.py" --catalog "$CATALOG" \
      --require "$REQUIRE_COMPONENTS"
  fi
  log "pull: catalog=$CATALOG drop=$DROP"
  OTK_CATALOG="$CATALOG" OTK_DROP="$DROP" OTK_ROOT="$OTK_ROOT" \
    "$SCRIPT_DIR/drop-build.sh"
}

run_bundle() {
  run_pull
  guard_drop
  [[ -d "$DROP" ]] || die "drop missing after pull: $DROP"
  if [[ -z "$BUNDLE" ]]; then
    BUNDLE="${DROP%/}.tar.gz"
  fi
  mkdir -p "$(dirname "$BUNDLE")"
  log "bundle: $DROP → $BUNDLE"
  case "$BUNDLE" in
    *.tar.zst)
      need_cmd zstd
      tar -C "$DROP" -cf - . | zstd -T0 -19 -o "$BUNDLE"
      ;;
    *)
      tar -C "$DROP" -czf "$BUNDLE" .
      ;;
  esac
  local sum
  sum="$(sha256_file "$BUNDLE")"
  echo "${sum}  $(basename "$BUNDLE")" >"${BUNDLE}.sha256"
  log "bundle ready sha256=$sum"
}

unpack_bundle() {
  [[ -f "$BUNDLE" ]] || die "bundle not found: $BUNDLE"
  guard_drop
  mkdir -p "$DROP"
  log "unpack: $BUNDLE → $DROP"
  case "$BUNDLE" in
    *.tar.zst)
      need_cmd zstd
      zstd -dc "$BUNDLE" | tar -C "$DROP" -xf -
      ;;
    *)
      tar -C "$DROP" -xzf "$BUNDLE"
      ;;
  esac
}

run_static_ingest() {
  local serve="${OTK_SERVE:-$OTK_ROOT/serve}"
  [[ -n "$serve" && "$serve" != / && "$serve" != "$HOME" ]] || die "refusing OTK_SERVE=$serve"
  mkdir -p "$serve"
  log "static ingest: $DROP → $serve"
  if [[ -d "$DROP/pypi" ]]; then
    mkdir -p "$serve/pypi"
    cp -a "$DROP/pypi/." "$serve/pypi/"
    if [[ -d "$serve/pypi/packages" ]]; then
      "$PYTHON" "$SCRIPT_DIR/lib/make_simple_index.py" "$serve/pypi"
    fi
  fi
  if [[ -d "$DROP/rpm" ]]; then
    mkdir -p "$serve/rpm"
    cp -a "$DROP/rpm/." "$serve/rpm/"
  fi
  if [[ -d "$DROP/galaxy" ]]; then
    mkdir -p "$serve/galaxy"
    cp -a "$DROP/galaxy/." "$serve/galaxy/"
  fi
  if [[ -d "$DROP/oci" ]]; then
    mkdir -p "$serve/oci"
    cp -a "$DROP/oci/." "$serve/oci/"
  fi
  if [[ -f "$DROP/pypi/requirements.lock" ]]; then
    cp -a "$DROP/pypi/requirements.lock" "$serve/pypi/requirements.lock"
  fi
  cat >"$serve/CLIENTS.txt" <<EOF
# Static high-side clients (no Pulp). From this directory:
#   python3 -m http.server 8080 --directory .
# pip:    pip install --no-index --find-links pypi/packages -r pypi/requirements.lock
#         or pip install -i http://127.0.0.1:8080/pypi/simple --trusted-host 127.0.0.1 PKG
# dnf:    baseurl=http://127.0.0.1:8080/rpm/<repo-id>/
# galaxy: ansible-galaxy collection install galaxy/collections/*.tar.gz
# oci:    skopeo copy oci-archive:oci/<archive>.tar docker-daemon:name:tag
EOF
  log "static serve ready: $serve"
}

run_ingest() {
  if [[ -n "${BUNDLE:-}" && -f "$BUNDLE" ]]; then
    unpack_bundle
  fi
  [[ -d "$DROP" ]] || die "drop dir missing: $DROP (pass --drop or --bundle)"
  "$PYTHON" "$SCRIPT_DIR/lib/drop_verify.py" --verify "$DROP"
  if [[ -n "${PULP_URL:-}" ]]; then
    log "ingest: drop=$DROP pulp=$PULP_URL (no public internet)"
    "$SCRIPT_DIR/drop-import-pulp.sh" "$DROP"
    return
  fi
  log "ingest: drop=$DROP PULP_URL unset — static serve (no public internet)"
  run_static_ingest
}

case "$MODE" in
  pull) run_pull ;;
  bundle) run_bundle ;;
  ingest) run_ingest ;;
  *) die "unknown --mode $MODE (expected pull|bundle|ingest)" ;;
esac
