#!/usr/bin/env bash
# First-run bootstrap of one Quay: wait for it, create the superuser through the API,
# keep its OAuth token in .secrets/SIDE.token (0600), create the demo organisation.
#   usage: scripts/quay-init.sh SIDE URL            e.g. scripts/quay-init.sh low http://localhost:18081
# Idempotent: a second run finds the user exists and only re-checks the org.
set -euo pipefail
side=${1:?side}; url=${2:?url}
here=$(cd "$(dirname "$0")/.." && pwd)
user=${QUAY_USER:-admin}; pass=${QUAY_PASS:-quayadmin123}; org=${QUAY_ORG:-demo}
mkdir -p "$here/.secrets"; umask 077
tok="$here/.secrets/$side.token"

for _ in $(seq 1 60); do [ "$(curl -s -o /dev/null -w "%{http_code}" "$url/v2/")" = 401 ] && break; sleep 5; done
[ "$(curl -s -o /dev/null -w "%{http_code}" "$url/v2/")" = 401 ] || { echo "$url not up after 5 min" >&2; exit 1; }

if [ ! -s "$tok" ]; then
  # Only works while no user exists (FEATURE_USER_INITIALIZE); returns an OAuth token for the superuser.
  resp=$(curl -fsS -X POST "$url/api/v1/user/initialize" -H 'Content-Type: application/json' \
    -d "{\"username\":\"$user\",\"password\":\"$pass\",\"email\":\"$user@example.com\",\"access_token\":true}") \
    || { echo "initialize failed (user may already exist; delete .secrets/$side.token only if you know the token)" >&2; exit 1; }
  printf '%s' "$resp" | python3 -c 'import json,sys; print(json.load(sys.stdin)["access_token"])' > "$tok"
  echo "$side: superuser $user created, token saved to .secrets/$side.token"
fi

auth="Authorization: Bearer $(cat "$tok")"
if curl -fs -H "$auth" "$url/api/v1/organization/$org" >/dev/null 2>&1; then
  echo "$side: organisation $org exists"
else
  curl -fsS -X POST -H "$auth" -H 'Content-Type: application/json' "$url/api/v1/organization/" \
    -d "{\"name\":\"$org\",\"email\":\"$org@example.com\"}" >/dev/null
  echo "$side: organisation $org created"
fi
echo "$side: docker login ${url#*://} -u $user"
