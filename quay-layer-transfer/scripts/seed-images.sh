#!/usr/bin/env bash
# Build a semver image family on the low side: ORG/app and ORG/api, several versions each,
# sharing base and runtime layers. Also tags "latest" and "dev" that the export must ignore.
#   usage: scripts/seed-images.sh [VERSION ...]      default: 1.0.0 1.1.0 1.2.0 2.0.0
# Needs: docker (pointed at the daemon that can reach the low registry), .secrets/low.token
set -euo pipefail
here=$(cd "$(dirname "$0")/.." && pwd)
reg=${LOW_REGISTRY:-localhost:18081}; org=${QUAY_ORG:-demo}
user=${QUAY_USER:-admin}; pass=${QUAY_PASS:-quayadmin123}
versions=("$@"); [ ${#versions[@]} -gt 0 ] || versions=(1.0.0 1.1.0 1.2.0 2.0.0)

printf '%s' "$pass" | docker login "$reg" -u "$user" --password-stdin >/dev/null
for app in app api; do
  for v in "${versions[@]}"; do
    docker build -q --build-arg APP=$app --build-arg VERSION="$v" -t "$reg/$org/$app:$v" "$here/seed" >/dev/null
    docker push -q "$reg/$org/$app:$v" >/dev/null
    echo "pushed $org/$app:$v"
  done
  # non-semver tags: must not be selected by the export
  newest=${versions[$((${#versions[@]} - 1))]}   # bash 3.2 has no negative indexes
  docker tag "$reg/$org/$app:$newest" "$reg/$org/$app:latest"; docker push -q "$reg/$org/$app:latest" >/dev/null
  docker tag "$reg/$org/$app:${versions[0]}" "$reg/$org/$app:dev";        docker push -q "$reg/$org/$app:dev" >/dev/null
done
echo "layers per image (shared digests show the dedupe potential):"
for app in app api; do
  for v in "${versions[@]}"; do
    printf '  %-14s ' "$app:$v"; skopeo inspect --tls-verify=false --creds "$user:$pass" "docker://$reg/$org/$app:$v" | python3 -c 'import json,sys; print(" ".join(l.split(":")[1][:8] for l in json.load(sys.stdin)["Layers"]))'
  done
done
