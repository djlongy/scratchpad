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
mode=${TRANSFER_MODE:-bidirectional}
# The far side's published digest list. In bidirectional mode it is both the exclusion
# list for this archive and the record the prune plan is computed from.
have=$here/data/low-export/have/blobs.txt
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

# The desired state of the far side, in full, every time. It is what lets the high
# side delete a tag that went away upstream: an "add these" message never can.
# What the far side may now delete: everything it is recorded as holding that this
# export no longer wants. In oneway mode that record is the Redis ledger, and it is
# cleared HERE, before the transfer leaves, so a lost transfer only ever costs a re-send.
# In bidirectional mode the record is the far side's own published list, so there is
# nothing to clear and a wrongly pruned blob simply comes back in the next export.
# See scripts/prune-plan.py.
if [ "$mode" = bidirectional ]; then
  prune_from=("$have")
else
  prune_from=(${REDIS_CLI:-sudo docker compose exec -T dedupe-redis redis-cli})
fi
prune=$(python3 "$here/scripts/prune-plan.py" "$work/oci" "${prune_from[@]}" 2>/tmp/prune-plan.err) || {
  echo "  prune plan skipped: $(tail -1 /tmp/prune-plan.err)" >&2
  prune=""
}
if [ -n "$prune" ]; then
  n=$(printf '%s\n' "$prune" | grep -c .)
  echo "  prune plan: $n blob(s) no longer wanted; the far side may drop them"
  [ "$mode" = oneway ] && echo "  ledger: forgot them here first, so a lost transfer only costs a re-send" || true
fi

printf '%s' "$sel" | python3 -c '
import json, sys
d = json.load(sys.stdin)
prune = [line for line in sys.argv[2].splitlines() if line.strip()]
json.dump({"transfer": sys.argv[1], "repos": d["repos"], "prune": prune}, sys.stdout, indent=1)' "$stamp" "$prune" > "$work/state.json"

# --- checksums back (bidirectional mode) ------------------------------------------------
# import.sh publishes the digests the far-side OCI store already holds to
# data/low-export/have/blobs.txt. Those blobs are left OUT of the archive, so a blob that
# has crossed is never packed again and the send flow needs no dedupe of its own.
# manifest.json still lists every layer of every image, which is what lets oci-merge.py
# prove completeness against the store on the far side.
# No file (first run, or TRANSFER_MODE=oneway where the ledger does the dedupe instead)
# means pack everything.
: > "$work/exclude"
if [ "$mode" = bidirectional ] && [ -f "$have" ]; then
  while read -r d; do
    b=${d#sha256:}
    # `if`, not `[ ... ] && printf`: under `set -e` a final iteration whose test fails
    # would make the whole loop the failing command and abort the export.
    if [ -f "$work/oci/blobs/sha256/$b" ]; then printf 'oci/blobs/sha256/%s\n' "$b"; fi
  done < "$have" > "$work/exclude"
  echo "  checksums back: $(wc -l < "$work/exclude" | tr -d ' ') of $blobs blob(s) already on the high side, left out"
fi

# COPYFILE_DISABLE: macOS tar would add AppleDouble ._* entries, which the flow would
# count as blobs.
COPYFILE_DISABLE=1 tar -C "$work" -X "$work/exclude" -cf "$here/data/low-export/transfer-$stamp.tar" oci manifest.json state.json
echo "wrote data/low-export/transfer-$stamp.tar: ${#selected[@]} images, $(( blobs - $(wc -l < "$work/exclude") )) of $blobs blobs packed, $(du -h "$here/data/low-export/transfer-$stamp.tar" | cut -f1)"
