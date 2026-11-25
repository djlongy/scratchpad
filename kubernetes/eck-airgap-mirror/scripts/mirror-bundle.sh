#!/usr/bin/env bash
# mirror-bundle.sh: export everything an air-gapped Elastic Stack needs for one
# stack version into one bundle directory with a sha256 manifest.
# Run on an internet-connected host. Needs: bash, curl, jq, skopeo, python3, unzip.
#
#   mirror-bundle.sh -v 9.5.2 -o ./bundle --dry-run     # sizes only, downloads nothing
#   mirror-bundle.sh -v 9.5.2 -o ./bundle               # full export
#
# Bundle layout (load-bundle.sh expects it):
#   images/*.tar                 docker-archive per image (+ images.tsv: name, digest)
#   artifacts/downloads/...      agent binaries (artifacts.elastic.co layout) and
#                                endpoint artifacts (artifacts.security.elastic.co layout)
#   geoip/*.tgz + overview.json  custom GeoIP endpoint content
#   models/elser_model_2*        ML model repository content
#   kb/*.zip + index.xml         AI Assistant knowledge base artifact repository
#   MANIFEST.sha256              sha256sum -c compatible, paths relative to the bundle
set -euo pipefail

VERSION=""; OUT=""; DRY=0
ECK_VERSION="${ECK_VERSION:-3.5.0}"
# Components: images agent endpoint geoip models kb
COMPONENTS="${COMPONENTS:-images agent endpoint geoip models kb}"
# EPR variant: production (every integration, newest rules), full (every integration
# as released with VERSION), lite (common subset), none
EPR_VARIANT="${EPR_VARIANT:-production}"
IMAGE_ARCH="${IMAGE_ARCH:-amd64}"
# Extra images (space separated, repo:tag) appended to the list, e.g. elastic-agent-complete
EXTRA_IMAGES="${EXTRA_IMAGES:-}"
# Static web server image, only needed when you serve the files with a web server
WEB_IMAGE="${WEB_IMAGE:-}"
# Agent matrix: "<url dir>:<artifact name>" and arch/package lists
AGENT_ARTIFACTS="${AGENT_ARTIFACTS:-beats/elastic-agent:elastic-agent endpoint-dev:endpoint-security fleet-server:fleet-server}"
AGENT_ARCHS="${AGENT_ARCHS:-linux-x86_64}"          # linux-x86_64 linux-arm64 windows-x86_64 darwin-x86_64 darwin-aarch64
AGENT_PACKAGES="${AGENT_PACKAGES:-tar.gz}"           # tar.gz deb rpm msi (msi: windows only, deb/rpm: linux only)
# ML models: "<base name>:<variant,variant>", variant generic is the file without a suffix.
# The built-in inference endpoints use elser_model_2 (.elser-2-elasticsearch),
# multilingual-e5-small (.multilingual-e5-small-elasticsearch) and rerank-v1
# (.rerank-v1-elasticsearch, published without a platform variant).
MODELS="${MODELS:-elser_model_2:linux-x86_64,generic multilingual-e5-small:linux-x86_64,generic rerank-v1:generic}"
KB_PRODUCTS="${KB_PRODUCTS:-security kibana observability elasticsearch openapi}"
KB_SUFFIX="${KB_SUFFIX:-}"                           # e.g. --.multilingual-e5-small-elasticsearch ; empty = default (ELSER) artifacts
ARTIFACTS_BASE="https://artifacts.elastic.co/downloads"
ENDPOINT_BASE="https://artifacts.security.elastic.co"
GEOIP_URL="https://geoip.elastic.co/v1/database?elastic_geoip_service_tos=agree"
MODELS_BASE="https://ml-models.elastic.co"
KB_BASE="https://kibana-knowledge-base-artifacts.elastic.co"
EPR_PUBLIC="https://epr.elastic.co"

usage() { sed -n 2,12p "$0"; exit 2; }
while [ $# -gt 0 ]; do
  case "$1" in
    -v) VERSION="$2"; shift 2 ;;
    -o) OUT="$2"; shift 2 ;;
    --dry-run) DRY=1; shift ;;
    *) usage ;;
  esac
done
[ -n "$VERSION" ] && [ -n "$OUT" ] || usage
MAJMIN="${VERSION%.*}"
for t in curl jq python3; do command -v "$t" >/dev/null || { echo "missing tool: $t" >&2; exit 1; }; done
has() { case " $COMPONENTS " in *" $1 "*) return 0 ;; esac; return 1; }
has images && { command -v skopeo >/dev/null || { echo "missing tool: skopeo" >&2; exit 1; }; }
sha256() { if command -v sha256sum >/dev/null; then sha256sum "$@"; else shasum -a 256 "$@"; fi; }

