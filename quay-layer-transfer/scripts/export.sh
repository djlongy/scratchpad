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
repos=$(curl -fsS -H "$auth" "$api/repository?namespace=$org&limit=100" \
  | python3 -c 'import json,sys; print("\n".join(r["name"] for r in json.load(sys.stdin)["repositories"]))')
selected=()
for repo in $repos; do
  tags=$(curl -fsS -H "$auth" "$api/repository/$org/$repo/tag/?limit=100&onlyActiveTags=true" \
    | python3 -c '
import json, re, sys
keep = int(sys.argv[1])
semver = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)$")           # 1.2.3 or v1.2.3; latest/dev/rc tags are ignored
tags = [t["name"] for t in json.load(sys.stdin)["tags"] if semver.match(t["name"])]
tags.sort(key=lambda t: tuple(int(x) for x in semver.match(t).groups()), reverse=True)
print("\n".join(tags[:keep]))' "$keep")
  for t in $tags; do selected+=("$org/$repo:$t"); done
done
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
COPYFILE_DISABLE=1 tar -C "$work" -cf "$here/data/low-export/transfer-$stamp.tar" oci manifest.json
echo "wrote data/low-export/transfer-$stamp.tar: ${#selected[@]} images, $blobs blobs, $(du -h "$here/data/low-export/transfer-$stamp.tar" | cut -f1)"
