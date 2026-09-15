#!/usr/bin/env bash
# ─── DO NOT EDIT — template orchestrator ──────────────────────────
# Behaviour is driven by image.env. To change anything for your fork
# (registry, tag, backend, severity gates, …), edit image.env — not
# this file. Edit here only when changing the template's own logic.
# ──────────────────────────────────────────────────────────────────
#
# Single-image build + push driver.
#
# - Computes pushed tag as <UPSTREAM_VERSION>-<gitShort>.
# - Pins the upstream base (and the cert builder) to an immutable
#   repo:tag@sha256 reference before anything reads it.
# - `docker build` with the full OCI label set.
# - Optionally pushes via the selected REGISTRY_KIND backend.
# - ALWAYS emits image.json + build.env naming the image THIS build
#   produced, regardless of --push. No-push exports an OCI layout
#   archive (image.oci.tar) and hands over its manifest digest; --push
#   hands over the registry digest reference. Neither ever names the
#   upstream image. See scripts/lib/image-identity.sh.
#
# Usage:
#   ./scripts/build.sh            # build only, load into local daemon
#   ./scripts/build.sh --push     # build + push via REGISTRY_KIND backend
#   ./scripts/build.sh --dry-run  # resolve config + digest, no build
#   ./scripts/build.sh --help     # full flag list
#
# Required env when --push:
#   REGISTRY_KIND       "harbor" (default) | "artifactory_jcr" | "artifactory_pro"
#   then per-backend (validated pre-build by _build_validate_backend):
#     harbor            → HARBOR_REGISTRY, HARBOR_PROJECT, HARBOR_USER, HARBOR_PASSWORD
#     artifactory_jcr   → ARTIFACTORY_URL, ARTIFACTORY_USER,
#                          ARTIFACTORY_TOKEN | ARTIFACTORY_PASSWORD,
#                          ARTIFACTORY_TEAM
#     artifactory_pro   → same as jcr, plus optional ARTIFACTORY_PROJECT
#
# Optional env: see image.env.reference. Highlights:
#   IMAGE_NAME          default: leaf of UPSTREAM_IMAGE
#   RESTORE_USER        true when the Dockerfile's fork region enters
#                       root and restores the user; default false
#   ORIGINAL_USER       the user to restore (RESTORE_USER=true only);
#                       unset = read from `crane config`
#   CA_CERT             PEM → certs/ci-injected.crt (cert sidecar)
#   CRANE_URL           override for air-gap
#   APPEND_GIT_SHORT    default true; false to skip the SHA suffix
#
# Structure: small named phases, orchestrator at the bottom. Phases
# never silently skip downstream work — they return non-zero and the
# orchestrator handles the rollup.

set -euo pipefail

# ── Two roots: TEMPLATE_ROOT (scripts, read-only, from BASH_SOURCE) vs
# PROJECT_ROOT (image.env/Dockerfile/certs + where build.env/SBOM/scan
# artifacts land; defaults to CWD, override with --project-root or the
# PROJECT_ROOT env var). They coincide when the template builds itself.
# See docs/DESIGN.md "Two roots".
TEMPLATE_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# PROJECT_ROOT defaults to CWD; _build_parse_args may override via flag.
# Re-export so child scripts (scan/scan-*.sh, ingest/sbom-post.sh)
# inherit the same project context.
export PROJECT_ROOT="${PROJECT_ROOT:-$(pwd)}"
export TEMPLATE_ROOT

# ── Shared lib: provides _dbg, import_bamboo_vars (bamboo_* → bare),
# and load_image_env (source image.env, shell/CI overrides win). Every
# script sources it, so config loading is one code path everywhere.
# shellcheck source=lib/load-image-env.sh
. "${TEMPLATE_ROOT}/scripts/lib/load-image-env.sh"
# shellcheck source=lib/artifact-names.sh
. "${TEMPLATE_ROOT}/scripts/lib/artifact-names.sh"
# shellcheck source=lib/image-identity.sh
. "${TEMPLATE_ROOT}/scripts/lib/image-identity.sh"

# ════════════════════════════════════════════════════════════════════
# PHASE 0 — Argument parsing
# ════════════════════════════════════════════════════════════════════
# Runs first, before any work. Sets WANT_PUSH and WANT_DRY_RUN for
# later phases. Unknown flags fail loud with a usage hint instead of
# being silently ignored (which let e.g. `--list` trigger a full build
# when the user was just probing for options).

