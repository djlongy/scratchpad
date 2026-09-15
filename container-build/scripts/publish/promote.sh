#!/usr/bin/env bash
# ─── DO NOT EDIT — template publish job ────────────────────────────
# Behaviour comes from the publish job's variables (RELEASE_REGISTRY,
# RELEASE_REPOSITORY, RELEASE_REGISTRY_USER/PASSWORD) and from the
# identity + evidence the build and scan jobs left on disk.
# ───────────────────────────────────────────────────────────────────
#
# scripts/publish/promote.sh — copy the verified candidate to the release
# registry, by digest.
#
# Promotion is a copy of bytes that already exist. This script never
# builds: it does not source, call or reimplement build.sh. The released
# image is the image the gates scanned, or there is no release.
#
# What it does, in order:
#   1. Reads the candidate identity from image.json. Registry transport
#      only — an OCI archive has no source registry to copy from.
#   2. Verifies every required gate's evidence against that digest:
#      the SBOM producer's subject.json, and the vulnerability gate's
#      scan-result.json (outcome and its own subject).
#   3. Optionally verifies the candidate's cosign signature at the source.
#   4. crane copy candidate@digest → release repository, tag and digest.
#   5. Reads the destination digest back and fails unless it matches.
#   6. Writes artifacts/<instance>/release.json.
#
# Exit codes:
#   0  the release destination holds the same digest the gates passed
#   1  anything else — missing evidence, a failed gate, a copy that did
#      not preserve the digest, a missing tool or credential
#
# Usage:
#   bash scripts/publish/promote.sh
#
# Environment (the publish job in pipelines/container.yml sets these):
#   RELEASE_REGISTRY            release registry host or URL
#   RELEASE_REPOSITORY          repository inside it, no tag
#   RELEASE_REGISTRY_USER       release credential — this job only
#   RELEASE_REGISTRY_PASSWORD   release credential — this job only
#   RELEASE_TAG                 destination tag. Empty takes the
#                               candidate's tag from image.json.
#   COSIGN_ENABLED              true makes a valid candidate signature a
#                               precondition of the copy. See D6 below.

set -euo pipefail

TEMPLATE_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
PROJECT_ROOT="${PROJECT_ROOT:-$(pwd)}"
export TEMPLATE_ROOT PROJECT_ROOT
cd "${PROJECT_ROOT}"

# shellcheck source=../lib/load-image-env.sh
. "${TEMPLATE_ROOT}/scripts/lib/load-image-env.sh"
# shellcheck source=../lib/artifact-names.sh
. "${TEMPLATE_ROOT}/scripts/lib/artifact-names.sh"
# shellcheck source=../lib/image-identity.sh
. "${TEMPLATE_ROOT}/scripts/lib/image-identity.sh"

_fail() { echo "ERROR: $*" >&2; exit 1; }

import_bamboo_vars
load_image_env || exit 1

for _tool in jq crane; do
  command -v "${_tool}" >/dev/null 2>&1 \
    || _fail "${_tool} is not on PATH. The runtime image in runtime/Dockerfile ships both."
done

# ════════════════════════════════════════════════════════════════════
# 1. The candidate
# ════════════════════════════════════════════════════════════════════
# image.json is the authoritative identity record, so it is what the
# release is built from — not build.env, which is a dotenv convenience
# that a stale run could have left behind.

IMAGE_JSON="${IMAGE_JSON:-image.json}"
[ -s "${IMAGE_JSON}" ] \
  || _fail "no ${PROJECT_ROOT}/${IMAGE_JSON}. The build job's artifacts did not reach this job, so there is no candidate to promote."

CANDIDATE_TRANSPORT="$(jq -r '.transport    // ""' "${IMAGE_JSON}")"
CANDIDATE_DIGEST="$(jq   -r '.digest       // ""' "${IMAGE_JSON}")"
CANDIDATE_REF="$(jq      -r '.reference    // ""' "${IMAGE_JSON}")"
CANDIDATE_REPO="$(jq     -r '.repository   // ""' "${IMAGE_JSON}")"
CANDIDATE_TAG="$(jq      -r '.tag          // ""' "${IMAGE_JSON}")"
CANDIDATE_KIND="$(jq     -r '.subject_kind // ""' "${IMAGE_JSON}")"

case "${CANDIDATE_TRANSPORT}" in
  registry) ;;
  oci-archive)
    _fail "this build produced an OCI archive, not a pushed candidate.
       There is no source registry to copy from, and promotion never
       rebuilds or re-pushes. Set candidate-registry so the build pushes
       a candidate, then promote that." ;;
  *)
    _fail "${IMAGE_JSON} records transport='${CANDIDATE_TRANSPORT:-<empty>}', which is not a transport this template writes." ;;
