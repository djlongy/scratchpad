#!/usr/bin/env bash
# High side: merge every received transfer into the OCI store and push its images into the
# high Quay. Run after the receive flow has unpacked a transfer under data/high-store/incoming/.
#   usage: scripts/import.sh                      processes every incoming transfer not yet imported
# Needs: skopeo, python3, and .secrets/high.token (the reconcile pass uses the Quay API).
set -euo pipefail
here=$(cd "$(dirname "$0")/.." && pwd)
reg=${HIGH_REGISTRY:-localhost:18082}
user=${QUAY_USER:-admin}; pass=${QUAY_PASS:-quayadmin123}
store=$here/data/high-store/oci
incoming=$here/data/high-store/incoming
done_dir=$here/data/high-store/imported
mkdir -p "$store" "$incoming" "$done_dir"

# --- the return channel (bidirectional mode) --------------------------------------------
# Publish the digests this store holds where the low side can read them. On a real link
# this is the one narrow, content-free channel back — a sorted list of hashes, no payload.
# export.sh then leaves those blobs out of the next archive, which is what lets the send
# flow be a plain passthrough with no dedupe state of its own.
#
# Published on EVERY exit — success, a failed transfer, an empty incoming/ — because a run
# that merged blobs and then failed on a later transfer has still changed what this side
# holds, and a stale list has the low side re-send blobs that are already here. Written to
# a temporary file and moved into place so the low side never reads a half-written list.
have=$here/data/low-export/have/blobs.txt
publish_have() {
  rc=$?
  mkdir -p "$(dirname "$have")"
  ls "$store/blobs/sha256" 2>/dev/null | sed 's/^/sha256:/' | sort > "$have.tmp"
  mv "$have.tmp" "$have"
  echo "published have/blobs.txt: $(wc -l < "$have" | tr -d ' ') digest(s) the low side need not send again"
  exit $rc
}
trap publish_have EXIT

shopt -s nullglob
for t in "$incoming"/transfer-*; do
  name=$(basename "$t")
  # the receive flow writes file by file; wait until manifest.json is there and the tree is quiet
  [ -f "$t/manifest.json" ] || { echo "$name: no manifest.json yet, skipping"; continue; }
  before=$(find "$t" -type f | wc -l); sleep 3; after=$(find "$t" -type f | wc -l)
  [ "$before" = "$after" ] || { echo "$name: still being written, skipping"; continue; }

  # The completeness check IS the guard on everything destructive below. A transfer
  # that lost blobs in flight is indistinguishable from one whose desired state says
  # "delete these", so an incomplete merge means additive-only and no prune. The
  # transfer stays in incoming/ so a later delivery of the missing blobs completes it.
  if python3 "$here/scripts/oci-merge.py" "$t" "$store"; then
    complete=yes
  else
    complete=no
    echo "  $name: incomplete after merge — no reconcile, no prune, left in incoming/ to retry"
  fi

  # state.json is the complete desired tag -> digest map, so reconcile is the only
  # writer: it adds, retargets and deletes in one pass. manifest.json lists only what
  # this transfer carried, which cannot express a deletion - it stays the fallback for
  # transfers produced before state.json existed.
  if [ "$complete" = no ]; then
    continue
  elif [ -f "$t/state.json" ]; then
    python3 "$here/scripts/reconcile.py" "$t/state.json" "$store" "$reg" "$user" "$pass" \
      "$here/.secrets/high.token" "$here/data/high-store/.applied"
    # Only now, after the tags that should survive are in place, drop what nothing
    # wants any more. Guarded again locally: see scripts/prune-store.py.
    python3 "$here/scripts/prune-store.py" "$t/state.json" "$store"
  else
    echo "  $name: no state.json, additive import only"
    for ref in $(python3 -c 'import json,sys; print("\n".join(i["ref"] for i in json.load(open(sys.argv[1]))["images"]))' "$t/manifest.json"); do
      skopeo copy -q --dest-tls-verify=false --dest-creds "$user:$pass" "oci:$store:$ref" "docker://$reg/$ref"
      echo "  imported $reg/$ref"
    done
  fi

  mv "$t" "$done_dir/$name"
  echo "$name: imported, moved to imported/"
done