TOTAL=0
mkdir -p "$OUT"
log() { printf '%s\n' "$*" >&2; }
addsize() { TOTAL=$((TOTAL + ${1:-0})); }
mib() { echo $(($1 / 1048576)); }

# fetch URL DEST: resumable, skips a finished file, --dry-run only sizes it.
fetch() {
  local url="$1" dest="$2" size
  size=$(curl -sIL -m 30 "$url" | tr -d '\r' | awk 'tolower($1)=="content-length:"{s=$2} END{print s+0}')
  addsize "$size"
  if [ "$DRY" = 1 ]; then printf '  %8s MiB  %s\n' "$(mib "$size")" "$dest" >&2; return 0; fi
  mkdir -p "$(dirname "$dest")"
  if [ -f "$dest" ] && [ "$(wc -c <"$dest" | tr -d ' ')" = "$size" ]; then return 0; fi
  curl -fL --retry 3 -C - -sS -o "$dest" "$url"
}
# fetch_optional: .sha512 / .asc files may be missing for some artifacts
fetch_optional() { curl -fsI -m 30 "$1" >/dev/null 2>&1 && fetch "$1" "$2" || true; }

# ---- images ---------------------------------------------------------------
do_images() {
  log "== images (linux/$IMAGE_ARCH, ECK operator $ECK_VERSION)"
  local list=(
    "docker.elastic.co/eck/eck-operator:${ECK_VERSION}"
    "docker.elastic.co/elasticsearch/elasticsearch:${VERSION}"
    "docker.elastic.co/kibana/kibana:${VERSION}"
    "docker.elastic.co/elastic-agent/elastic-agent:${VERSION}"
  )
  case "$EPR_VARIANT" in
    production) list+=("docker.elastic.co/package-registry/distribution:production") ;;
    full) list+=("docker.elastic.co/package-registry/distribution:${VERSION}") ;;
    lite) list+=("docker.elastic.co/package-registry/distribution:lite-${VERSION}") ;;
    none) ;;
    *) echo "EPR_VARIANT must be production, full, lite or none" >&2; exit 1 ;;
  esac
  [ -z "$WEB_IMAGE" ] || list+=("$WEB_IMAGE")
  # shellcheck disable=SC2206
  list+=($EXTRA_IMAGES)
  mkdir -p "$OUT/images"
  : >"$OUT/images/images.tsv.new"
  local img raw dig sz file tag
  for img in "${list[@]}"; do
    raw=$(skopeo inspect --raw "docker://$img" --override-os linux --override-arch "$IMAGE_ARCH")
    dig=$(jq -r --arg a "$IMAGE_ARCH" '[.manifests[]? | select(.platform.os=="linux" and .platform.architecture==$a) | .digest][0] // empty' <<<"$raw")
    if [ -n "$dig" ]; then raw=$(skopeo inspect --raw "docker://${img%:*}@$dig"); fi
    sz=$(jq '[.layers[].size] | add' <<<"$raw")
    addsize "$sz"
    file="$OUT/images/$(echo "${img#*/}" | tr '/:' '__').tar"
    if [ "$DRY" = 1 ]; then printf '  %8s MiB  %s\n' "$(mib "$sz")" "$img" >&2; continue; fi
    tag="$img"
    [ -f "$file" ] && [ -f "$file.ok" ] || {
      skopeo copy --override-os linux --override-arch "$IMAGE_ARCH" "docker://$img" "docker-archive:$file:$tag"
      touch "$file.ok"
    }
    rm -f "$file.ok"
    printf '%s\t%s\t%s\n' "$img" "${dig:-single-arch}" "images/$(basename "$file")" >>"$OUT/images/images.tsv.new"
  done
  [ "$DRY" = 1 ] || mv "$OUT/images/images.tsv.new" "$OUT/images/images.tsv"
}