esac

# The shared checker owns the digest grammar and the reference shape, so
# a registry hand-off is proved the same way here as in every scan job.
IMAGE_TRANSPORT="${CANDIDATE_TRANSPORT}"
IMAGE_DIGEST="${CANDIDATE_DIGEST}"
IMAGE_REF="${CANDIDATE_REF}"
export IMAGE_TRANSPORT IMAGE_DIGEST IMAGE_REF
verify_image_identity || exit 1

# The evidence root the scan jobs wrote into. IMAGE_NAME comes from
# build.env when the job sourced it, and from the candidate repository
# leaf otherwise — the push backends compose one from the other.
if [ -z "${IMAGE_NAME:-}" ]; then
  IMAGE_NAME="${CANDIDATE_REPO##*/}"
  IMAGE_NAME="${IMAGE_NAME%%:*}"
fi
export IMAGE_NAME
artifact_paths

echo "=== Promotion ==="
echo "  Candidate:       ${CANDIDATE_REF}"
echo "  Evidence:        ${SCAN_ARTIFACT_ROOT}/"

# ════════════════════════════════════════════════════════════════════
# 2. The gates
# ════════════════════════════════════════════════════════════════════
# Both mandatory gates write into this one root, and both bind their
# output to a digest. A gate whose evidence names a different image is
# not evidence about this candidate, so it blocks the release exactly
# like a failed one.

SUBJECT_JSON="${SCAN_ARTIFACT_ROOT}/subject.json"
RESULT_JSON="${SCAN_ARTIFACT_ROOT}/scan-result.json"

[ -s "${SUBJECT_JSON}" ] \
  || _fail "no SBOM subject record at ${SUBJECT_JSON}. The SBOM gate did not run, or its artifacts did not reach this job."
_subject_digest="$(jq -r '.digest // ""' "${SUBJECT_JSON}")"
[ "${_subject_digest}" = "${CANDIDATE_DIGEST}" ] \
  || _fail "${SUBJECT_JSON} records digest ${_subject_digest:-<none>}, the candidate is ${CANDIDATE_DIGEST}.
       The SBOM describes a different image."

[ -s "${SBOM_FILE}" ] \
  || _fail "no SBOM at ${SBOM_FILE}. Its subject record is present, the SBOM it describes is not."

[ -s "${RESULT_JSON}" ] \
  || _fail "no vulnerability gate result at ${RESULT_JSON}. Missing evidence is not a pass."
GATE_OUTCOME="$(jq -r '.outcome // ""' "${RESULT_JSON}")"
[ "${GATE_OUTCOME}" = "pass" ] \
  || _fail "the vulnerability gate outcome is '${GATE_OUTCOME:-<none>}'. Only a pass promotes."
_result_digest="$(jq -r '.subject.digest // ""' "${RESULT_JSON}")"
[ "${_result_digest}" = "${CANDIDATE_DIGEST}" ] \
  || _fail "${RESULT_JSON} scored digest ${_result_digest:-<none>}, the candidate is ${CANDIDATE_DIGEST}.
       The verdict belongs to a different image."

echo "  Gates:           sbom-subject=pass vuln-scan=${GATE_OUTCOME}"

# ════════════════════════════════════════════════════════════════════
# 3. Signature
# ════════════════════════════════════════════════════════════════════
# D6 is unresolved: no signer identity is provisioned, so cosign-enabled
# defaults to false and this check is dormant rather than mandatory.
# Turning it on makes a valid candidate signature a precondition of the
# copy. It is deliberately NOT a needs entry on the publish job — the
# attest job is dormant too, and adding it would make a green release
# graph depend on infrastructure that does not exist yet. Mandatory
# signing stays outstanding until a key and a signer identity exist.
SIGNATURE_MODE="disabled"
SIGNATURE_VERIFIED=false
case "$(printf '%s' "${COSIGN_ENABLED:-false}" | tr '[:upper:]' '[:lower:]')" in
  true|1|yes|on) SIGNATURE_MODE="required" ;;