_build_print_usage() {
  cat <<EOF
Usage: ./scripts/build.sh [flags]

  --push                   Build, then push to HARBOR_REGISTRY/HARBOR_PROJECT
                           (or via the Artifactory backend when
                           REGISTRY_KIND=artifactory_jcr or artifactory_pro).
  --dry-run                Resolve config + base digest, print the report
                           block, stop before docker build. No image
                           produced. Useful for "what would this build
                           with my current env?"
  --project-root <path>    Per-image repo root containing image.env,
                           Dockerfile, certs/. Default: \$PWD. Use this
                           when invoking from outside the per-image repo,
                           e.g. \`bash .template/scripts/build.sh
                           --project-root /builds/my-image\`.
  --env-file <path>        Path to image.env. Default:
                           \$PROJECT_ROOT/image.env. Use to point at an
                           alternate config (e.g. dev/prod variants).
  --dockerfile <path>      Path to the Dockerfile. Default: ./Dockerfile
                           when it exists, else ./Dockerfile.example (the
                           template repo's own self-test fixture). Set it
                           to build a differently named file.
  --help, -h               This message.

  Flags can appear in any order. Without --push or --dry-run, build
  runs locally and loads into the docker daemon without pushing.

Per-fork customisation: edit the Dockerfile directly, in the
"FORK EDITS GO HERE" region between the cert-injection stage and
the final USER flip. Use that region for RUN \`apk upgrade\`/\`apt-get
upgrade\` (CVE remediation), package installs, COPY of static configs,
ENV/HEALTHCHECK lines, etc.

All behavioural toggles are env-driven. See image.env.reference for the
full list. Commonly-used flags:

  REGISTRY_KIND=artifactory_jcr   use scripts/push-backends/artifactory_jcr.sh
                                  (Free tier — needs python3 for build-info merger)
  REGISTRY_KIND=artifactory_pro   use scripts/push-backends/artifactory_pro.sh
                                  (Pro tier — slimmer, no python3 dep)
                                  Default: REGISTRY_KIND=harbor → push-backends/harbor.sh
  CA_CERT='<pem>'             inject a corp CA — sidecar materialises it
                              into certs/ and the Dockerfile picks it up
  ARTIFACTORY_BUILD_XRAY_PRESCAN=true
                              jf docker scan inside the build job
                              BEFORE push (admin gate; default false)
  ARTIFACTORY_BUILD_XRAY_POSTSCAN=true
                              jf build-scan inside the build job
                              AFTER push (default false)
  ARTIFACTORY_XRAY_FAIL_ON_VIOLATIONS=true
                              fail build on Xray policy violation

Note: BUILD_ in the var name marks these as INLINE scans that run
INSIDE the push backend (build job). The standalone scan scripts
(scripts/scan/xray-{vuln,sbom}.sh) are separate CI stages — same
Xray engine, different pipeline placement. Both default OFF — opt
in only when an Xray licence is provisioned.
EOF
}

_build_parse_args() {
  WANT_PUSH=0
  WANT_DRY_RUN=0
  IMAGE_ENV_FILE=""
  # Empty = "not chosen yet"; resolved after cd into PROJECT_ROOT below,
  # because the fallback depends on which files are actually there.
  DOCKERFILE=""

  # Loop until args exhausted. Each --flag may take a value (then we
  # shift twice). Unknown flag = fail loud with usage so e.g. `--lst`
  # doesn't silently get treated as a positional.
  while [ $# -gt 0 ]; do
    case "$1" in
      --push)         WANT_PUSH=1; shift ;;
      --dry-run)      WANT_DRY_RUN=1; shift ;;
      --project-root)
        if [ $# -lt 2 ] || [ -z "$2" ]; then
          echo "ERROR: --project-root requires a path argument" >&2
          _build_print_usage >&2
          return 1
        fi
        # Resolve to absolute path so subsequent cd / sourcing is stable.
        PROJECT_ROOT="$(cd "$2" 2>/dev/null && pwd)" || {
          echo "ERROR: --project-root path does not exist: $2" >&2
          return 1
        }
        export PROJECT_ROOT
        shift 2
        ;;
      --env-file)
        if [ $# -lt 2 ] || [ -z "$2" ]; then
          echo "ERROR: --env-file requires a path argument" >&2
          _build_print_usage >&2
          return 1
        fi
        IMAGE_ENV_FILE="$2"
        shift 2
        ;;
      --dockerfile)
        if [ $# -lt 2 ] || [ -z "$2" ]; then
          echo "ERROR: --dockerfile requires a path argument" >&2
          _build_print_usage >&2
          return 1
        fi
        DOCKERFILE="$2"
        shift 2
        ;;
      --help|-h)      _build_print_usage; exit 0 ;;
      *)
        echo "ERROR: unknown flag '$1'" >&2
        echo "" >&2
        _build_print_usage >&2
        return 1
        ;;
    esac
  done

  # cd into project root so every subsequent phase reads image.env,
  # writes build.env, sees certs/, etc. relative to the per-image repo
  # rather than the template clone.
  cd "${PROJECT_ROOT}"

  # A consumer repo owns ./Dockerfile. This template ships none, so its
  # own self-test falls back to Dockerfile.example. An explicit
  # --dockerfile always wins. Missing-file errors stay with docker.
  if [ -z "${DOCKERFILE}" ]; then
    if [ -f Dockerfile ] || [ ! -f Dockerfile.example ]; then
      DOCKERFILE="Dockerfile"
    else
      DOCKERFILE="Dockerfile.example"
    fi
  fi

  # Tell load_image_env exactly which file to source. Default keeps
  # backwards compat with callers that just `cd <project>` first.
  if [ -n "${IMAGE_ENV_FILE}" ]; then
    export IMAGE_ENV_FILE
  fi

  echo "→ context: TEMPLATE_ROOT=${TEMPLATE_ROOT}"
  echo "           PROJECT_ROOT=${PROJECT_ROOT}"
  echo "           DOCKERFILE=${DOCKERFILE}"
  if [ -n "${IMAGE_ENV_FILE:-}" ]; then
    echo "           IMAGE_ENV_FILE=${IMAGE_ENV_FILE}"
  fi
}

# ════════════════════════════════════════════════════════════════════
# PHASE 1 — Config loading (delegated to scripts/lib/load-image-env.sh)
# ════════════════════════════════════════════════════════════════════
# image.env is the SINGLE source of truth. It MUST exist or the build
# fails — image.env.example is a TEMPLATE you copy from on first
# checkout, never sourced as real config.
#
# Two-layer precedence:
#   1. image.env          — committed canonical config (REQUIRED)
#   2. Shell / CI env     — always wins, for pipeline-level overrides
#
# Bamboo bonus: any env var named `bamboo_FOO` is auto-imported as
# `FOO` before the snapshot via import_bamboo_vars (also in the lib).
#
# Implementation lives in scripts/lib/load-image-env.sh and is shared
# by all scripts that read image.env (xray-vuln.sh, xray-sbom.sh,
# sbom-post.sh, etc.). See that file for the snapshot/restore details.

# Decompose a full image reference (UPSTREAM_REF) into its parts:
#   _REF_HOST    registry host    docker.io | ghcr.io | registry:5000 | art.example.com
#   _REF_PATH    repo path        library/nginx | prom/prometheus | mirror/library/nginx
#   _REF_TAG     tag              1.25.3-alpine   ("" if the ref carried none)
#   _REF_DIGEST  sha256:...       the pin, KEPT as-is ("" if the ref
#                                 carried none — PHASE 6 resolves one).
#
# Follows Docker's reference grammar so air-gapped mirrors, ported
# registries, and Docker Hub shorthand all split correctly:
#   - the segment before the first "/" is the registry host ONLY if it
#     contains "." or ":" or equals "localhost"; otherwise the ref is on
#     Docker Hub and that segment is part of the path
#   - a single-segment Docker Hub path is namespaced under "library/"
#     ("nginx" → "library/nginx")
#   - a ":" introduces a tag ONLY after the last "/" — a ":" before it
#     is a registry port (registry:5000/app), never a tag
_build_decompose_upstream_ref() {
  local ref="$1" rest first last_segment host path tag="" digest=""

  # 1. peel optional @sha256:... digest
  case "${ref}" in
    *@*) digest="${ref##*@}"; rest="${ref%@*}" ;;
    *)   rest="${ref}" ;;
  esac

  # 2. peel tag — only a ':' in the LAST path segment is a tag separator
  last_segment="${rest##*/}"
  case "${last_segment}" in
    *:*) tag="${last_segment##*:}"; rest="${rest%:*}" ;;
  esac

  # 3. split host from path using Docker's host-detection rule
  case "${rest}" in
    */*)
      first="${rest%%/*}"
      case "${first}" in
        *.*|*:*|localhost) host="${first}";   path="${rest#*/}" ;;
        *)                 host="docker.io";  path="${rest}"     ;;
      esac
      ;;
    *) host="docker.io"; path="${rest}" ;;
  esac

  # 4. Docker Hub single-segment repos live under library/
  case "${host}" in
    docker.io|index.docker.io)
      case "${path}" in
        */*) : ;;
        *)   path="library/${path}" ;;
      esac
      ;;
  esac

  _REF_HOST="${host}"; _REF_PATH="${path}"; _REF_TAG="${tag}"; _REF_DIGEST="${digest}"
}

# True for a well-formed manifest digest. Anything else is a typo, a
# truncated copy/paste or an uppercase hex string no registry accepts —
# catching it here beats a confusing pull error mid-build.
_build_valid_digest() {
  printf '%s' "${1:-}" | grep -qE '^sha256:[0-9a-f]{64}$'
}

# A per-segment override that DISAGREES with the ref it was decomposed
# from means two inputs name two different bases. Pick neither.
_build_reject_conflicting_override() {
  local name="$1" override="$2" parsed="$3"
  if [ -n "${override}" ] && [ -n "${parsed}" ] && [ "${override}" != "${parsed}" ]; then
    echo "ERROR: ${name}='${override}' conflicts with UPSTREAM_REF='${UPSTREAM_REF}' (which says '${parsed}')" >&2
    echo "       UPSTREAM_REF is the canonical base. Either drop ${name} or edit UPSTREAM_REF." >&2
    return 1
  fi
  return 0
}

# Re-assemble the canonical reference from its parts. Called once in
# PHASE 4 and again after PHASE 6 pins a tag-only base to its digest,
# so every consumer after that point sees the same immutable identity.
_build_compose_upstream_ref() {
  local ref="${UPSTREAM_REGISTRY}/${UPSTREAM_IMAGE}"
  if [ -n "${UPSTREAM_TAG}" ];    then ref="${ref}:${UPSTREAM_TAG}"; fi
  if [ -n "${UPSTREAM_DIGEST}" ]; then ref="${ref}@${UPSTREAM_DIGEST}"; fi
  printf '%s' "${ref}"
}

# Validate required fields + apply defaults. Fails fast on missing
# required fields.
_build_apply_defaults_and_normalise() {
  # Canonical input is a single full image URL in UPSTREAM_REF, e.g.
  #   UPSTREAM_REF="docker.io/library/nginx:1.25.3-alpine"
  #   UPSTREAM_REF="docker.io/library/nginx:1.25.3-alpine@sha256:<64 hex>"
  # Decompose it into the registry/image/tag/digest parts that the rest
  # of the build consumes. A digest on the way in is KEPT — it is the
  # whole point of pinning, and re-resolving it would let a moving tag
  # swap the base out from under the labels. Setting the per-segment
  # vars directly without UPSTREAM_REF still works; setting them
  # ALONGSIDE a UPSTREAM_REF that says something else does not.
  UPSTREAM_DIGEST=""
  if [ -n "${UPSTREAM_REF:-}" ]; then
    _build_decompose_upstream_ref "${UPSTREAM_REF}"
    _build_reject_conflicting_override UPSTREAM_REGISTRY "${UPSTREAM_REGISTRY:-}" "${_REF_HOST}" || return 1
    _build_reject_conflicting_override UPSTREAM_IMAGE    "${UPSTREAM_IMAGE:-}"    "${_REF_PATH}" || return 1
    _build_reject_conflicting_override UPSTREAM_TAG      "${UPSTREAM_TAG:-}"      "${_REF_TAG}"  || return 1
    UPSTREAM_REGISTRY="${_REF_HOST}"
    UPSTREAM_IMAGE="${_REF_PATH}"
    # A digest-only ref parses to an empty tag, so an explicit
    # UPSTREAM_TAG there is extra information, not a conflict.
    UPSTREAM_TAG="${UPSTREAM_TAG:-${_REF_TAG}}"
    UPSTREAM_DIGEST="${_REF_DIGEST}"
    if [ -n "${UPSTREAM_DIGEST}" ] && ! _build_valid_digest "${UPSTREAM_DIGEST}"; then
      echo "ERROR: UPSTREAM_REF carries a malformed digest: @${UPSTREAM_DIGEST}" >&2
      echo "       Expected @sha256:<64 lowercase hex>." >&2
      return 1
    fi
    _dbg "decomposed UPSTREAM_REF=${UPSTREAM_REF} → REGISTRY=${UPSTREAM_REGISTRY} IMAGE=${UPSTREAM_IMAGE} TAG=${UPSTREAM_TAG} DIGEST=${UPSTREAM_DIGEST:-<none>}"
  fi

  : "${UPSTREAM_REGISTRY:?set UPSTREAM_REF (full image URL) or UPSTREAM_REGISTRY in image.env}"
  : "${UPSTREAM_IMAGE:?set UPSTREAM_REF (full image URL) or UPSTREAM_IMAGE in image.env}"
  if [ -z "${UPSTREAM_TAG:-}" ] && [ -z "${UPSTREAM_DIGEST}" ]; then
    echo "ERROR: no upstream version — set UPSTREAM_REF with a :tag or an @sha256 digest," >&2
    echo "       or set UPSTREAM_TAG in image.env." >&2
    return 1
  fi

  # UPSTREAM_VERSION is the HUMAN-READABLE half of the identity, and the
  # only part that belongs in a docker tag. A digest-only base has no
  # tag to carry, so derive a short stable stand-in from the digest
  # rather than claiming "latest" — which would name a different image.
  if [ -n "${UPSTREAM_TAG:-}" ]; then
    UPSTREAM_VERSION="${UPSTREAM_TAG}"
  else
    UPSTREAM_VERSION="sha256-${UPSTREAM_DIGEST#sha256:}"
    UPSTREAM_VERSION="${UPSTREAM_VERSION:0:19}"
    _dbg "digest-only base → UPSTREAM_VERSION=${UPSTREAM_VERSION} (no tag to inherit)"
  fi

  # Defaults are SAFE-BY-DEFAULT: every optional behaviour is OFF
  # unless explicitly turned on. The bare-minimum build path is
  # "pull → retag → push" with no cert injection, no Xray, no SBOM.
  # Anything bespoke (package upgrades, extra installs, file drops)
  # goes directly in the Dockerfile's editable region — never sneaks
  # into the upstream template path via env-var toggles.
  # IMAGE_NAME defaults to UPSTREAM_IMAGE's LEAF segment (after the
  # last "/") so:
  #   nginx              → nginx
  #   library/nginx      → nginx
  #   prom/prometheus    → prometheus
  # Consumers can override with IMAGE_NAME= in image.env if they want
  # a different short name (e.g. "nginx-hardened").
  [ -z "${IMAGE_NAME:-}" ] && _dbg "default applied: IMAGE_NAME=${UPSTREAM_IMAGE##*/} (leaf of UPSTREAM_IMAGE=${UPSTREAM_IMAGE})"
  [ -z "${VENDOR:-}"     ] && _dbg "default applied: VENDOR=example.com (was unset)"

  IMAGE_NAME="${IMAGE_NAME:-${UPSTREAM_IMAGE##*/}}"
  VENDOR="${VENDOR:-example.com}"
  # ORIGINAL_USER has no default here on purpose: PHASE 6.5 resolves it
  # only when RESTORE_USER=true, and an unresolved one fails the build
  # rather than falling back to root.
}

# ════════════════════════════════════════════════════════════════════
# PHASE 2 — Tag computation + source URL
# ════════════════════════════════════════════════════════════════════
# Tag format:
#   <UPSTREAM_VERSION>-<gitShort>
# The upstream version IS the semver (its tag, or a short form of its
# digest when the base is pinned by digest alone); the git SHA
# differentiates builds of the same upstream version. No internal
# version axis.

# Portable epoch → ISO8601 UTC (GNU `date -d @N`, BSD/macOS `date -r N`).
_build_epoch_to_iso() {
  date -u -d "@$1" +%Y-%m-%dT%H:%M:%SZ 2>/dev/null \
    || date -u -r "$1"  +%Y-%m-%dT%H:%M:%SZ 2>/dev/null
}

_build_compute_tag() {
  if ! git rev-parse HEAD >/dev/null 2>&1; then
    GIT_SHA="unknown"
    GIT_SHORT="unknown"
  else
    GIT_SHA=$(git rev-parse HEAD)
    GIT_SHORT=$(git rev-parse --short=7 HEAD)
  fi

  # ── Reproducible build timestamp (SOURCE_DATE_EPOCH) ──────────────
  # Anchor the build clock to the git COMMIT time so the same commit
  # rebuilds to the SAME digest (a re-push then overwrites in place
  # instead of orphaning the old digest and breaking scans-by-digest).
  # Precedence: env override > git committer date > wall clock (no git,
  # not reproducible). See docs/DESIGN.md "Reproducible build digest".
  if [ -z "${SOURCE_DATE_EPOCH:-}" ] && [ "${GIT_SHA}" != "unknown" ]; then
    SOURCE_DATE_EPOCH=$(git show -s --format=%ct HEAD 2>/dev/null || echo "")
  fi
  if [ -z "${SOURCE_DATE_EPOCH:-}" ]; then
    SOURCE_DATE_EPOCH=$(date -u +%s)
    _dbg "no git commit time available — SOURCE_DATE_EPOCH=now (build not reproducible)"
  fi
  # NOT exported globally — passed inline to the buildx command in
  # _build_docker_build instead, so it can't leak into the caller's shell
  # and silently pin a later same-shell run's timestamp. BuildKit reads it
  # from that command's environment all the same.
  CREATED=$(_build_epoch_to_iso "${SOURCE_DATE_EPOCH}")
  _dbg "SOURCE_DATE_EPOCH=${SOURCE_DATE_EPOCH} → CREATED=${CREATED}"

  # APPEND_GIT_SHORT controls whether the pushed tag carries the
  # git short SHA. Default true (build differentiation matters when
  # rebuilding the same upstream tag). Set to false/0/no to keep the
  # raw upstream tag — useful when UPSTREAM_TAG is a moving alias
  # like "latest" or "stable" and you want the local image tag to
  # mirror that exactly. Falsy values: false/False/FALSE/0/no/No/NO.
  local _append="${APPEND_GIT_SHORT:-true}"
  case "$(printf '%s' "${_append}" | tr '[:upper:]' '[:lower:]')" in
    false|0|no|off)
      FULL_TAG="${UPSTREAM_VERSION}"
      _dbg "APPEND_GIT_SHORT=${_append} → tag=${FULL_TAG} (no SHA suffix)"
      ;;
    *)
      FULL_TAG="${UPSTREAM_VERSION}-${GIT_SHORT}"
      _dbg "APPEND_GIT_SHORT=${_append} → tag=${FULL_TAG}"
      ;;
  esac
}

# CI-supplied source URL (GitLab / Bamboo) or git remote fallback.
_build_resolve_source_url() {
  SOURCE_URL="${CI_PROJECT_URL:-${bamboo_planRepository_1_repositoryUrl:-}}"
  if [ -z "${SOURCE_URL}" ] && git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
    SOURCE_URL=$(git config --get remote.origin.url 2>/dev/null || echo "")
  fi
}

# ════════════════════════════════════════════════════════════════════
# PHASE 3 — Cert materialisation
# ════════════════════════════════════════════════════════════════════
# If CA_CERT is set (CI secret), write it to certs/ so the cert
# sidecar stage in the Dockerfile picks it up. Overwrites are
# intentional — CI runs should be reproducible. Typical CI source:
# curl from an Artifactory generic repo into the CA_CERT variable
# (or set the variable's value to the PEM directly).
#
# When certs/ stays empty the sidecar still runs and the final stage
# still COPYs its trust files over the upstream's — the bundle is
# replaced with the cert builder's, just without a corp CA added. The Dockerfile's FROM final re-bases FROM base so the
# sidecar's USER root never propagates into the final image.

_build_materialise_certs() {
  mkdir -p certs
  : > certs/.gitkeep

  if [ -n "${CA_CERT:-}" ]; then
    echo "${CA_CERT}" > certs/ci-injected.crt
    echo "→ Wrote CA_CERT to certs/ci-injected.crt ($(wc -c < certs/ci-injected.crt) bytes)"
    return 0
  fi

  _dbg "no CA_CERT in env — using certs/ on disk as-is (bundle is still replaced)"
}

# ════════════════════════════════════════════════════════════════════
# PHASE 4 — Push target derivation (backend-agnostic)
# ════════════════════════════════════════════════════════════════════
# build.sh stays out of every backend's namespace. Each push-backend
# (scripts/push-backends/<kind>.sh) is wholly responsible for:
#   - reading its OWN required env (HARBOR_* for harbor.sh,
#     ARTIFACTORY_* for artifactory_jcr.sh / artifactory_pro.sh, etc.)
#   - validating those vars when --push is requested
#   - retagging the local image to its push URL before docker push
#
# That means HARBOR_* and ARTIFACTORY_* are fully independent — you
# only need to set the namespace that matches your REGISTRY_KIND, and
# neither cross-derives from the other. Symmetric, no surprises.
#
# Build.sh just composes a SIMPLE local tag for docker build to use:
#   <IMAGE_NAME>:<FULL_TAG>          e.g. nginx:1.25.3-alpine-a1b2c3d
# Backends retag from this to their target URL during push_to_backend.

_build_resolve_push_target() {
  REGISTRY_KIND_LC="$(echo "${REGISTRY_KIND:-harbor}" | tr '[:upper:]' '[:lower:]')"
  _dbg "REGISTRY_KIND=${REGISTRY_KIND:-<unset>} → backend=${REGISTRY_KIND_LC}"

  FULL_IMAGE="${IMAGE_NAME}:${FULL_TAG}"
  # Carries the digest already if image.env supplied one. PHASE 6
  # re-composes this once it has resolved a tag-only base.
  UPSTREAM_REF="$(_build_compose_upstream_ref)"
}

# Pre-flight: source the selected push backend and run its require_env
# so a missing config (HARBOR_REGISTRY / ARTIFACTORY_URL / etc.) fails
# fast BEFORE we waste time on docker build + crane probes. Only runs
# when --push was requested. Fail-fast for an unknown REGISTRY_KIND
# happens here too — same error as the dispatch in PHASE 8 but earlier.
#
# Convention: each push-backends/<kind>.sh exposes a `${kind}_require_env`
# function (no leading underscore — it's part of the public contract).
# build.sh sources the backend and calls that function; an underscore-
# prefixed `_${kind}_require_env` is also accepted.
_build_validate_backend() {
  [ "${WANT_PUSH}" -eq 1 ] || return 0

  local kind="${REGISTRY_KIND_LC:-harbor}"
  local backend="${TEMPLATE_ROOT}/scripts/push-backends/${kind}.sh"
  if [ ! -f "${backend}" ]; then
    echo "ERROR: REGISTRY_KIND='${kind}' but ${backend} not found" >&2
    echo "       Available backends:" >&2
    ls "${TEMPLATE_ROOT}/scripts/push-backends/" 2>/dev/null | sed 's/\.sh$//' | sed 's/^/         /' >&2
    return 1
  fi

  # shellcheck disable=SC1090
  . "${backend}"

  # Try the public name (kind_require_env) first, then the underscore-
  # prefixed _kind_require_env, then no-op if neither exists.
  local fn
  for fn in "${kind}_require_env" "_${kind}_require_env"; do
    if declare -f "${fn}" >/dev/null 2>&1; then
      _dbg "early backend validation: calling ${fn}"
      "${fn}" || return 1
      return 0
    fi
  done
  _dbg "backend ${kind} has no require_env hook — skipping pre-flight"
}

# ════════════════════════════════════════════════════════════════════
# PHASE 5 — Report resolved config
# ════════════════════════════════════════════════════════════════════
# Printed BEFORE the upstream digest is resolved — the user sees
# progress immediately. Digest resolution runs next and can take a few
# seconds against slow/air-gapped registries.

_build_print_config_report() {
  echo ""
  echo "=========================================="
  echo "  container-build-template build"
  echo "=========================================="
  echo "  Image:              ${FULL_IMAGE}"
  echo "  Upstream:           ${UPSTREAM_REF}"
  echo "  Upstream digest:    <resolving...>"
  echo "  Restore USER:       ${RESTORE_USER:-false} (true = fork edits enter root)"
  echo "  Git commit:         ${GIT_SHORT} (${GIT_SHA})"
  echo "  Created (UTC):      ${CREATED}"
  echo "  Vendor:             ${VENDOR}"
  echo "  Source URL:         ${SOURCE_URL:-<none>}"
  echo "=========================================="
  echo ""
}

# ════════════════════════════════════════════════════════════════════
# PHASE 6 — Immutable reference resolution
# ════════════════════════════════════════════════════════════════════
# Turns every base reference into repo[:tag]@sha256:… exactly once, so
# FROM, the base inspection, the USER lookup and the labels all name
# the same image. Strategy per reference:
#   0. a digest already on the ref        — kept verbatim, no lookup
#   1. crane digest                       — fast, manifest-only
#   2. auto-install crane from CRANE_URL  — if not on PATH
#   3. docker buildx imagetools inspect   — fallback
# Failing all of those stops the build (see _build_require_pin).

# If no CRANE_URL is set, derive one matching host OS/arch.
_build_derive_crane_url() {
  [ -n "${CRANE_URL:-}" ] && return 0

  local _os="" _arch=""
  case "$(uname -s)" in
    Linux)  _os="Linux" ;;
    Darwin) _os="Darwin" ;;
  esac
  case "$(uname -m)" in
    x86_64|amd64)   _arch="x86_64" ;;
    aarch64|arm64)  _arch="arm64" ;;
  esac
  if [ -n "${_os}" ] && [ -n "${_arch}" ]; then
    CRANE_URL="https://github.com/google/go-containerregistry/releases/download/v0.20.2/go-containerregistry_${_os}_${_arch}.tar.gz"
  fi
}

# Try to install crane into ${HOME}/.local/bin from CRANE_URL. Never
# fatal — returns 0 on success, 1 on failure (caller falls back).
_build_install_crane() {
  if command -v crane >/dev/null 2>&1; then
    _dbg "crane already on PATH: $(command -v crane)"
    return 0
  fi
  _build_derive_crane_url

  if [ -z "${CRANE_URL:-}" ]; then
    echo "  NOTE: crane not on PATH and CRANE_URL not set — skipping install" >&2
    echo "        (will fall back to docker buildx imagetools inspect)" >&2
    _dbg "uname=$(uname -s)/$(uname -m) didn't match a known crane release URL"
    return 1
  fi

  echo "→ crane not on PATH — installing from ${CRANE_URL}"
  mkdir -p "${HOME}/.local/bin"
  if curl -fSL --progress-bar --max-time 120 "${CRANE_URL}" \
       | tar xz -C "${HOME}/.local/bin" crane 2>/dev/null \
     && [ -x "${HOME}/.local/bin/crane" ]; then
    export PATH="${HOME}/.local/bin:${PATH}"
    echo "  ✓ crane installed to ${HOME}/.local/bin/crane ($(${HOME}/.local/bin/crane version 2>&1 | head -1))"
    return 0
  fi

  echo "  WARN: crane install failed — URL unreachable or tarball invalid" >&2
  echo "        (will fall back to docker buildx imagetools inspect)" >&2
  return 1
}

# Resolve ONE reference to its manifest digest, echoed on stdout.
# Shared by the upstream base and the cert builder so both obey the
# same policy. Returns 1 when neither tool produced a usable digest.
_build_resolve_digest_for() {
  local ref="$1" _out _rc

  if command -v crane >/dev/null 2>&1; then
    _out=$(crane digest "${ref}" 2>&1) && _rc=0 || _rc=$?
    if [ "${_rc}" -eq 0 ] && _build_valid_digest "${_out}"; then
      printf '%s' "${_out}"
      return 0
    fi
    echo "  WARN: crane digest failed (rc=${_rc}) for ${ref}" >&2
    printf '%s\n' "${_out}" | head -2 | sed 's/^/        /' >&2
  fi

  if command -v docker >/dev/null 2>&1; then
    _out=$(docker buildx imagetools inspect "${ref}" --format '{{.Digest}}' 2>/dev/null || echo "")
    if _build_valid_digest "${_out}"; then
      printf '%s' "${_out}"
      return 0
    fi
    echo "  WARN: docker buildx imagetools inspect also failed for ${ref}" >&2
  fi
  return 1
}

_build_resolve_base_digest() {
  _build_install_crane || true   # PHASE 6.5's USER probe needs it too

  # Already pinned: the digest IS the answer. Asking the registry again
  # would be a second lookup of a moving tag, which can return a
  # different image than the one the ref names.
  if [ -n "${UPSTREAM_DIGEST}" ]; then
    BASE_DIGEST="${UPSTREAM_DIGEST}"
    echo "→ Upstream already pinned: ${UPSTREAM_REF}"
    return 0
  fi

  echo "→ Resolving upstream digest: ${UPSTREAM_REF}"
  BASE_DIGEST=$(_build_resolve_digest_for "${UPSTREAM_REF}") || return 1
  UPSTREAM_DIGEST="${BASE_DIGEST}"
  UPSTREAM_REF="$(_build_compose_upstream_ref)"
  echo "  ✓ pinned upstream: ${UPSTREAM_REF}"
}

# Same policy for the cert sidecar's base. It contributes the trust
# store the final image ships, so "whatever alpine:3.20 points at
# today" is as much of a supply-chain gap as an unpinned FROM.
_build_resolve_cert_builder() {
  # Keep this default in sync with Dockerfile.example's ARG.
  CERT_BUILDER_IMAGE="${CERT_BUILDER_IMAGE:-docker.io/library/alpine:3.20}"

  local _digest=""
  case "${CERT_BUILDER_IMAGE}" in *@*) _digest="${CERT_BUILDER_IMAGE##*@}" ;; esac
  if [ -n "${_digest}" ]; then
    if ! _build_valid_digest "${_digest}"; then
      echo "ERROR: CERT_BUILDER_IMAGE carries a malformed digest: @${_digest}" >&2
      echo "       Expected @sha256:<64 lowercase hex>." >&2
      return 2
    fi
    echo "→ Cert builder already pinned: ${CERT_BUILDER_IMAGE}"
    return 0
  fi

  echo "→ Resolving cert builder digest: ${CERT_BUILDER_IMAGE}"
  _digest=$(_build_resolve_digest_for "${CERT_BUILDER_IMAGE}") || return 1
  CERT_BUILDER_IMAGE="${CERT_BUILDER_IMAGE}@${_digest}"
  echo "  ✓ pinned cert builder: ${CERT_BUILDER_IMAGE}"
}

# Digest resolution is FAIL-CLOSED. Building from a tag we could not
# resolve means building from whatever that tag points at right now,
# which is not the base the labels, the SBOM and the scan report will
# claim. --dry-run resolves config without producing an image, so there
# an unreachable registry is a warning, not a stop.
# Return 2 from a resolver marks a config error — always fatal.
_build_require_pin() {
  local _rc=0
  "$@" || _rc=$?
  if [ "${_rc}" -eq 0 ]; then
    return 0
  fi
  if [ "${_rc}" -ne 1 ] || [ "${WANT_DRY_RUN}" -ne 1 ]; then
    echo "ERROR: could not pin an immutable digest — refusing to build from an" >&2
    echo "       unresolved tag. Fix registry auth, set CRANE_URL for an" >&2
    echo "       air-gapped crane download, or pin it yourself with @sha256:<64 hex>." >&2
    exit 1
  fi
  echo "  --dry-run: continuing UNPINNED — a real build stops here." >&2
}

# ════════════════════════════════════════════════════════════════════
# PHASE 6.5 — Runtime user resolution
# ════════════════════════════════════════════════════════════════════
# A Dockerfile cannot emit a USER line conditionally, so the template
# emits none at all: the final stage re-bases FROM base and the image
# keeps whatever user the upstream configured. That is the certificate-
# only recipe, and nothing in this phase has to run for it.
#
# A fork whose FORK EDITS region switches to `USER root` does have to
# switch back. It says so with RESTORE_USER=true in image.env and
# uncomments the `USER ${ORIGINAL_USER}` line in that region. Then this
# phase has to produce a user, by one of two routes:
#   1. ORIGINAL_USER set in image.env / shell env → used verbatim
#   2. `crane config <pinned upstream>` → .config.User
#
# A config that declares no User is a real answer: the upstream runs as
# root, so ORIGINAL_USER=root and the build says which one it is. A
# FAILED inspection is not an answer — falling back to root there would
# silently escalate an image that was meant to stay non-root, so the
# build stops instead (a --dry-run only warns, it produces no image).

_build_resolve_upstream_user() {
  if [ "${RESTORE_USER:-false}" != "true" ]; then
    if [ -n "${ORIGINAL_USER:-}" ]; then
      echo "  NOTE: ORIGINAL_USER is set but RESTORE_USER is not true — no USER line" >&2
      echo "        is emitted, so the base image's user stands and this is ignored." >&2
    fi
    ORIGINAL_USER=""
    export ORIGINAL_USER
    echo "→ RESTORE_USER is not true → final image inherits the base image's USER"
    return 0
  fi

  if [ -n "${ORIGINAL_USER:-}" ]; then
    echo "→ ORIGINAL_USER explicitly set: ${ORIGINAL_USER} (skipping auto-detect)"
    export ORIGINAL_USER
    return 0
  fi

  echo "→ Detecting upstream USER: crane config ${UPSTREAM_REF}"
  local _config="" _user=""
  if command -v crane >/dev/null 2>&1; then
    _config=$(crane config "${UPSTREAM_REF}" 2>/dev/null) || _config=""
  else
    echo "  crane is not on PATH — the upstream config cannot be read" >&2
  fi

  if [ -z "${_config}" ]; then
    echo "ERROR: could not inspect ${UPSTREAM_REF}, so the user to restore is" >&2
    echo "       unknown. RESTORE_USER=true means the build enters root, and root" >&2
    echo "       is not a safe guess to come back to. Fix registry auth or the" >&2
    echo "       crane install, or set ORIGINAL_USER explicitly in image.env." >&2
    if [ "${WANT_DRY_RUN}" -ne 1 ]; then
      exit 1
    fi
    echo "  --dry-run: continuing UNRESOLVED — a real build stops here." >&2
    ORIGINAL_USER=""
    export ORIGINAL_USER
    return 0
  fi

  if command -v jq >/dev/null 2>&1; then
    _user=$(printf '%s' "${_config}" | jq -r '.config.User // ""' 2>/dev/null)
  else
    # Fallback parser: grep .config.User from the JSON. Brittle but
    # works for the common single-line / pretty-printed case.
    _user=$(printf '%s' "${_config}" | grep -oE '"User"[[:space:]]*:[[:space:]]*"[^"]*"' \
                                     | head -1 \
                                     | sed -E 's/.*"User"[[:space:]]*:[[:space:]]*"([^"]*)".*/\1/')
  fi

  # Whatever the config holds goes through byte-for-byte: "nginx",
  # "1000:1000" and "app:app" all mean different things to Docker.
  if [ -n "${_user}" ]; then
    ORIGINAL_USER="${_user}"
    echo "  ✓ ORIGINAL_USER auto-detected: ${ORIGINAL_USER}"
  else
    ORIGINAL_USER="root"
    echo "  ✓ upstream config declares no USER — it runs as root → ORIGINAL_USER=root"
  fi
  export ORIGINAL_USER
}

# ════════════════════════════════════════════════════════════════════
# PHASE 7 — docker build
# ════════════════════════════════════════════════════════════════════
# Dynamic OCI labels passed via --label. Label policy: preserve
# upstream, append ours. See Dockerfile for the reasoning — we
# explicitly own only the dynamic provenance labels and team
# identity; everything else flows through untouched.

# The OCI exporter needs buildx's docker-container driver; the default
# "docker" driver supports only the local, tarball and image exporters.
# Created once under our own name and addressed with --builder, rather
# than `--use`, so a developer's default builder stays where they left it.
BUILDX_BUILDER="${BUILDX_BUILDER:-container-build-template}"

_build_ensure_container_builder() {
  if docker buildx inspect "${BUILDX_BUILDER}" >/dev/null 2>&1; then
    _dbg "buildx builder ${BUILDX_BUILDER} already exists"
    return 0
  fi
  echo "→ creating buildx builder ${BUILDX_BUILDER} (docker-container driver)"
  docker buildx create --name "${BUILDX_BUILDER}" --driver docker-container >/dev/null || {
    echo "ERROR: could not create the '${BUILDX_BUILDER}' buildx builder, so the OCI" >&2
    echo "       archive this build hands downstream cannot be exported." >&2
    return 1
  }
}

_build_docker_build() {
  local build_args=(
    --build-arg "UPSTREAM_REGISTRY=${UPSTREAM_REGISTRY}"
    --build-arg "UPSTREAM_IMAGE=${UPSTREAM_IMAGE}"
    --build-arg "UPSTREAM_TAG=${UPSTREAM_TAG}"
    # THE canonical base: one full URL carrying the digest PHASE 6
    # pinned. The Dockerfile's `FROM ${UPSTREAM_REF}` uses this, so the
    # image is built from the same bytes crane inspected. The three
    # parts above are kept for forks that read them; nothing in the
    # template's own FROM lines depends on them any more.
    --build-arg "UPSTREAM_REF=${UPSTREAM_REF}"
    # Pinned by PHASE 6 the same way. Override via image.env / shell env
    # for air-gap to point at your internal Artifactory / Nexus mirror.
    --build-arg "CERT_BUILDER_IMAGE=${CERT_BUILDER_IMAGE}"
  )
  # Only reaches the Dockerfile when RESTORE_USER=true put a value in
  # it. The certificate-only recipe declares no ORIGINAL_USER ARG, and
  # an unconsumed build-arg is a warning on every build.
  if [ -n "${ORIGINAL_USER:-}" ]; then
    build_args+=(--build-arg "ORIGINAL_USER=${ORIGINAL_USER}")
  fi
  local label_args=(
    --label "org.opencontainers.image.vendor=${VENDOR}"
    --label "org.opencontainers.image.authors=${AUTHORS:-Platform Engineering}"
    --label "org.opencontainers.image.created=${CREATED}"
    --label "org.opencontainers.image.revision=${GIT_SHA}"
    --label "org.opencontainers.image.version=${FULL_TAG}"
    --label "org.opencontainers.image.ref.name=${FULL_TAG}"
    --label "org.opencontainers.image.base.name=${UPSTREAM_REF}"
    --label "promoted.from=${UPSTREAM_REF}"
    --label "promoted.tag=${FULL_TAG}"
  )
  if [ -n "${BASE_DIGEST}" ]; then
    label_args+=(--label "org.opencontainers.image.base.digest=${BASE_DIGEST}")
  fi
  if [ -n "${SOURCE_URL}" ]; then
    label_args+=(--label "org.opencontainers.image.source=${SOURCE_URL}")
    label_args+=(--label "org.opencontainers.image.url=${SOURCE_URL}")
  fi

  # Two exporters, because the two paths consume the result differently.
  #
  # --push: every backend retags the built image and pushes it from the
  # local daemon, so the build must `--load`, and the docker exporter
  # takes a flat single-arch manifest only — hence
  # `--provenance=false --sbom=false`. REGISTRY_KIND=artifactory_jcr
  # additionally REQUIRES the flat form: an OCI index breaks its Free-tier
  # build-info merger. We don't use buildx attestations (Syft/Grype/Xray
  # cover that), so this is lossless. docs/DESIGN.md "buildx attestation flags".
  #
  # no --push: nothing is pushed, so the hand-off is an OCI image layout
  # archive that downstream jobs scan as `oci-archive:` with no daemon and
  # no registry (decision D8). The OCI exporter is unavailable on buildx's
  # default "docker" driver, so this path needs a docker-container builder.
  local _build_cmd=(docker build) _export_args=()
  if docker buildx version >/dev/null 2>&1; then
    if [ "${WANT_PUSH}" -eq 1 ]; then
      _build_cmd=(docker buildx build --provenance=false --sbom=false --load)
      echo "→ docker buildx build --load (provenance/sbom disabled — the push backend pushes from the daemon)"
    else
      _build_ensure_container_builder || return 1
      _build_cmd=(docker buildx build --builder "${BUILDX_BUILDER}")
      _export_args=(--output "type=oci,dest=${IMAGE_ARCHIVE}")
      echo "→ docker buildx build --output type=oci,dest=${IMAGE_ARCHIVE} (builder ${BUILDX_BUILDER})"
    fi
    _export_args+=(--metadata-file "${BUILD_METADATA_FILE}")
  elif [ "${WANT_PUSH}" -eq 1 ]; then
    echo "→ docker build (buildx not detected — flat manifest by default)"
  else
    echo "ERROR: a no-push build hands downstream jobs an OCI archive, which needs" >&2
    echo "       'docker buildx' with a docker-container builder. buildx is not" >&2
    echo "       installed. Install it, or run with --push." >&2
    return 1
  fi
  # SOURCE_DATE_EPOCH is passed INLINE (command-scoped) rather than
  # exported, so BuildKit clamps layer/config timestamps for a
  # reproducible digest without the value leaking into the caller's shell.
  SOURCE_DATE_EPOCH="${SOURCE_DATE_EPOCH}" "${_build_cmd[@]}" \
    "${build_args[@]}" "${label_args[@]}" \
    ${_export_args[@]+"${_export_args[@]}"} -t "${FULL_IMAGE}" \
    -f "${DOCKERFILE}" .
  echo "→ build complete: ${FULL_IMAGE}"

  # Export derived values so the sourced backend script can pull them
  # in via parameter expansion when building the identity record.
  export UPSTREAM_REGISTRY UPSTREAM_IMAGE UPSTREAM_TAG UPSTREAM_DIGEST
  export UPSTREAM_REF BASE_DIGEST GIT_SHA CREATED
}

# ════════════════════════════════════════════════════════════════════
# PHASE 8a — Archive hand-off (no-push only)
# ════════════════════════════════════════════════════════════════════
# A no-push build still has to tell downstream jobs what it built, and
# the only honest answer is the archive it just wrote: its manifest
# digest, read out of the archive's own index.json, plus the file's
# sha256 so a consumer can tell a truncated artifact from a whole one.
# No registry reference is invented — nothing was published.
#
# SBOM_FILE / VULN_SCAN_FILE in the record are filename DECLARATIONS for
# the scan stage; build.sh does not generate those artifacts.

_build_emit_archive_identity() {
  _ii_require_jq || return 1

  if [ ! -s "${IMAGE_ARCHIVE}" ]; then
    echo "ERROR: the build reported success but wrote no OCI archive at ${IMAGE_ARCHIVE}" >&2
    return 1
  fi

  IMAGE_DIGEST="$(image_archive_digest "${IMAGE_ARCHIVE}")"
  if ! _ii_valid_digest "${IMAGE_DIGEST}"; then
    echo "ERROR: ${IMAGE_ARCHIVE} index.json names no manifest digest (got '${IMAGE_DIGEST}')" >&2
    return 1
  fi

  # BuildKit reports the same digest in --metadata-file. When both are
  # present they must agree; a disagreement means the archive on disk is
  # not what this build exported.
  if [ -s "${BUILD_METADATA_FILE}" ]; then
    local _meta_digest
    _meta_digest="$(jq -r '."containerimage.digest" // ""' "${BUILD_METADATA_FILE}")"
    if [ -n "${_meta_digest}" ] && [ "${_meta_digest}" != "${IMAGE_DIGEST}" ]; then
      echo "ERROR: build metadata says ${_meta_digest}, ${IMAGE_ARCHIVE} says ${IMAGE_DIGEST}" >&2
      return 1
    fi
  fi

  # Exported because write_image_identity reads them, and because the
  # SBOM stage build.sh may hand off to reads IMAGE_REF / IMAGE_DIGEST.
  export IMAGE_TRANSPORT="oci-archive"
  export IMAGE_REF="oci-archive:${IMAGE_ARCHIVE}"
  export IMAGE_TAG_REF=""
  # Unpublished: the repository is the local image name, with no registry
  # host in front of it that would read as pullable.
  export IMAGE_REPOSITORY="${IMAGE_NAME}"
  export IMAGE_TAG="${FULL_TAG}"
  export IMAGE_DIGEST
  IMAGE_ARCHIVE_SHA256="$(_ii_file_sha256 "${IMAGE_ARCHIVE}")"
  IMAGE_SUBJECT_KIND="$(image_archive_subject_kind "${IMAGE_ARCHIVE}")"
  IMAGE_PLATFORMS="$(image_archive_platforms "${IMAGE_ARCHIVE}")"
  export IMAGE_ARCHIVE_SHA256 IMAGE_SUBJECT_KIND IMAGE_PLATFORMS

  write_image_identity || return 1
  sed 's/^/    /' build.env
}

# ════════════════════════════════════════════════════════════════════
# PHASE 8b — Push + build.env override (delegated to push backend)
# ════════════════════════════════════════════════════════════════════
# Every push backend lives at scripts/push-backends/<kind>.sh and
# exports a single function: push_to_backend "<built-local-ref>".
# That function is responsible for:
#   1. docker login to its target host
#   2. docker push (or jf docker push, etc.)
#   3. resolving the pushed MANIFEST digest and calling
#      write_image_identity (scripts/lib/image-identity.sh), which is the
#      one writer of image.json + build.env. A backend supplies only its
#      registry/repository values; the schema is not its to choose.
#
# Adding a new backend = drop a new file in push-backends/ that
# exposes push_to_backend. No edits to build.sh required. Swap by
# changing REGISTRY_KIND in image.env.
#
# REGISTRY_KIND defaults to "harbor" (plain docker push). Other
# shipped backends: "artifactory_jcr", "artifactory_pro".

_build_push_and_emit_env() {
  local kind="${REGISTRY_KIND_LC:-harbor}"
  local backend="${TEMPLATE_ROOT}/scripts/push-backends/${kind}.sh"
  if [ ! -f "${backend}" ]; then
    echo "ERROR: REGISTRY_KIND='${kind}' but ${backend} not found" >&2
    echo "       Available backends:" >&2
    ls "${TEMPLATE_ROOT}/scripts/push-backends/" 2>/dev/null | sed 's/\.sh$//' | sed 's/^/         /' >&2
    return 1
  fi

  _dbg "dispatching push: backend=${kind} target=${FULL_IMAGE}"
  # shellcheck disable=SC1090
  . "${backend}"
  push_to_backend "${FULL_IMAGE}" || return 1

  echo "→ identity record written by push backend: ${kind}"
  sed 's/^/    /' build.env
}

# ════════════════════════════════════════════════════════════════════
# Orchestrator
# ════════════════════════════════════════════════════════════════════
# One phase per line. Phase helpers never skip downstream work — any
# failure returns non-zero here and the orchestrator exits.

_build_parse_args "$@"

import_bamboo_vars   # from scripts/lib/load-image-env.sh
load_image_env       # from scripts/lib/load-image-env.sh
_build_apply_defaults_and_normalise

_build_compute_tag
_build_resolve_source_url
_build_materialise_certs
_build_resolve_push_target
_build_validate_backend          # fail-fast on missing HARBOR_*/ARTIFACTORY_*

# Docker logins for ALL registries we might pull from (the upstream
# host, HARBOR_REGISTRY, ARTIFACTORY_PUSH_HOST). MUST run BEFORE base
# digest + USER resolution: crane reads ~/.docker/config.json, so an
# auth-protected upstream mirror needs the login in place first — else
# `crane digest` (base.digest label) and `crane config` (the USER probe
# RESTORE_USER=true asks for) both 403 anonymously, emptying base.digest
# and failing the build that needed a user to restore. The push backend still does its
# own login for the push target later. Upstream pull login is opt-in
# (UPSTREAM_REGISTRY_USER + UPSTREAM_REGISTRY_PASSWORD) — see docker-login.sh.
# shellcheck source=lib/docker-login.sh
. "${TEMPLATE_ROOT}/scripts/lib/docker-login.sh"
docker_login_all_registries || true

_build_print_config_report
_build_require_pin _build_resolve_base_digest
_build_require_pin _build_resolve_cert_builder
_build_resolve_upstream_user

# --dry-run stops here: config resolved, digest fetched, USER probed.
if [ "${WANT_DRY_RUN}" -eq 1 ]; then
  echo "→ --dry-run: stopping before docker build"
  echo "  NOTE: build.env was NOT written/refreshed by --dry-run. A scan run"
  echo "        now would read any PRE-EXISTING build.env (possibly stale)."
  exit 0
fi

_build_docker_build

# One identity record per build, written by whichever path produced the
# image. Both go through write_image_identity, so a downstream job reads
# the same fields either way.
if [ "${WANT_PUSH}" -eq 1 ]; then
  _build_push_and_emit_env
else
  _build_emit_archive_identity
fi

# SBOM generation lives in scripts/scan/syft-sbom.sh as its own stage —
# call it after build (or as a CI postscan job) when you want a BOM.
