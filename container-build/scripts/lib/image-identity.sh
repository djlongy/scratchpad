#!/usr/bin/env bash
# ─── DO NOT EDIT — template lib ────────────────────────────────────
# The image identity contract, with one writer and one checker.
#
#   write_image_identity    the ONLY writer of image.json + build.env.
#                           build.sh's no-push path and all three push
#                           backends call it, so every path emits the
#                           same fields with the same meanings.
#   verify_image_identity   the consuming side (scan-common.sh). Missing
#                           evidence, an unreadable archive or a digest
#                           that does not describe the file on disk fails
#                           the job. It never steps back to the upstream
#                           image, which is a different image.
#
# image.json is authoritative; build.env is the dotenv convenience GitLab
# imports, and must agree with it. Only a MANIFEST digest is ever written
# into a digest field — a local config/image ID is not one, and naming it
# `digest` would let a downstream job believe it scanned a published
# artifact.
# ───────────────────────────────────────────────────────────────────
# shellcheck disable=SC2148

# Canonical filenames. The archive and the metadata file are build
# outputs; image.json is the identity record that travels with them.
IMAGE_JSON="${IMAGE_JSON:-image.json}"
IMAGE_ARCHIVE="${IMAGE_ARCHIVE:-image.oci.tar}"
BUILD_METADATA_FILE="${BUILD_METADATA_FILE:-image.build-metadata.json}"
export IMAGE_JSON IMAGE_ARCHIVE BUILD_METADATA_FILE

_ii_require_jq() {
  command -v jq >/dev/null 2>&1 && return 0
  echo "ERROR: jq is required to read and write the image identity record." >&2
  echo "       Install it (alpine: apk add jq). CI's .install-tools anchor already does." >&2
  return 1
}

_ii_valid_digest() {
  printf '%s' "${1:-}" | grep -qE '^sha256:[0-9a-f]{64}$'
}

