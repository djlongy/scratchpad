#!/usr/bin/env bash
# Low side: catalog → drop/ folder (WYSIWYG delivery unit for high-side Pulp/Harbor).
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
OTK_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
# shellcheck source=lib/common.sh
source "$SCRIPT_DIR/lib/common.sh"

CATALOG="${OTK_CATALOG:-$OTK_ROOT/catalog}"
DROP="${OTK_DROP:-$OTK_ROOT/drop}"
RELEASE_ID="${OTK_RELEASE_ID:-$(date -u +%Y%m%dT%H%M%SZ)-$(git -C "$OTK_ROOT" rev-parse --short HEAD 2>/dev/null || echo local)}"
PYTHON="${OTK_PYTHON:-python3}"
OTK_CATALOG="$CATALOG"
export OTK_CATALOG

log() { printf '[drop-build] %s\n' "$*"; }

if [[ -z "$DROP" || "$DROP" == / || "$DROP" == "$HOME" ]]; then
  die "refusing to wipe DROP=$DROP"
fi

rm -rf "$DROP"
mkdir -p "$DROP"/{pypi/packages,galaxy/collections,rpm,oci,vuln-db,meta/{sbom,reports,provenance}}

# ── PyPI (full transitive tree + per-target closure) ──────────────────
req="$CATALOG/pypi/requirements.txt"
if [[ -f "$req" ]] && grep -qvE '^\s*(#|$)' "$req" 2>/dev/null; then
  log "pypi: resolve full dependency tree from $req"
  "$PYTHON" "$SCRIPT_DIR/lib/pypi_resolve.py" \
    --requirements "$req" \
    --dest "$DROP/pypi/packages" \
    --tree "$DROP/meta/provenance/pypi-tree.json" \
    --lock "$DROP/pypi/requirements.lock"
else
  log "pypi: empty — skip"
fi

# ── Galaxy ────────────────────────────────────────────────────────────
gal="$CATALOG/galaxy/requirements.yml"
if [[ -f "$gal" ]] && grep -q 'name:' "$gal" 2>/dev/null; then
  if command -v ansible-galaxy >/dev/null 2>&1; then
    log "galaxy: download collections"
    gal_abs="$(cd "$(dirname "$gal")" && pwd)/$(basename "$gal")"
    ansible-galaxy collection download -r "$gal_abs" --download-path "$DROP/galaxy/collections"
  else
    log "WARN: ansible-galaxy missing — skip galaxy"
  fi
else
  log "galaxy: empty — skip"
fi

# ── RPM (dnf --resolve + createrepo; docker EL if no host dnf) ────────
RPM_ARTIFACT_ROOT="$DROP/rpm"
export RPM_ARTIFACT_ROOT
# shellcheck source=lib/rpm.sh
source "$SCRIPT_DIR/lib/rpm.sh"
build_rpm

# ── OCI (archives + digest provenance; Pulp/Harbor import on high) ────
img="$CATALOG/images/images.txt"
if [[ -f "$img" ]] && grep -qvE '^\s*(#|$)' "$img" 2>/dev/null; then
  if command -v skopeo >/dev/null 2>&1; then
    log "oci: skopeo copy + digest record from $img"
    OTK_OCI_PLATFORM="${OTK_OCI_PLATFORM:-linux/amd64}" \
      "$PYTHON" "$SCRIPT_DIR/lib/oci_images.py" pull \
      --list "$img" --dest "$DROP/oci"
  else
    log "WARN: skopeo missing — skip oci"
  fi
else
  log "oci: empty — skip"
fi

# ── MANIFEST + SHA256SUMS ─────────────────────────────────────────────
"$PYTHON" "$SCRIPT_DIR/lib/drop_verify.py" --write --release-id "$RELEASE_ID" "$DROP"

log "drop ready: $DROP (release_id=$RELEASE_ID)"
log "next: deliver this folder to high, then ./scripts/otk-airgap.sh --mode ingest --drop $DROP"