esac
if [ "${SIGNATURE_MODE}" = "required" ]; then
  command -v cosign >/dev/null 2>&1 \
    || _fail "COSIGN_ENABLED is true but cosign is not on PATH, so the candidate signature cannot be checked."
  _cosign_key="${COSIGN_PUBLIC_KEY:-${COSIGN_KEY:-}}"
  [ -n "${_cosign_key}" ] \
    || _fail "COSIGN_ENABLED is true but neither COSIGN_PUBLIC_KEY nor COSIGN_KEY is set."
  echo "→ cosign verify ${CANDIDATE_REF}"
  # The attest job signs with --tlog-upload=false, so verification does
  # not look for a transparency-log entry either.
  cosign verify --key "${_cosign_key}" --insecure-ignore-tlog=true "${CANDIDATE_REF}" >/dev/null \
    || _fail "no valid signature on the candidate at the source registry. Refusing to promote an unsigned image while signing is required."
  SIGNATURE_VERIFIED=true
  echo "  ✓ signature verified at the source"
fi

# ════════════════════════════════════════════════════════════════════
# 4. Copy
# ════════════════════════════════════════════════════════════════════

# A publish job is retryable, and the resource group serialises the
# waiting jobs without ordering them. An older pipeline's retry can
# therefore run after a newer pipeline has already released, and move
# the release tag back onto an older digest. The branch head is the
# tiebreak: if this commit is no longer it, this pipeline has nothing
# current to release. Tag pipelines are immutable, so they skip it.
# git answers this, not the REST API: the job token is accepted on only
# a few API endpoints, but CI_REPOSITORY_URL already carries it for git.
if [ -n "${CI_COMMIT_BRANCH:-}" ] && [ -z "${CI_COMMIT_TAG:-}" ]; then
  if [ -z "${CI_REPOSITORY_URL:-}" ]; then
    echo "  NOTE: no CI_REPOSITORY_URL — not a CI run, so the branch-head check is skipped."
  else
    _branch_head="$(git ls-remote --heads "${CI_REPOSITORY_URL}" "refs/heads/${CI_COMMIT_BRANCH}" 2>/dev/null \
      | awk 'NR==1 {print $1}')"
    [ -n "${_branch_head}" ] \
      || _fail "could not read the head of ${CI_COMMIT_BRANCH} from the project repository.
       The check needs git on PATH and CI_REPOSITORY_URL readable by this job.
       Without it a stale pipeline could move the release tag back."
    [ "${_branch_head}" = "${CI_COMMIT_SHA:-}" ] \
      || _fail "${CI_COMMIT_BRANCH} is now at ${_branch_head}, this pipeline built ${CI_COMMIT_SHA:-<unset>}.
       A newer commit has been built since, so promoting this one would move
       the release tag backwards. Publish from the newest pipeline instead."
    echo "  Branch head:     ${_branch_head}"
  fi
fi

[ -n "${RELEASE_REGISTRY:-}" ]   || _fail "RELEASE_REGISTRY is empty. Set the release-registry input on the pipeline."
[ -n "${RELEASE_REPOSITORY:-}" ] || _fail "RELEASE_REPOSITORY is empty. Set the release-repository input on the pipeline."

RELEASE_TAG="${RELEASE_TAG:-${CANDIDATE_TAG}}"
[ -n "${RELEASE_TAG}" ] \
  || _fail "no release tag. image.json carries no tag and RELEASE_TAG is unset, so the release would be addressable by digest alone."

_dest_host="${RELEASE_REGISTRY#*://}"      # a URL form is accepted; crane takes a host
_dest_host="${_dest_host%/}"
DEST_REPOSITORY="${_dest_host}/${RELEASE_REPOSITORY#/}"
DEST_TAG_REF="${DEST_REPOSITORY}:${RELEASE_TAG}"
DEST_DIGEST_REF="${DEST_REPOSITORY}@${CANDIDATE_DIGEST}"

# Release credentials live in their own docker config, created here and
# removed on the way out. The build job's login state is in a different
# job on a different runner; nothing is shared either way, and a
# credential this job writes does not outlive it.
DOCKER_CONFIG="$(mktemp -d)"
export DOCKER_CONFIG
trap 'rm -rf "${DOCKER_CONFIG}"' EXIT

if [ -n "${RELEASE_REGISTRY_USER:-}" ] && [ -n "${RELEASE_REGISTRY_PASSWORD:-}" ]; then
  echo "→ crane auth login ${_dest_host%%/*} (release credential)"
  printf '%s' "${RELEASE_REGISTRY_PASSWORD}" \
    | crane auth login "${_dest_host%%/*}" -u "${RELEASE_REGISTRY_USER}" --password-stdin \
    || _fail "release registry login failed for ${_dest_host%%/*}."
