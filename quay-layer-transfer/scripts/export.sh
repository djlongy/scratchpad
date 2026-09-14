#!/usr/bin/env bash
# Low side: export the newest KEEP semver tags of every repository in ORG as one OCI archive.
#   usage: scripts/export.sh [KEEP]            default KEEP=3 (N, N-1, N-2)
# Output: data/low-export/transfer-<UTC stamp>.tar containing
#   oci/            an OCI image layout with every selected image (blobs are content-addressed,
#                   so layers shared between versions appear once)
#   manifest.json   {transfer, images: [{ref, digest, layers[]}]} for the receiving side
# Needs: skopeo, .secrets/low.token (Quay API), the low registry reachable at $LOW_REGISTRY.
set -euo pipefail
keep=${1:-3}
here=$(cd "$(dirname "$0")/.." && pwd)
reg=${LOW_REGISTRY:-localhost:18081}; org=${QUAY_ORG:-demo}
user=${QUAY_USER:-admin}; pass=${QUAY_PASS:-quayadmin123}
api="http://$reg/api/v1"; auth="Authorization: Bearer $(cat "$here/.secrets/low.token")"
stamp=$(date -u +%Y%m%dT%H%M%SZ)
work=$(mktemp -d); trap 'rm -rf "$work"' EXIT
mkdir -p "$work/oci" "$here/data/low-export"

# --- which images -------------------------------------------------------------------------
sel=$(python3 "$here/scripts/select-tags.py" "$api" "$org" "$keep" "$here/.secrets/low.token")
selected=()
# not mapfile: macOS still ships bash 3.2 and this script runs on the operator's machine
while IFS= read -r r; do [ -n "$r" ] && selected+=("$r"); done < <(
  printf '%s' "$sel" | python3 -c 'import json,sys; print("\n".join(json.load(sys.stdin)["selected"]))')
[ ${#selected[@]} -gt 0 ] || { echo "no semver tags under $org" >&2; exit 1; }
echo "selected ${#selected[@]} image(s): ${selected[*]}"

# --- copy into one OCI layout --------------------------------------------------------------
for ref in "${selected[@]}"; do
  skopeo copy -q --src-tls-verify=false --src-creds "$user:$pass" "docker://$reg/$ref" "oci:$work/oci:$ref"
done

# --- manifest for the far side --------------------------------------------------------------
python3 - "$work" "$stamp" "${selected[@]}" > "$work/manifest.json" <<'PY'
import json, sys
work, stamp, refs = sys.argv[1], sys.argv[2], sys.argv[3:]
index = json.load(open(f"{work}/oci/index.json"))
by_ref = {m["annotations"]["org.opencontainers.image.ref.name"]: m["digest"] for m in index["manifests"]}
images = []
for ref in refs:
    digest = by_ref[ref]
    man = json.load(open(f"{work}/oci/blobs/sha256/{digest.split(':')[1]}"))
    images.append({"ref": ref, "digest": digest, "config": man["config"]["digest"],
                   "layers": [layer["digest"] for layer in man["layers"]]})
json.dump({"transfer": stamp, "images": images}, sys.stdout, indent=1)
PY

blobs=$(find "$work/oci/blobs" -type f | wc -l | tr -d ' ')
# COPYFILE_DISABLE: macOS tar would add AppleDouble ._* entries, which the flow would count as blobs
# The desired state of the far side, in full, every time. It is what lets the high
# side delete a tag that went away upstream: an "add these" message never can.
printf '%s' "$sel" | python3 -c '
import json, sys
d = json.load(sys.stdin)
json.dump({"transfer": sys.argv[1], "repos": d["repos"]}, sys.stdout, indent=1)' "$stamp" > "$work/state.json"

COPYFILE_DISABLE=1 tar -C "$work" -cf "$here/data/low-export/transfer-$stamp.tar" oci manifest.json state.json
echo "wrote data/low-export/transfer-$stamp.tar: ${#selected[@]} images, $blobs blobs, $(du -h "$here/data/low-export/transfer-$stamp.tar" | cut -f1)"
