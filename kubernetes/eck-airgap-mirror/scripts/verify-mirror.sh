#!/usr/bin/env bash
# verify-mirror.sh: probe the package registry and the file store with the same
# requests Kibana, Fleet, Elasticsearch and Elastic Endpoint make.
# Exits 1 on the first failed check. Needs: bash, curl, jq, python3.
#
#   verify-mirror.sh -v 9.5.2 \
#     -e https://epr.elastic-mirror.svc:8080 \
#     -f https://minio.elastic-mirror.svc:9000/elastic-mirror \
#     -b https://minio.elastic-mirror.svc:9000/elastic-kb \
#     -c /tmp/mirror-ca.crt
#
# -v  stack version
# -e  package registry base URL
# -f  file store URL of the bucket that holds downloads/, geoip/ and models/
# -b  file store URL of the bucket that holds the knowledge base
# -c  CA file that signed the mirror certificate (curl --cacert)
set -euo pipefail
V=""; EPR=""; FILES=""; KB=""; CA=""
while [ $# -gt 0 ]; do
  case "$1" in
    -v) V="$2"; shift 2 ;;
    -e) EPR="${2%/}"; shift 2 ;;
    -f) FILES="${2%/}"; shift 2 ;;
    -b) KB="${2%/}"; shift 2 ;;
    -c) CA="$2"; shift 2 ;;
    *) sed -n 2,16p "$0"; exit 2 ;;
  esac
done
[ -n "$V" ] || { sed -n 2,16p "$0"; exit 2; }
CURL_CA=(); [ -z "$CA" ] || CURL_CA=(--cacert "$CA")
PASS=0
ok() { PASS=$((PASS + 1)); printf 'ok   %s\n' "$*"; }
warn() { printf 'warn %s\n' "$*"; }
die() { printf 'FAIL %s\n' "$*" >&2; exit 1; }
get() { curl -fsS ${CURL_CA[@]+"${CURL_CA[@]}"} -m 120 "$@"; }
head_ok() { curl -fsI ${CURL_CA[@]+"${CURL_CA[@]}"} -m 30 -o /dev/null "$1"; }

if [ -n "$EPR" ]; then
  get "$EPR/" >/dev/null || die "registry root"
  ok "registry answers at $EPR/"
  c=$(get "$EPR/categories?kibana.version=$V" | jq length)
  [ "$c" -gt 0 ] || die "registry /categories is empty"
  ok "registry /categories: $c categories"
  n=$(get "$EPR/search?kibana.version=$V&prerelease=false" | jq length)
  [ "$n" -gt 0 ] || die "registry /search returned no packages for Kibana $V"
  ok "registry /search: $n packages compatible with Kibana $V"
  for p in system fleet_server elastic_agent endpoint security_detection_engine; do
    pv=$(get "$EPR/search?package=$p&kibana.version=$V" | jq -r '.[0].version // empty')
    [ -n "$pv" ] || { warn "registry has no $p for Kibana $V (the lite image holds a subset)"; continue; }
    get "$EPR/package/$p/$pv/" | jq -e --arg p "$p" '.name == $p' >/dev/null || die "registry /package/$p/$pv"
    get -o /dev/null "$EPR/epr/$p/$p-$pv.zip" || die "registry zip download $p-$pv"
    ok "registry package $p $pv (metadata and zip download)"
  done
fi

if [ -n "$FILES" ]; then
  tmp=$(mktemp -d)
  trap 'rm -rf "$tmp"' EXIT
  url="$FILES/downloads/endpoint/manifest/artifacts-$V.zip"
  hdr=$(curl -sI ${CURL_CA[@]+"${CURL_CA[@]}"} -m 30 "$url" | tr -d '\r')
  head -1 <<<"$hdr" | grep -q ' 200' || die "endpoint manifest artifacts-$V.zip not served"
  etag=$(awk 'tolower($1)=="etag:"{print $2}' <<<"$hdr")
  # Endpoint refuses an ETag with a dash (a multipart upload) and never refreshes.
  [[ "$etag" =~ ^\"?[0-9a-f]{32}\"?$ ]] || die "endpoint manifest ETag '$etag' is not a plain MD5"
  ok "endpoint manifest ETag is a plain MD5"
  get -o "$tmp/m.zip" "$url"
  python3 -I -c 'import sys,zipfile; open(sys.argv[2],"wb").write(zipfile.ZipFile(sys.argv[1]).read("manifest.json"))' "$tmp/m.zip" "$tmp/m.json"
  ok "endpoint manifest_version $(jq -r .manifest_version "$tmp/m.json")"
  # Endpoint requests each artifact as <base><relative_url>, and relative_url starts
  # with /downloads, so the Defend base_url is the bucket URL ($FILES).
  miss=0
  while read -r r; do
    head_ok "$FILES$r" || { miss=$((miss + 1)); echo "missing $r" >&2; }
  done < <(jq -r '.artifacts[].relative_url' "$tmp/m.json")
  [ "$miss" = 0 ] || die "$miss endpoint artifacts missing"
  ok "all $(jq '.artifacts|length' "$tmp/m.json") endpoint artifacts present"

  f="$FILES/downloads/beats/elastic-agent/elastic-agent-$V-linux-x86_64.tar.gz"
  if head_ok "$f"; then ok "agent binary served"; else warn "agent binary not served: $f"; fi

  if get "$FILES/geoip/overview.json" 2>/dev/null | jq -e 'length > 0 and all(.[]; .name and .md5_hash)' >/dev/null; then
    ok "GeoIP overview.json is valid"
  else warn "no valid GeoIP overview.json at $FILES/geoip/"; fi
  for m in elser_model_2.metadata.json multilingual-e5-small.metadata.json rerank-v1.metadata.json; do
    if head_ok "$FILES/models/$m"; then ok "model file $m"; else warn "model file $m not served"; fi
  done
fi

if [ -n "$KB" ]; then
  if get "$KB/" 2>/dev/null | grep -q '<ListBucketResult'; then ok "knowledge base listing at $KB/"; else warn "no knowledge base listing at $KB/"; fi
fi
echo "$PASS checks passed"
