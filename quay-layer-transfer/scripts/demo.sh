#!/usr/bin/env bash
# The whole proof in one run, against a freshly started stack (compose up -d, both Quays
# initialised, NiFi flows built). Three transfers:
#   1. first export of the newest three versions: every blob crosses
#   2. a new patch release: only its new layer, config and manifest cross
#   3. the same export again: nothing crosses
# Each transfer is imported into the high Quay and pulled back.
#
# TRANSFER_MODE decides WHERE the dedupe happens, not whether it happens. The three
# numbers below are the same either way:
#   bidirectional (default)  export.sh leaves out what the far side published in
#                            have/blobs.txt; the flow is a passthrough
#   oneway                   the archive carries everything and NiFi drops blobs the
#                            Redis ledger has seen
# Build the matching send flow first: scripts/nifi-flow.py --mode "$TRANSFER_MODE" --reset
set -euo pipefail
here=$(cd "$(dirname "$0")/.." && pwd)
cd "$here"
if command -v docker >/dev/null; then :; else echo "docker required" >&2; exit 1; fi
mode=${TRANSFER_MODE:-bidirectional}
export TRANSFER_MODE=$mode
# What the archive that crosses is called: oneway repacks and renames it, bidirectional
# passes the exporter's file through untouched.
suffix=$([ "$mode" = oneway ] && echo "-dedup" || echo "")
echo "== mode: $mode"

wait_for_transfer() { # name -> waits until the receive flow has unpacked it
  for _ in $(seq 1 60); do [ -f "data/high-store/incoming/$1/manifest.json" ] && { sleep 15; return; }; sleep 5; done
  echo "transfer $1 did not arrive on the high side" >&2; exit 1
}
# The trailing character matters: the exporter's tar carries a `oci/blobs/sha256/`
# directory entry that MergeContent's does not, and without it the two modes would report
# counts one apart for identical content.
crossed() { tar -tf "data/diode/received/$1$suffix.tar" | grep -cE 'blobs/sha256/[0-9a-f]' || true; }
size_of() { du -h "data/diode/received/$1$suffix.tar" | cut -f1; }
redis_keys() { docker compose exec -T dedupe-redis redis-cli dbsize; }
export_name() { scripts/export.sh 3 | tee /dev/stderr | sed -n 's#^wrote data/low-export/\(transfer-[^.]*\)\.tar.*#\1#p'; }  # runs the export, prints its name

echo "== 1. seed the low side"
scripts/seed-images.sh 1.0.0 1.1.0 1.2.0 2.0.0 | grep -v '^  '
echo "== 2. transfer 1: newest three versions per repository"
t1=$(export_name); wait_for_transfer "$t1"
echo "   crossed: $(crossed "$t1") blobs in $(size_of "$t1"), redis keys: $(redis_keys)"
scripts/import.sh | grep -E '^(transfer|missing|published)'
scripts/verify.sh demo/app 2.0.0 1.2.0; scripts/verify.sh demo/api 2.0.0 1.2.0
echo "== 3. transfer 2: a new release, only its new blobs cross"
scripts/seed-images.sh 2.1.0 | grep pushed
t2=$(export_name); wait_for_transfer "$t2"
echo "   crossed: $(crossed "$t2") blobs in $(size_of "$t2") (expect 6: 2 version layers, 2 configs, 2 manifests), redis keys: $(redis_keys)"
scripts/import.sh | grep -E '^(transfer|missing|published)'
scripts/verify.sh demo/app 2.1.0 2.0.0; scripts/verify.sh demo/api 2.1.0
echo "== 4. transfer 3: the same export again, nothing crosses"
t3=$(export_name); wait_for_transfer "$t3"
echo "   crossed: $(crossed "$t3") blobs in $(size_of "$t3") (expect 0), redis keys: $(redis_keys)"
scripts/import.sh | grep -E '^(transfer|missing|published)'
echo "== done: store holds $(find data/high-store/oci/blobs/sha256 -type f | wc -l | tr -d ' ') blobs, $(python3 -c 'import json;print(len(json.load(open("data/high-store/oci/index.json"))["manifests"]))') images"
