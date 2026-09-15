#!/usr/bin/env bash
# ─── DO NOT EDIT — template push backend ───────────────────────────
# Selected via REGISTRY_KIND="harbor" in image.env. Config knobs
# (HARBOR_REGISTRY / HARBOR_PROJECT / HARBOR_USER / HARBOR_PASSWORD)
# come from image.env + masked CI vars. Edit those, not this file.
# ───────────────────────────────────────────────────────────────────
#
# push-backend: Harbor (and any plain Docker Registry v2 endpoint).
#
# Sourced by scripts/build.sh when REGISTRY_KIND is unset OR set to
# "harbor". Exposes a single entry point — push_to_backend() — that
# handles env validation, retag from the simple local build tag,
# docker login, docker push, digest extraction, and build.env emission.
#
# Mirrors the contract of the artifactory backends (artifactory_jcr.sh
# / artifactory_pro.sh) so a
# fork can swap backends by changing REGISTRY_KIND alone, without
# touching build.sh.
#
# ── HARBOR vars are fully independent of ARTIFACTORY_* ──────────────
# This backend reads ONLY HARBOR_* env. It never falls back to or
# auto-derives anything from ARTIFACTORY_*. Same independence holds
# the other way — artifactory.sh ignores HARBOR_*. Pick the namespace
# that matches your REGISTRY_KIND; the other one is irrelevant.
#
# ── Variables this backend reads (set in image.env / shell env) ─────
#
# Required (when --push):
#   HARBOR_REGISTRY    destination registry host (e.g. harbor.example.com)
#   HARBOR_PROJECT     project / path prefix under HARBOR_REGISTRY
#                      (composes the push URL as
#                       <HARBOR_REGISTRY>/<HARBOR_PROJECT>/<IMAGE_NAME>:<FULL_TAG>)
#
# Required for authenticated registries:
#   HARBOR_USER
#   HARBOR_PASSWORD    password or token (CI-masked, never committed)
#
# Inputs from build.sh (already exported):
#   FULL_IMAGE         simple local docker tag, e.g. nginx:1.25.3-alpine-abc
#                      → this backend retags it to the Harbor target URL
#                        before pushing
#   FULL_TAG           computed tag (UPSTREAM_TAG[-gitShort])
#   IMAGE_NAME, UPSTREAM_TAG, UPSTREAM_REF, BASE_DIGEST, GIT_SHA, CREATED
#
# ── Outputs ─────────────────────────────────────────────────────────
#
# This backend supplies its registry values and calls the shared writer
# in scripts/lib/image-identity.sh, which emits image.json + build.env.
# The Artifactory backends do the same, so downstream stages read one
# schema and never have to know which backend ran.

set -uo pipefail

# ════════════════════════════════════════════════════════════════════
# Internals
# ════════════════════════════════════════════════════════════════════

_harbor_require_env() {
  local missing=0 var
  for var in HARBOR_REGISTRY HARBOR_PROJECT; do
    if [ -z "${!var:-}" ]; then
      echo "ERROR: ${var} is required for the Harbor backend (--push)" >&2
      missing=1
    fi
  done
  return "${missing}"
}
# Public alias — build.sh's _build_validate_backend looks up
# `${kind}_require_env` so each backend exports a predictable name.
harbor_require_env() { _harbor_require_env "$@"; }

_harbor_docker_login() {
  if ! command -v docker >/dev/null 2>&1; then
    echo "ERROR: 'docker' CLI not found on PATH" >&2
    return 1
  fi
  if [ -z "${HARBOR_USER:-}" ] || [ -z "${HARBOR_PASSWORD:-}" ]; then
    _dbg "Harbor creds incomplete (registry=${HARBOR_REGISTRY} user=${HARBOR_USER:+set}) — skipping login"
    echo "  WARN: HARBOR_USER / HARBOR_PASSWORD unset — relying on existing daemon login" >&2
    return 0
  fi
  echo "→ docker login ${HARBOR_REGISTRY} (Harbor backend)"
  printf '%s' "${HARBOR_PASSWORD}" | docker login "${HARBOR_REGISTRY}" \
    -u "${HARBOR_USER}" --password-stdin
}

