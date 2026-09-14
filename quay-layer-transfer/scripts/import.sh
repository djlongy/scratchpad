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

shopt -s nullglob
for t in "$incoming"/transfer-*; do
  name=$(basename "$t")
  # the receive flow writes file by file; wait until manifest.json is there and the tree is quiet
  [ -f "$t/manifest.json" ] || { echo "$name: no manifest.json yet, skipping"; continue; }
  before=$(find "$t" -type f | wc -l); sleep 3; after=$(find "$t" -type f | wc -l)
  [ "$before" = "$after" ] || { echo "$name: still being written, skipping"; continue; }

  python3 "$here/scripts/oci-merge.py" "$t" "$store"

  # state.json is the complete desired tag -> digest map, so reconcile is the only
  # writer: it adds, retargets and deletes in one pass. manifest.json lists only what
  # this transfer carried, which cannot express a deletion - it stays the fallback for
  # transfers produced before state.json existed.
  if [ -f "$t/state.json" ]; then
    python3 "$here/scripts/reconcile.py" "$t/state.json" "$store" "$reg" "$user" "$pass" "$here/.secrets/high.token"
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
