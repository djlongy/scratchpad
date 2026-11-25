#!/usr/bin/env bash
# load-bundle.sh: verify a bundle from mirror-bundle.sh, push its images to your
# internal registry and upload its files to the mirror's S3 store.
# Run on the air-gapped side. Needs: bash, curl 7.75 or later, skopeo (images only).
#
#   load-bundle.sh -b ./bundle-9.5.2 -r registry.example.internal/elastic \
#     -s https://127.0.0.1:9000 -c ./mirror-ca.crt
#
# -b  bundle directory
# -r  <registry>/<namespace> for the images (run `skopeo login` first). Omit to skip images.
# -s  S3 endpoint of the store. Omit to skip files.
# -c  CA file for the store (curl --cacert)
# Env: S3_USER, S3_PASSWORD (the write-only store user). Never pass them as arguments.
#
# Mapping: artifacts/* -> elastic-mirror/<path>, geoip/* and models/* -> elastic-mirror/<dir>/<file>,
# kb/*.zip -> elastic-kb/<file>. kb/index.xml is not uploaded: the store's own bucket
# listing is the document Kibana reads.
set -euo pipefail

BUNDLE=""; REG=""; S3=""; CA=""
while [ $# -gt 0 ]; do
  case "$1" in
    -b) BUNDLE="$2"; shift 2 ;;
    -r) REG="$2"; shift 2 ;;
    -s) S3="$2"; shift 2 ;;
    -c) CA="$2"; shift 2 ;;
    *) sed -n 2,17p "$0"; exit 2 ;;
  esac
done
[ -d "$BUNDLE" ] || { sed -n 2,17p "$0"; exit 2; }

echo "== verifying sha256 manifest (fails closed)"
(
  cd "$BUNDLE"
  if command -v sha256sum >/dev/null; then sha256sum -c --quiet MANIFEST.sha256
  else shasum -a 256 -c --quiet MANIFEST.sha256; fi
) || { echo "manifest check failed, nothing loaded" >&2; exit 1; }
echo "manifest OK"

if [ -n "$REG" ] && [ -f "$BUNDLE/images/images.tsv" ]; then
  echo "== images -> $REG"
  while IFS=$'\t' read -r img _dig file; do
    # docker.elastic.co/kibana/kibana:9.5.2 -> <registry>/<ns>/kibana/kibana:9.5.2
    dest="docker://$REG/${img#*/}"
    skopeo copy "docker-archive:$BUNDLE/$file" "$dest"
    # docker-archive rebuilds the manifest: pin this digest, not the upstream one.
    echo "pushed $dest $(skopeo inspect --format '{{.Digest}}' "$dest")"
  done <"$BUNDLE/images/images.tsv"
fi

[ -n "$S3" ] || { echo "files skipped (no -s)"; exit 0; }
: "${S3_USER:?set S3_USER}" "${S3_PASSWORD:?set S3_PASSWORD}"
cacert=(); [ -z "$CA" ] || cacert=(--cacert "$CA")

put() { # <local file> <bucket/key>
  local code
  # One PUT per object, never multipart: the store then returns the MD5 as the ETag,
  # the only form Elastic Endpoint accepts. The largest file is under 1 GiB.
  code=$(curl -sS "${cacert[@]}" --aws-sigv4 aws:amz:us-east-1:s3 -u "$S3_USER:$S3_PASSWORD" \
    -T "$1" -o /dev/null -w '%{http_code}' "$S3/$2")
  [ "$code" = 200 ] || { echo "PUT $2: HTTP $code" >&2; exit 1; }
}

echo "== files -> $S3"
n=0
cd "$BUNDLE"
while IFS= read -r -d '' f; do
  f="${f#./}"
  case "$f" in
    artifacts/*) put "$f" "elastic-mirror/${f#artifacts/}" ;;
    geoip/*|models/*) put "$f" "elastic-mirror/$f" ;;
    kb/*.zip) put "$f" "elastic-kb/${f#kb/}" ;;
    *) continue ;;
  esac
  n=$((n + 1))
done < <(find artifacts geoip models kb -type f -print0 2>/dev/null)
echo "uploaded $n files"
