#!/usr/bin/env bash
# Mirror a pinned RKE2 release's image set: resolve, fetch, vet, and on publish
# push, sign, and export one transfer archive for an air-gapped destination.
#
#   MIRROR_PUBLISH=false ./image-mirror/mirror.sh   # merge request: resolve + fetch + vet
#   MIRROR_PUBLISH=true  ./image-mirror/mirror.sh   # default branch: + publish, sign, export
#
# Needs skopeo, syft, grype, curl, python3. Publishing also needs cosign, and
# REGISTRY / REGISTRY_USER / REGISTRY_TOKEN. The object store (baseline and
# export) is optional and off unless S3_ENDPOINT is set; when it is, aws-cli and
# AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY are needed too.
#
# No credential is ever passed on a command line or echoed.
set -euo pipefail

here=$(cd "$(dirname "$0")" && pwd)
cfg=${MIRROR_CONFIG:-$here/rke2.yaml}
out=${MIRROR_OUT:-$PWD/out}
publish=${MIRROR_PUBLISH:-false}

# Gate: ratchet (default) fails only when a fixable finding at or above
# GATE_SEVERITY appears that the last published release did not carry (the
# baseline lives in the object store). strict fails on any fixable finding at
# that severity. off never fails. Never wrap the gate in `|| true`.
gate=${GATE:-ratchet}
gate_severity=${GATE_SEVERITY:-critical}
baseline_object=${BASELINE_OBJECT:-image-mirror/baseline/vulns.json}

# Signing key: a local private key file, or a KMS URI understood by cosign
# (hashivault://<key>, awskms://..., gcpkms://..., azurekms://...).
cosign_key=${COSIGN_KEY:-}

# Object store. Empty endpoint = not configured: the ratchet has no baseline to
# read, and nothing is exported. That is what makes a vet-only run need no
# credentials at all.
s3_endpoint=${S3_ENDPOINT:-}
s3_bucket=${S3_BUCKET:-}
s3_prefix=${S3_PREFIX:-image-diode/low-export}

# The namespace every mirrored image lands under on the destination registry.
org=${MIRROR_ORG:-rke2}

work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
mkdir -p "$out/sbom" "$out/scan"

# Repository path is the upstream path with the registry host stripped, placed
# under one org, so the whole set is one mirror rewrite rule on the consumer:
#   docker.io/rancher/hardened-kubernetes:tag -> rke2/rancher/hardened-kubernetes:tag
target_of() {
  local img=$1 first=${1%%/*}
  case $first in
    *.* | *:* | localhost) img=${img#*/} ;;
  esac
  printf '%s/%s' "$org" "$img"
}