else
  _fail "no release credential: CBT_RELEASE_REGISTRY_USER and CBT_RELEASE_REGISTRY_PASSWORD
       are empty in this job. They are protected CI variables, so an
       unprotected branch or tag cannot read them — protect the ref, or set
       them on the project. Refusing to copy to the release registry
       anonymously."
fi

# Limitation: the source is read with whatever auth the release credential
# gives, which is none for a private candidate registry on a different
# host. Add a read-only candidate credential to the publish job if a
# consumer's candidate registry is closed.
#
# Limitation: single-platform manifests only. crane copies whatever the
# reference names, but the gates scored one manifest, so a multi-arch
# index would ship platforms nothing scanned. Gate per platform before
# promoting an index.
if [ "${CANDIDATE_KIND}" = "index" ]; then
  echo "  WARN: the candidate is a multi-platform index; the gates scored one manifest." >&2
fi

echo "→ crane copy ${CANDIDATE_REF} → ${DEST_TAG_REF}"
crane copy "${CANDIDATE_REF}" "${DEST_TAG_REF}" \
  || _fail "crane copy to ${DEST_TAG_REF} failed."
echo "→ crane copy ${CANDIDATE_REF} → ${DEST_DIGEST_REF}"
crane copy "${CANDIDATE_REF}" "${DEST_DIGEST_REF}" \
  || _fail "crane copy to ${DEST_DIGEST_REF} failed."

# ════════════════════════════════════════════════════════════════════
# 5. Read the destination back
# ════════════════════════════════════════════════════════════════════
# The copy reporting success is the tool's claim. The destination's own
# answer is the evidence, and it has to be the digest the gates passed.

DEST_DIGEST="$(crane digest "${DEST_TAG_REF}" 2>/dev/null || echo "")"
[ "${DEST_DIGEST}" = "${CANDIDATE_DIGEST}" ] \
  || _fail "${DEST_TAG_REF} resolves to ${DEST_DIGEST:-<nothing>}, the promoted candidate is ${CANDIDATE_DIGEST}.
       The release destination does not hold the image the gates passed."
echo "  ✓ ${DEST_TAG_REF} is ${DEST_DIGEST}"

# ════════════════════════════════════════════════════════════════════
# 6. The release record
# ════════════════════════════════════════════════════════════════════

_evidence_json() {
  local path
  for path in "$@"; do
    [ -s "${path}" ] || continue
    jq -n --arg p "${path}" --arg s "$(_ii_file_sha256 "${path}")" '{path: $p, sha256: $s}'
  done | jq -s '.'
}

RELEASE_JSON="${SCAN_ARTIFACT_ROOT}/release.json"
jq -n \
  --arg src_ref   "${CANDIDATE_REF}" \
  --arg src_repo  "${CANDIDATE_REPO}" \
  --arg src_tag   "${CANDIDATE_TAG}" \
  --arg digest    "${CANDIDATE_DIGEST}" \
  --arg dst_ref   "${DEST_TAG_REF}" \
  --arg dst_repo  "${DEST_REPOSITORY}" \
  --arg dst_tag   "${RELEASE_TAG}" \
  --arg dst_dig   "${DEST_DIGEST}" \
  --arg gate      "${GATE_OUTCOME}" \
  --arg sig_mode  "${SIGNATURE_MODE}" \
  --argjson sig_ok "${SIGNATURE_VERIFIED}" \
  --arg pipeline  "${CI_PIPELINE_ID:-}" \
  --arg job       "${CI_JOB_ID:-}" \
  --arg commit    "${CI_COMMIT_SHA:-${GIT_SHA:-}}" \
  --argjson evidence "$(_evidence_json "${SUBJECT_JSON}" "${SBOM_FILE}" "${RESULT_JSON}" "${VULN_SCAN_FILE}")" \
  --arg promoted  "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
  '{schema_version: 1,
    source:      {reference: $src_ref, repository: $src_repo, tag: $src_tag, digest: $digest},
    destination: {reference: $dst_ref, repository: $dst_repo, tag: $dst_tag, digest: $dst_dig},
    gates:       [{name: "sbom-subject", outcome: "pass"},
                  {name: "vuln-scan",    outcome: $gate}],
    signature:   {mode: $sig_mode, verified: $sig_ok},
    evidence:    $evidence,
    pipeline_id: $pipeline,
    job_id:      $job,
    source_commit: $commit,
    promoted_at: $promoted}' > "${RELEASE_JSON}" \
  || _fail "could not write ${RELEASE_JSON}"

echo "  → ${RELEASE_JSON}"
echo "Promoted: ${DEST_TAG_REF} (${CANDIDATE_DIGEST})"
