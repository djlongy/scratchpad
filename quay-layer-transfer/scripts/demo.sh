#!/usr/bin/env bash
# The whole proof in one run, against a freshly started stack (compose up -d, both Quays
# initialised, NiFi flows built). Three transfers:
#   1. first export of the newest three versions: every blob crosses
#   2. a new patch release: only its new layer, config and manifest cross
#   3. the same export again: nothing crosses
# Each transfer is imported into the high Quay and pulled back.
set -euo pipefail
here=$(cd "$(dirname "$0")/.." && pwd)
cd "$here"
if command -v docker >/dev/null; then :; else echo "docker required" >&2; exit 1; fi

wait_for_transfer() { # name -> waits until the receive flow has unpacked it
  for _ in $(seq 1 60); do [ -f "data/high-store/incoming/$1-dedup/manifest.json" ] && { sleep 15; return; }; sleep 5; done
  echo "transfer $1 did not arrive on the high side" >&2; exit 1
}
crossed() { tar -tf "data/diode/received/$1-dedup.tar" | grep -c 'blobs/sha256/' || true; }
redis_keys() { docker compose exec -T dedupe-redis redis-cli dbsize; }
export_name() { scripts/export.sh 3 | tee /dev/stderr | sed -n 's#^wrote data/low-export/\(transfer-[^.]*\)\.tar.*#\1#p'; }  # runs the export, prints its name

echo "== 1. seed the low side"
scripts/seed-images.sh 1.0.0 1.1.0 1.2.0 2.0.0 | grep -v '^  '
echo "== 2. transfer 1: newest three versions per repository"
t1=$(export_name); wait_for_transfer "$t1"
echo "   crossed: $(crossed "$t1") blobs, redis keys: $(redis_keys)"
scripts/import.sh | grep '^transfer'
scripts/verify.sh demo/app 2.0.0 1.2.0; scripts/verify.sh demo/api 2.0.0 1.2.0
echo "== 3. transfer 2: a new release, only its new blobs cross"
scripts/seed-images.sh 2.1.0 | grep pushed
t2=$(export_name); wait_for_transfer "$t2"
echo "   crossed: $(crossed "$t2") blobs (expect 6: 2 version layers, 2 configs, 2 manifests), redis keys: $(redis_keys)"
scripts/import.sh | grep '^transfer'
scripts/verify.sh demo/app 2.1.0 2.0.0; scripts/verify.sh demo/api 2.1.0
echo "== 4. transfer 3: the same export again, nothing crosses"
t3=$(export_name); wait_for_transfer "$t3"
echo "   crossed: $(crossed "$t3") blobs (expect 0), redis keys: $(redis_keys)"
scripts/import.sh | grep '^transfer'
echo "== done: store holds $(find data/high-store/oci/blobs/sha256 -type f | wc -l | tr -d ' ') blobs, $(python3 -c 'import json;print(len(json.load(open("data/high-store/oci/index.json"))["manifests"]))') images"
