#!/usr/bin/env bash
# High side: prove the imported images are usable and identical to the low side.
#   usage: scripts/verify.sh ORG/REPO TAG [TAG...]     e.g. scripts/verify.sh demo/app 2.0.0 1.2.0
# For each tag: manifest digest on high == low, docker pull from high, run the container.
set -euo pipefail
repo=${1:?org/repo}; shift
low=${LOW_REGISTRY:-localhost:18081}; high=${HIGH_REGISTRY:-localhost:18082}
user=${QUAY_USER:-admin}; pass=${QUAY_PASS:-quayadmin123}
rc=0
printf '%s' "$pass" | docker login "$high" -u "$user" --password-stdin >/dev/null
for tag in "$@"; do
  dl=$(skopeo inspect --tls-verify=false --creds "$user:$pass" "docker://$low/$repo:$tag" | python3 -c 'import json,sys; print(json.load(sys.stdin)["Digest"])')
  dh=$(skopeo inspect --tls-verify=false --creds "$user:$pass" "docker://$high/$repo:$tag" | python3 -c 'import json,sys; print(json.load(sys.stdin)["Digest"])')
  if [ "$dl" != "$dh" ]; then echo "FAIL $repo:$tag digest differs: low $dl high $dh"; rc=1; continue; fi
  docker image rm -f "$high/$repo:$tag" >/dev/null 2>&1 || true
  docker pull -q "$high/$repo:$tag" >/dev/null
  out=$(docker run --rm "$high/$repo:$tag")
  echo "ok   $repo:$tag  digest ${dh:7:12}  pulled from high, runs: \"$out\""
done
exit $rc