# Compose the Harbor push URL from HARBOR_* vars + the build-time
# IMAGE_NAME/FULL_TAG. Decoupled from build.sh so the backend owns
# its target shape.
_harbor_compose_target() {
  printf '%s/%s/%s:%s' \
    "${HARBOR_REGISTRY}" "${HARBOR_PROJECT}" "${IMAGE_NAME}" "${FULL_TAG}"
}

_harbor_print_banner() {
  local source_ref="$1" target="$2"
  echo ""
  echo "=== Harbor push ==="
  echo "  Source (local):  ${source_ref}"
  echo "  Target:          ${target}"
  echo "  Push host:       ${HARBOR_REGISTRY}"
  echo "  Project path:    ${HARBOR_PROJECT}"
}

# The registry's own answer for what the push created. The "digest:
# sha256:…" line docker push prints is that answer; crane and the local
# RepoDigests entry are two ways of asking again if the output was
# swallowed. All three yield a MANIFEST digest — never an image id.
_harbor_resolve_push_digest() {
  local target="$1" push_output="${2:-}" digest=""
  digest=$(printf '%s' "${push_output}" | awk '/digest: sha256:/{print $3}' | head -1)
  if [ -z "${digest}" ] && command -v crane >/dev/null 2>&1; then
    digest=$(crane digest "${target}" 2>/dev/null || echo "")
  fi
  if [ -z "${digest}" ]; then
    digest=$(docker inspect --format='{{index .RepoDigests 0}}' "${target}" 2>/dev/null \
             | grep -oE 'sha256:[0-9a-f]{64}' || echo "")
  fi
  printf '%s' "${digest}"
}

# Supply the registry-side values and let the shared writer own the
# schema, so this backend and the two Artifactory ones hand downstream
# jobs the same fields. An unresolved digest fails there, loudly.
_harbor_write_identity() {
  local target="$1" push_digest="$2"
  IMAGE_TRANSPORT="registry"
  IMAGE_REPOSITORY="${target%:*}"
  IMAGE_DIGEST="${push_digest}"
  IMAGE_REF="${IMAGE_REPOSITORY}@${push_digest}"
  IMAGE_TAG_REF="${target}"
  IMAGE_TAG="${FULL_TAG}"
  # --load takes a flat single-arch manifest, so the pushed subject is
  # one manifest for the build host's platform.
  IMAGE_SUBJECT_KIND="manifest"
  IMAGE_PLATFORMS="$(image_host_platform)"
  # Exported because write_image_identity reads them, and because the
  # SBOM stage build.sh may hand off to reads IMAGE_REF / IMAGE_DIGEST.
  export IMAGE_TRANSPORT IMAGE_REPOSITORY IMAGE_DIGEST IMAGE_REF IMAGE_TAG_REF
  export IMAGE_TAG IMAGE_SUBJECT_KIND IMAGE_PLATFORMS
  write_image_identity
}

# ════════════════════════════════════════════════════════════════════
# Entry point
# ════════════════════════════════════════════════════════════════════

push_to_backend() {
  local source_ref="$1"   # simple local tag from build.sh, e.g. nginx:1.25.3-alpine-abc

  _harbor_require_env  || return 1
  _harbor_docker_login || return 1

  local target
  target="$(_harbor_compose_target)"
  _harbor_print_banner "${source_ref}" "${target}"

  # Retag the local image to the Harbor target URL before push.
  docker tag "${source_ref}" "${target}" || {
    echo "ERROR: docker tag ${source_ref} → ${target} failed" >&2
    return 1
  }

  echo ""
  echo "→ docker push ${target}"
  local push_output push_digest
  push_output=$(docker push "${target}" 2>&1) || {
    echo "${push_output}" >&2
    echo "ERROR: docker push failed" >&2
    return 1
  }
  echo "${push_output}"

  push_digest=$(_harbor_resolve_push_digest "${target}" "${push_output}")
  echo "→ pushed: ${target%:*}@${push_digest:-<unresolved>}"

  _harbor_write_identity "${target}" "${push_digest}" || return 1
  echo "Pushed: ${target}"
}