# Artifact paths are recorded relative to the project root so they stay
# portable between a laptop and a runner; resolve them here to read them.
_ii_abs() {
  case "${1:-}" in
    /*) printf '%s' "$1" ;;
    *)  printf '%s/%s' "${PROJECT_ROOT:-$(pwd)}" "${1:-}" ;;
  esac
}

# sha256 of a file. Alpine/GNU ship sha256sum, macOS ships shasum.
_ii_file_sha256() {
  local f
  f="$(_ii_abs "$1")"
  if command -v sha256sum >/dev/null 2>&1; then
    sha256sum "${f}" | cut -d' ' -f1
  elif command -v shasum >/dev/null 2>&1; then
    shasum -a 256 "${f}" | cut -d' ' -f1
  else
    openssl dgst -sha256 "${f}" | awk '{print $NF}'
  fi
}

# ── OCI layout archive readers ──────────────────────────────────────
# An OCI image layout tarball carries index.json at its root. The digest
# named there is the manifest digest of what the exporter wrote, so it is
# the archive's own statement of identity rather than a daemon-local id.

image_archive_index() {
  tar -xOf "$(_ii_abs "$1")" index.json 2>/dev/null
}

image_archive_digest() {
  image_archive_index "$1" | jq -r '.manifests[0].digest // ""'
}

# manifest = one platform. index = a multi-platform (or attested) index,
# whose per-platform manifests a single scan does not cover.
image_archive_subject_kind() {
  case "$(image_archive_index "$1" | jq -r '.manifests[0].mediaType // ""')" in
    *image.index.v1+json|*manifest.list.v2+json) printf 'index' ;;
    *)                                           printf 'manifest' ;;
  esac
}

image_archive_platforms() {
  local platforms
  platforms=$(image_archive_index "$1" \
    | jq -r '[.manifests[] | select(.platform) | "\(.platform.os)/\(.platform.architecture)"] | unique | join(" ")')
  if [ -n "${platforms}" ]; then
    printf '%s' "${platforms}"
  else
    image_host_platform
  fi
}

# What a build with no --platform flag targets. Used for the single-arch
# push path, and where a single-platform archive's index.json omits it.
# Limitation: a --platform build would have to thread the flag's value
# through here instead of asking uname.
image_host_platform() {
  case "$(uname -m)" in
    x86_64|amd64)  printf 'linux/amd64' ;;
    aarch64|arm64) printf 'linux/arm64' ;;
  esac
}

# ── Writer ──────────────────────────────────────────────────────────
# Callers set these before calling, and nothing else:
#   IMAGE_TRANSPORT   registry | oci-archive
#   IMAGE_DIGEST      sha256:<64 hex> — the manifest digest produced
#   IMAGE_REF         registry     → <IMAGE_REPOSITORY>@<IMAGE_DIGEST>
#                     oci-archive  → oci-archive:<IMAGE_ARCHIVE>
#   IMAGE_REPOSITORY  registry     → full repository, no tag and no digest
#                     oci-archive  → the local image name; nothing is
#                                    pullable, so no registry host is invented
#   IMAGE_TAG_REF     registry tag reference; empty for an archive
#   IMAGE_TAG, IMAGE_NAME, IMAGE_SUBJECT_KIND, IMAGE_PLATFORMS
#   IMAGE_ARCHIVE_SHA256                      (oci-archive only)
# plus the build context build.sh exports: GIT_SHA, CREATED, UPSTREAM_REF,
# UPSTREAM_TAG, UPSTREAM_DIGEST, BASE_DIGEST, SBOM_FILE, VULN_SCAN_FILE.
write_image_identity() {
  _ii_require_jq || return 1

  if ! _ii_valid_digest "${IMAGE_DIGEST:-}"; then
    echo "ERROR: IMAGE_DIGEST='${IMAGE_DIGEST:-}' is not a manifest digest (sha256:<64 hex>)." >&2
    echo "       Refusing to hand a config/image ID to downstream jobs as one." >&2
    return 1
  fi

  local archive="" archive_sha=""
  case "${IMAGE_TRANSPORT:-}" in
    registry)
      case "${IMAGE_REF:-}" in
        *"@${IMAGE_DIGEST}") : ;;
        *)
          echo "ERROR: IMAGE_REF='${IMAGE_REF:-}' does not carry IMAGE_DIGEST='${IMAGE_DIGEST}'." >&2
          echo "       A registry hand-off is the digest reference of what was pushed." >&2
          return 1
          ;;
      esac
      ;;
    oci-archive)
      archive="${IMAGE_ARCHIVE}"
      archive_sha="${IMAGE_ARCHIVE_SHA256:-}"
      if [ ! -s "$(_ii_abs "${archive}")" ]; then
        echo "ERROR: IMAGE_TRANSPORT=oci-archive but no archive at $(_ii_abs "${archive}")" >&2
        return 1
      fi
      ;;
    *)
      echo "ERROR: IMAGE_TRANSPORT must be 'registry' or 'oci-archive' (got '${IMAGE_TRANSPORT:-<empty>}')" >&2
      return 1
      ;;
  esac

  if [ -n "${UPSTREAM_REF:-}" ] && [ "${IMAGE_REF}" = "${UPSTREAM_REF}" ]; then
    echo "ERROR: IMAGE_REF names the upstream image, not the one this build produced." >&2
    return 1
  fi

  # platforms is a JSON array, so the space-separated dotenv form is split
  # back into words here. bash 3.2 needs the +expansion guard to reference
  # an empty array under `set -u`.
  local platforms=()
  if [ -n "${IMAGE_PLATFORMS:-}" ]; then
    read -r -a platforms <<< "${IMAGE_PLATFORMS}"
  fi

  # Schema per the GitLab CI standard §9.1. Pipeline and job ids are
  # strings, and are written only when the producer actually had them.
  jq -n \
    --arg repository   "${IMAGE_REPOSITORY:-}" \
    --arg digest       "${IMAGE_DIGEST}" \
    --arg reference    "${IMAGE_REF}" \
    --arg tag          "${IMAGE_TAG:-}" \
    --arg tag_ref      "${IMAGE_TAG_REF:-}" \
    --arg transport    "${IMAGE_TRANSPORT}" \
    --arg archive      "${archive}" \
    --arg archive_sha  "${archive_sha}" \
    --arg commit       "${GIT_SHA:-}" \
    --arg pipeline     "${CI_PIPELINE_ID:-}" \
    --arg job          "${CI_JOB_ID:-}" \
    --arg subject_kind "${IMAGE_SUBJECT_KIND:-manifest}" \
    --arg created_at   "${CREATED:-}" \
    --arg upstream     "${UPSTREAM_REF:-}" \
    --arg upstream_dig "${UPSTREAM_DIGEST:-${BASE_DIGEST:-}}" \
    --args '
      {
        schema_version: 1,
        repository: $repository,
        digest: $digest,
        reference: $reference,
        tag: $tag,
        tag_reference: $tag_ref,
        transport: $transport,
        subject_kind: $subject_kind,
        platforms: $ARGS.positional,
        source_commit: $commit,
        created_at: $created_at,
        upstream_ref: $upstream,
        upstream_digest: $upstream_dig
      }
      + (if $archive  != "" then {archive: $archive, archive_sha256: $archive_sha} else {} end)
      + (if $pipeline != "" then {pipeline_id: $pipeline} else {} end)
      + (if $job      != "" then {job_id: $job} else {} end)
    ' ${platforms[@]+"${platforms[@]}"} > "${IMAGE_JSON}" || {
      echo "ERROR: could not write ${IMAGE_JSON}" >&2
      return 1
    }

  # Plain KEY=VALUE — NO `export ` prefix. GitLab's dotenv parser
  # (artifacts.reports.dotenv) rejects lines starting with `export `
  # because it reads "export KEY" as the key and fails on the space.
  # Consumers wrap `. ./build.env` in `set -a` for subshell propagation.
  cat > build.env <<EOF
IMAGE_REF=${IMAGE_REF}
IMAGE_TAG_REF=${IMAGE_TAG_REF:-}
IMAGE_TAG=${IMAGE_TAG:-}
IMAGE_DIGEST=${IMAGE_DIGEST}
IMAGE_NAME=${IMAGE_NAME:-}
IMAGE_REPOSITORY=${IMAGE_REPOSITORY:-}
IMAGE_TRANSPORT=${IMAGE_TRANSPORT}
IMAGE_ARCHIVE=${archive}
IMAGE_ARCHIVE_SHA256=${archive_sha}
IMAGE_SUBJECT_KIND=${IMAGE_SUBJECT_KIND:-manifest}
IMAGE_PLATFORMS=${IMAGE_PLATFORMS:-}
IMAGE_JSON=${IMAGE_JSON}
UPSTREAM_TAG=${UPSTREAM_TAG:-unknown}
UPSTREAM_REF=${UPSTREAM_REF:-unknown}
BASE_DIGEST=${BASE_DIGEST:-}
GIT_SHA=${GIT_SHA:-unknown}
CREATED=${CREATED:-}
SBOM_FILE=${SBOM_FILE}
VULN_SCAN_FILE=${VULN_SCAN_FILE}
EOF

  echo "→ wrote ${IMAGE_JSON} + build.env (transport=${IMAGE_TRANSPORT}, digest=${IMAGE_DIGEST})"
}

# ── Checker ─────────────────────────────────────────────────────────
# Everything a consumer needs before it trusts build.env: a real digest,
# agreement with image.json, and for an archive, a file that is present,
# intact and actually holding that digest.
verify_image_identity() {
  if ! _ii_valid_digest "${IMAGE_DIGEST:-}"; then
    echo "ERROR: build.env carries no usable IMAGE_DIGEST (got '${IMAGE_DIGEST:-<empty>}')." >&2
    echo "       The build did not hand over what it produced — failing rather than" >&2
    echo "       scanning the upstream image, which is a different image." >&2
    return 1
  fi

  # image.json is authoritative. A build.env that disagrees with it is a
  # leftover from an earlier run.
  local json
  json="$(_ii_abs "${IMAGE_JSON:-image.json}")"
  if [ -s "${json}" ] && command -v jq >/dev/null 2>&1; then
    local json_digest
    json_digest=$(jq -r '.digest // ""' "${json}" 2>/dev/null)
    if [ "${json_digest}" != "${IMAGE_DIGEST}" ]; then
      echo "ERROR: build.env IMAGE_DIGEST=${IMAGE_DIGEST} disagrees with ${json} (digest=${json_digest:-<none>})." >&2
      return 1
    fi
  fi

  case "${IMAGE_TRANSPORT:-}" in
    registry)
      case "${IMAGE_REF:-}" in
        *"@${IMAGE_DIGEST}") return 0 ;;
      esac
      echo "ERROR: IMAGE_TRANSPORT=registry but IMAGE_REF='${IMAGE_REF:-}' is not the digest reference" >&2
      echo "       of IMAGE_DIGEST=${IMAGE_DIGEST}." >&2
      return 1
      ;;
    oci-archive)
      local file
      file="$(_ii_abs "${IMAGE_ARCHIVE:-}")"
      if [ ! -s "${file}" ]; then
        echo "ERROR: IMAGE_TRANSPORT=oci-archive but the archive is missing or empty: ${file}" >&2
        echo "       The build job's artifacts must carry it to this job." >&2
        return 1
      fi
      if [ -n "${IMAGE_ARCHIVE_SHA256:-}" ]; then
        local have
        have="$(_ii_file_sha256 "${IMAGE_ARCHIVE}")"
        if [ "${have}" != "${IMAGE_ARCHIVE_SHA256}" ]; then
          echo "ERROR: ${file} sha256 is ${have}, build.env recorded ${IMAGE_ARCHIVE_SHA256}." >&2
          return 1
        fi
      fi
      _ii_require_jq || return 1
      local in_archive
      in_archive="$(image_archive_digest "${IMAGE_ARCHIVE}")"
      if [ "${in_archive}" != "${IMAGE_DIGEST}" ]; then
        echo "ERROR: ${file} index.json names ${in_archive:-<none>}, build.env says ${IMAGE_DIGEST}." >&2
        echo "       The record does not describe the archive it points at." >&2
        return 1
      fi
      return 0
      ;;
  esac

  echo "ERROR: IMAGE_TRANSPORT='${IMAGE_TRANSPORT:-<empty>}' is not a transport this template writes." >&2
  return 1
}