# ---- agent binaries -------------------------------------------------------
agent_files() { # print "<dir>/<file>" for the matrix
  local spec dir name arch pkg
  for spec in $AGENT_ARTIFACTS; do
    dir="${spec%%:*}"; name="${spec##*:}"
    for arch in $AGENT_ARCHS; do
      for pkg in $AGENT_PACKAGES; do
        case "$arch:$pkg" in
          windows-*:msi|windows-*:zip) echo "$dir/$name-$VERSION-$arch.$pkg" ;;
          windows-*:*) ;;
          darwin-*:tar.gz) echo "$dir/$name-$VERSION-$arch.tar.gz" ;;
          darwin-*:*) ;;
          linux-*:tar.gz) echo "$dir/$name-$VERSION-$arch.tar.gz" ;;
          linux-x86_64:deb) echo "$dir/$name-$VERSION-amd64.deb" ;;
          linux-arm64:deb) echo "$dir/$name-$VERSION-arm64.deb" ;;
          linux-x86_64:rpm) echo "$dir/$name-$VERSION-x86_64.rpm" ;;
          linux-arm64:rpm) echo "$dir/$name-$VERSION-aarch64.rpm" ;;
        esac
      done
    done
  done
}
do_agent() {
  log "== agent binaries ($AGENT_ARCHS, $AGENT_PACKAGES)"
  local f
  while read -r f; do
    [ -n "$f" ] || continue
    # skip combinations Elastic does not publish (e.g. fleet-server deb)
    curl -fsI -m 30 "$ARTIFACTS_BASE/$f" >/dev/null 2>&1 || { log "  not published: $f"; continue; }
    fetch "$ARTIFACTS_BASE/$f" "$OUT/artifacts/downloads/$f"
    fetch_optional "$ARTIFACTS_BASE/$f.sha512" "$OUT/artifacts/downloads/$f.sha512"
    fetch_optional "$ARTIFACTS_BASE/$f.asc" "$OUT/artifacts/downloads/$f.asc"
  done < <(agent_files)
  # Upgrade signature key: Fleet Server serves it as elastic-agent-upgrade-keys/default.pgp
  fetch "https://artifacts.elastic.co/GPG-KEY-elastic-agent" "$OUT/artifacts/GPG-KEY-elastic-agent"
}