# --- resolve -----------------------------------------------------------------
read -r version arch lists < <(python3 - "$cfg" <<'PY'
import sys

cfg, lists = {}, []
for raw in open(sys.argv[1]):
    line = raw.split('#', 1)[0].strip()
    if not line:
        continue
    if line.startswith('- '):
        lists.append(line[2:].strip())
        continue
    key, _, value = line.partition(':')
    if value.strip():
        cfg[key.strip()] = value.strip()
print(cfg['version'], cfg['arch'], ' '.join(lists))
PY
)
[ -n "${version:-}" ] && [ -n "${lists:-}" ] || { echo "cannot parse $cfg" >&2; exit 1; }
os=${arch%%-*}
cpu=${arch##*-}
# The release tag carries a '+'. It is legal in a URL path but several proxies
# rewrite it to a space, so encode it.
base="https://github.com/rancher/rke2/releases/download/${version//+/%2B}"

echo "== resolve $version ($arch), lists: $lists"
: > "$work/raw"
for list in $lists; do
  curl -fsSL --retry 3 "$base/rke2-images-$list.$arch.txt" >> "$work/raw"
done
grep -v '^[[:space:]]*$' "$work/raw" | sort -u > "$out/images.txt"

while read -r img; do
  ref=$(target_of "$img")
  safe=${ref//\//_}
  printf '%s\t%s\t%s\n' "$img" "$ref" "${safe//:/_}"
done < "$out/images.txt" > "$work/map"
echo "   $(wc -l < "$out/images.txt" | tr -d ' ') image(s) -> $out/images.txt"

# --- fetch: one OCI layout for every image, so shared blobs are stored once ---
echo "== fetch -> $out/store"
# A public registry answers a blob fetch with 502 now and then; skopeo's own
# retry covers a single request, this loop covers the whole image. A copy that
# still fails after three rounds is a real outage and stops the run.
while IFS=$'\t' read -r img ref _; do
  for attempt in 1 2 3; do
    if skopeo copy -q --retry-times 3 --override-os "$os" --override-arch "$cpu" \
        "docker://$img" "oci:$out/store:$ref"; then
      break
    fi
    [ "$attempt" -lt 3 ] || { echo "fetch failed three times: $img" >&2; exit 1; }
    echo "   retry $attempt for $img" >&2; sleep $((attempt * 20))
  done
done < "$work/map"

# --- vet: syft SBOM per image, grype report per image, then the gate ---------
echo "== vet (syft cyclonedx + grype --only-fixed; gate=$gate at $gate_severity)"
# A scanner that cannot load its database fails every image the same way a real
# finding would: a grype older than the published database schema fails the
# migration and then writes an EMPTY report for every image, which reads as a
# clean scan. Update the database once, loudly, before the loop so that failure
# is named here and not read as N gated images. Pin grype no older than the
# schema it will download, and bump that pin with the database.
grype db update || { echo "grype database update failed; refusing to vet" >&2; exit 2; }
grype db status || true
while IFS=$'\t' read -r _ ref safe; do
  skopeo copy -q "oci:$out/store:$ref" "oci-archive:$work/img.tar"
  syft -q "oci-archive:$work/img.tar" -o cyclonedx-json > "$out/sbom/$safe.cdx.json"
  rm -f "$work/img.tar"
  grype "sbom:$out/sbom/$safe.cdx.json" --only-fixed -o json --file "$out/scan/$safe.json" || true
  # Anything without a report is the scanner breaking, which must never be
  # counted as a scan result. Distinct exit code so CI can tell them apart.
  if [ ! -s "$out/scan/$safe.json" ]; then
    echo "grype produced no report for $ref; scanner failure, not a finding" >&2
    exit 2
  fi
done < "$work/map"

# The baseline is what the last publish left behind. Missing (first run, or a
# one-way link with no return channel) means the ratchet has nothing to hold and
# reports only.
: > "$work/baseline.json"
if [ -n "$s3_endpoint" ]; then
  if aws --endpoint-url "$s3_endpoint" s3api get-object --bucket "$s3_bucket" \
       --key "$baseline_object" "$work/baseline.json" >/dev/null 2>"$work/baseline.err"; then
    echo "   baseline: $(python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); print(d["version"], "published", d["stamp"])' "$work/baseline.json")"
  else
    grep -q NoSuchKey "$work/baseline.err" || { echo "baseline unreadable: $(cat "$work/baseline.err")" >&2; exit 2; }
    : > "$work/baseline.json"
    echo "   baseline: none (first publish, or a one-way link); ratchet reports only"
  fi
else
  echo "   baseline: no object store configured (S3_ENDPOINT unset); ratchet reports only"
fi

python3 - "$out/scan" "$gate" "$gate_severity" "$work/baseline.json" "$out/vulns.json" "$version" <<'PY'
import json
import os
import sys

scan_dir, gate, gate_sev, baseline_path, vulns_out, version = sys.argv[1:7]
order = ["negligible", "low", "medium", "high", "critical"]
level = order.index(gate_sev.lower())
total = {sev: 0 for sev in order}
rows = []
found = {sev: set() for sev in order}          # cve ids per severity, fixable only
for name in sorted(os.listdir(scan_dir)):
    with open(os.path.join(scan_dir, name)) as handle:
        matches = json.load(handle).get("matches") or []
    counts = {sev: 0 for sev in order}
    for match in matches:
        sev = match["vulnerability"]["severity"].lower()
        if sev in counts:
            counts[sev] += 1
            total[sev] += 1
            found[sev].add(match["vulnerability"]["id"])
    if any(counts.values()):
        rows.append((name[:-5], counts))
print("-- grype summary (fixable only) --")
for name, counts in rows:
    print("   %-58s %s" % (name, " ".join("%s=%d" % (s[:4], counts[s]) for s in order if counts[s])))
print("   %-58s %s" % ("TOTAL", " ".join("%s=%d" % (s[:4], total[s]) for s in order if total[s]) or "clean"))

# What this run would publish as the next baseline.
snapshot = {"version": version, "stamp": "", "ids": {sev: sorted(found[sev]) for sev in order if found[sev]}}
json.dump(snapshot, open(vulns_out, "w"), indent=1)

gated = set()
for sev in order[level:]:
    gated |= found[sev]
if gate == "off":
    print("   gate: off")
    sys.exit(0)
if gate == "strict":
    print("   gate: strict, fixable >= %s: %d id(s)" % (gate_sev, len(gated)))
    sys.exit(1 if gated else 0)
if os.path.getsize(baseline_path):
    ids = json.load(open(baseline_path)).get("ids", {})
    baseline = set()
    for sev in order[level:]:
        baseline |= set(ids.get(sev, []))
    new = sorted(gated - baseline)
    gone = sorted(baseline - gated)
    print("   gate: ratchet at %s: %d id(s), %d in baseline, %d new, %d gone"
          % (gate_sev, len(gated), len(gated & baseline), len(new), len(gone)))
    for cve in new:
        print("      NEW " + cve)
    sys.exit(1 if new else 0)
print("   gate: ratchet at %s with no baseline: %d id(s), reporting only" % (gate_sev, len(gated)))
PY

if [ "$publish" != "true" ]; then
  echo "== done (MIRROR_PUBLISH=$publish, nothing pushed)"
  exit 0
fi

# --- publish -----------------------------------------------------------------
: "${REGISTRY:?set REGISTRY}" "${REGISTRY_USER:?set REGISTRY_USER}" "${REGISTRY_TOKEN:?set REGISTRY_TOKEN}"
: "${cosign_key:?set COSIGN_KEY to a key file or a cosign KMS URI}"
export REGISTRY REGISTRY_USER REGISTRY_TOKEN
authfile=$work/registry-auth.json
mkdir -p "$work/docker"
export DOCKER_CONFIG=$work/docker
# Built by python from the environment so the token never reaches a command
# line, a process listing, or the job log.
python3 - > "$authfile" <<'PY'
import base64
import json
import os
import sys

pair = "%s:%s" % (os.environ["REGISTRY_USER"], os.environ["REGISTRY_TOKEN"])
auth = base64.b64encode(pair.encode()).decode()
json.dump({"auths": {os.environ["REGISTRY"]: {"auth": auth}}}, sys.stdout)
PY
chmod 600 "$authfile"

echo "== publish -> $REGISTRY/$org, sign with $cosign_key"
: > "$out/digests.txt"
while IFS=$'\t' read -r _ ref safe; do
  skopeo copy -q --retry-times 3 --dest-authfile "$authfile" \
    "oci:$out/store:$ref" "docker://$REGISTRY/$ref"
  digest=$(skopeo inspect --authfile "$authfile" --format '{{.Digest}}' "docker://$REGISTRY/$ref")
  # A digest already signed with this key is left alone: cosign sign appends, so
  # a re-run would stack duplicate signatures on every tag.
  # --insecure-ignore-tlog: these signatures are made with --tlog-upload=false
  # (no transparency log on the far side), and verify checks the log by default.
  # Without it the guard never passes and every run adds another signature layer
  # to the .sig tag. Nothing errors; the tag just grows.
  if ! cosign verify --key "$cosign_key" --insecure-ignore-tlog=true "$REGISTRY/$ref@$digest" >/dev/null 2>&1; then
    cosign sign --key "$cosign_key" --yes --tlog-upload=false \
      -a "ci.commit.sha=${CI_COMMIT_SHA:-local}" \
      -a "ci.job.url=${CI_JOB_URL:-local}" \
      "$REGISTRY/$ref@$digest"
  fi
  if ! cosign verify-attestation --key "$cosign_key" --type cyclonedx --insecure-ignore-tlog=true "$REGISTRY/$ref@$digest" >/dev/null 2>&1; then
    cosign attest --key "$cosign_key" --type cyclonedx --yes --tlog-upload=false \
      --predicate "$out/sbom/$safe.cdx.json" \
      "$REGISTRY/$ref@$digest"
  fi
  printf '%s\t%s\n' "$ref" "$digest" >> "$out/digests.txt"
done < "$work/map"

if [ -z "$s3_endpoint" ]; then
  echo "== done: $(wc -l < "$out/digests.txt" | tr -d ' ') image(s) published (S3_ENDPOINT unset, no baseline written, no export)"
  exit 0
fi

# The ratchet's next baseline: what this release carries, now that it is
# published. Written after the pushes so a failed publish leaves the old one.
python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); d["stamp"]=sys.argv[2]; json.dump(d, open(sys.argv[1],"w"), indent=1)' "$out/vulns.json" "$(date -u +%Y%m%dT%H%M%SZ)"
aws --endpoint-url "$s3_endpoint" s3 cp "$out/vulns.json" "s3://$s3_bucket/$baseline_object" --only-show-errors
echo "   baseline published: $baseline_object"

# --- export: the transfer archive for the one-way link ------------------------
stamp=$(date -u +%Y%m%dT%H%M%SZ)
mv "$out/store" "$out/oci"
# Same manifest shape as quay-layer-transfer/scripts/export.sh, so the same
# importer (oci-merge.py) reads both.
python3 - "$out" "$stamp" $(cut -f2 "$work/map") > "$out/manifest.json" <<'PY'
import json, sys
work, stamp, refs = sys.argv[1], sys.argv[2], sys.argv[3:]
index = json.load(open(f"{work}/oci/index.json"))
by_ref = {m["annotations"]["org.opencontainers.image.ref.name"]: m["digest"] for m in index["manifests"]}
images = []
for ref in refs:
    digest = by_ref[ref]
    man = json.load(open(f"{work}/oci/blobs/sha256/{digest.split(':')[1]}"))
    images.append({"ref": ref, "digest": digest, "config": man["config"]["digest"],
                   "layers": [layer["digest"] for layer in man["layers"]]})
json.dump({"transfer": stamp, "images": images}, sys.stdout, indent=1)
PY

# Checksums back: the far-side importer publishes the digests its OCI store
# already holds at <prefix root>/have/blobs.txt. Those blobs are left out of the
# archive, so only new layers cross. manifest.json still lists every layer,
# which is what lets the importer prove completeness on the far side.
# No file (a strict one-way link, or a first run) means send everything.
have_key="${s3_prefix%/low-export}/have/blobs.txt"
: > "$work/exclude"
# get-object, not head-object: some S3-compatible gateways answer HeadObject
# with HTTP 400 for aws-cli 2.x, which would read as "no file" and silently send
# every blob again.
if aws --endpoint-url "$s3_endpoint" s3api get-object --bucket "$s3_bucket" --key "$have_key" "$work/have.txt" >/dev/null 2>"$work/have.err"; then
  while read -r d; do
    b=${d#sha256:}
    # `if`, not `[ ... ] && printf`: under `set -e` a final iteration whose test fails
    # would make the loop the failing command and abort the run just before the export.
    if [ -f "$out/oci/blobs/sha256/$b" ]; then printf 'oci/blobs/sha256/%s\n' "$b"; fi
  done < "$work/have.txt" > "$work/exclude"
else
  grep -q NoSuchKey "$work/have.err" || { echo "have/blobs.txt unreadable: $(cat "$work/have.err")" >&2; exit 2; }
fi
echo "   checksums back: $(wc -l < "$work/exclude" | tr -d ' ') blob(s) already on the far side, left out"
# COPYFILE_DISABLE: macOS tar would add AppleDouble ._* entries, which the
# receiving flow would count as blobs.
COPYFILE_DISABLE=1 tar -C "$out" -X "$work/exclude" -cf "$out/transfer-$stamp.tar" oci manifest.json
mv "$out/oci" "$out/store"
aws --endpoint-url "$s3_endpoint" s3 cp "$out/transfer-$stamp.tar" \
  "s3://$s3_bucket/$s3_prefix/"
echo "== done: transfer-$stamp.tar ($(wc -l < "$out/digests.txt" | tr -d ' ') images) in s3://$s3_bucket/$s3_prefix/"