# ---- endpoint (Elastic Defend) global artifacts ---------------------------
do_endpoint() {
  log "== endpoint artifacts for $VERSION"
  local mdir="$OUT/artifacts/downloads/endpoint/manifest" zip rel
  zip="$mdir/artifacts-$VERSION.zip"
  mkdir -p "$mdir"
  curl -fsS -m 60 -o "$zip" "$ENDPOINT_BASE/downloads/endpoint/manifest/artifacts-$VERSION.zip"
  # manifest.json is the first zip member; the doc mirrors it with zcat
  python3 -I - "$zip" "$mdir/.manifest.json" <<'PY'
import sys, zipfile
z = zipfile.ZipFile(sys.argv[1])
open(sys.argv[2], "wb").write(z.read("manifest.json"))
PY
  local n=0 size=0 s
  while read -r rel s; do
    # relative_url comes from a remote manifest: it must stay under $OUT/artifacts
    case "$rel" in /*..*|*/../*|*/..|"") echo "unsafe artifact path in manifest: $rel" >&2; return 1 ;; /*) ;; *) echo "unsafe artifact path in manifest: $rel" >&2; return 1 ;; esac
    n=$((n + 1)); size=$((size + s)); addsize "$s"
    [ "$DRY" = 1 ] && continue
    mkdir -p "$OUT/artifacts$(dirname "$rel")"
    [ -f "$OUT/artifacts$rel" ] || curl -fsS --retry 3 -o "$OUT/artifacts$rel" "$ENDPOINT_BASE$rel"
  done < <(jq -r '.artifacts | to_entries[] | "\(.value.relative_url) \(.value.encoded_size)"' "$mdir/.manifest.json")
  log "  $n artifacts, $(mib "$size") MiB, manifest_version $(jq -r .manifest_version "$mdir/.manifest.json")"
  rm -f "$mdir/.manifest.json"
}

# ---- GeoIP ----------------------------------------------------------------
do_geoip() {
  log "== GeoIP databases (City, Country, ASN)"
  local tmp name url sz
  tmp=$(curl -fsS -m 60 "$GEOIP_URL")
  mkdir -p "$OUT/geoip"
  local keep='[.[] | select(.name | test("^GeoLite2-(City|Country|ASN)\\.tgz$"))]'
  while IFS=$'\t' read -r name url; do
    # presigned links answer GET only: read the total from a one-byte range request
    sz=$(curl -s -m 30 -r 0-0 -D - -o /dev/null "$url" | tr -d '\r' | awk 'tolower($1)=="content-range:"{split($3,a,"/"); print a[2]+0}')
    addsize "$sz"
    if [ "$DRY" = 1 ]; then printf '  %8s MiB  geoip/%s\n' "$(mib "$sz")" "$name" >&2; continue; fi
    curl -fsS --retry 3 -o "$OUT/geoip/$name" "$url"
  done < <(jq -r "$keep | .[] | [.name, .url] | @tsv" <<<"$tmp")
  # Elasticsearch GETs <endpoint>?elastic_geoip_service_tos=agree and follows each url:
  # relative urls resolve against the endpoint, so the bundle needs no signed links.
  jq "$keep | map(.url = .name)" <<<"$tmp" >"$OUT/geoip/overview.json"
}

# ---- ML models ------------------------------------------------------------
do_models() {
  log "== ML model files ($MODELS)"
  local spec name v base ext
  for spec in $MODELS; do
    name="${spec%%:*}"
    for v in $(tr ',' ' ' <<<"${spec#*:}"); do
      base="$name"; [ "$v" = generic ] || base="${name}_$v"
      for ext in metadata.json pt vocab.json; do
        fetch "$MODELS_BASE/$base.$ext" "$OUT/models/$base.$ext"
      done
    done
  done
}

# ---- AI Assistant knowledge base -----------------------------------------
do_kb() {
  log "== knowledge base artifacts for $MAJMIN (+ latest security-labs)"
  local listing keys k sz
  listing=$(curl -fsS -m 60 "$KB_BASE/")
  keys=$(python3 -I -c '
import re, sys
majmin, suffix, products = sys.argv[1], sys.argv[2], sys.argv[3].split()
xml = sys.stdin.read()
keys = re.findall(r"<Key>([^<]*)</Key>", xml)
want = {f"kb-product-doc-{p}-{majmin}{suffix}.zip" for p in products}
labs = sorted(k for k in keys if re.fullmatch(r"security-labs-\d{4}\.\d\d\.\d\d(-\d{6})?%s\.zip" % re.escape(suffix), k))
if labs:
    want.add(labs[-1])
print("\n".join(sorted(k for k in keys if k in want)))
' "$MAJMIN" "$KB_SUFFIX" "$KB_PRODUCTS" <<<"$listing")
  [ -n "$keys" ] || { echo "no knowledge base keys match $MAJMIN" >&2; return 1; }
  mkdir -p "$OUT/kb"
  for k in $keys; do
    sz=$(curl -sIL -m 30 "$KB_BASE/$k" | tr -d '\r' | awk 'tolower($1)=="content-length:"{s=$2} END{print s+0}')
    addsize "$sz"
    if [ "$DRY" = 1 ]; then printf '  %8s MiB  kb/%s\n' "$(mib "$sz")" "$k" >&2; continue; fi
    [ -f "$OUT/kb/$k" ] && [ "$(wc -c <"$OUT/kb/$k" | tr -d ' ')" = "$sz" ] || curl -fsS --retry 3 -o "$OUT/kb/$k" "$KB_BASE/$k"
  done
  [ "$DRY" = 1 ] && return 0
  # Kibana lists the repository root as an S3 ListBucketResult: keep only mirrored keys.
  # shellcheck disable=SC2086  # $keys is a newline-separated list of object keys
  python3 -I -c '
import re, sys
out, keep = sys.argv[1], set(sys.argv[2:])
xml = sys.stdin.read()
blocks = re.findall(r"<Contents>.*?</Contents>", xml, flags=re.S)
sel = [b for b in blocks if re.search(r"<Key>([^<]*)</Key>", b).group(1) in keep]
head = xml[: xml.index("<Contents>")]
open(out, "w").write(head + "".join(sel) + "</ListBucketResult>")
' "$OUT/kb/index.xml" $keys <<<"$listing"
}

# ---- run ------------------------------------------------------------------
for c in $COMPONENTS; do
  case "$c" in
    images) do_images ;; agent) do_agent ;; endpoint) do_endpoint ;;
    geoip) do_geoip ;; models) do_models ;; kb) do_kb ;;
    *) echo "unknown component: $c" >&2; exit 1 ;;
  esac
done

if [ "$DRY" = 1 ]; then
  log "== total download: $(mib "$TOTAL") MiB (compressed image layers + files; nothing written)"
  exit 0
fi

# Record rule/package versions the matching public registry serves for this stack
# (informational: the EPR image tagged $VERSION carries its own, older set).
{
  echo "stack_version=$VERSION"
  echo "eck_operator_version=$ECK_VERSION"
  echo "epr_variant=$EPR_VARIANT"
  echo "exported_utc=$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  for p in security_detection_engine endpoint fleet_server system elastic_agent; do
    echo "public_epr_${p}=$(curl -fsS -m 30 "$EPR_PUBLIC/search?package=$p&kibana.version=$VERSION" | jq -r '.[0].version // "n/a"')"
  done
} >"$OUT/bundle.env"

log "== sha256 manifest"
(
  cd "$OUT"
  rm -f MANIFEST.sha256
  find . -type f ! -name MANIFEST.sha256 ! -name '*.ok' ! -name '*.new' -print0 | LC_ALL=C sort -z |
    while IFS= read -r -d '' f; do sha256 "$f"; done >MANIFEST.sha256.new
  mv MANIFEST.sha256.new MANIFEST.sha256
)
log "done: $(wc -l <"$OUT/MANIFEST.sha256" | tr -d ' ') files, $(du -sh "$OUT" | cut -f1) in $OUT"
