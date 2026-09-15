#!/usr/bin/env bash
# ─── DO NOT EDIT (unless adding scenarios) ─────────────────────────
# Template self-tests. Run before pushing changes that touch build.sh,
# scan/*, ingest/sbom-post.sh, or lib/*. Per-fork repos don't need to
# vendor this — it's only useful when changing the template itself.
# ───────────────────────────────────────────────────────────────────
#
# scripts/test/regression.sh — local regression suite for build.sh + scan scripts
#
# Exercises every behavioural permutation we've shipped so far. Catches
# the "pristine green CI hides regressions" class of bug — every scenario
# below has either bitten us or could plausibly bite us next.
#
# Each scenario:
#   1. Resets image.env from scripts/test/image.env.fixture
#   2. Mutates env / image.env to the scenario's shape
#   3. Runs build.sh --dry-run (or other script) capturing stdout+stderr
#   4. Asserts expected markers appear (or absent) in the output
#
# Offline by design: no scenario needs a registry, a docker daemon or
# credentials. See the stub block below.
#
# Run with:
#   bash scripts/test/regression.sh                  # all scenarios
#   bash scripts/test/regression.sh registry-kind    # filter by name substring
#
# Exit 0 if every scenario passes, non-zero with summary otherwise.

set -uo pipefail

TEMPLATE_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
PROJECT_ROOT="${PROJECT_ROOT:-$(pwd)}"
export TEMPLATE_ROOT PROJECT_ROOT
cd "${PROJECT_ROOT}" || exit 1

FILTER="${1:-}"
FAILURES=()
PASSES=0
SKIPPED=0
CURRENT_NAME=""
TMP_DIR=$(mktemp -d)

# image.env is gitignored, so a fresh clone has none and a developer's
# copy would make every assertion depend on whatever they last edited.
# Scenarios reset from the committed fixture instead; a local image.env
# is stashed and put back on the way out.
FIXTURE="${TEMPLATE_ROOT}/scripts/test/image.env.fixture"
SNAPSHOT="${TMP_DIR}/image.env.snapshot"
if ! cp "${FIXTURE}" "${SNAPSHOT}"; then
  echo "ERROR: fixture not readable: ${FIXTURE}" >&2
  exit 1
fi
if [ -f image.env ]; then cp image.env "${TMP_DIR}/image.env.local"; fi
cp "${SNAPSHOT}" image.env
# Scan scenarios run the real producers against this checkout, so they
# create the per-instance evidence root here. Remove it on the way out,
# but only when the run is what created it.
ARTIFACT_ROOT_PREEXISTING=false
[ -d artifacts ] && ARTIFACT_ROOT_PREEXISTING=true

_restore_image_env() {
  if [ -f "${TMP_DIR}/image.env.local" ]; then
    cp "${TMP_DIR}/image.env.local" image.env
  else
    rm -f image.env
  fi
  [ "${ARTIFACT_ROOT_PREEXISTING}" = true ] || rm -rf artifacts
  rm -rf "${TMP_DIR}"
}
trap _restore_image_env EXIT

# Stubs for the three binaries that would otherwise reach a registry,
# on PATH for every scenario. crane and docker are where a --dry-run
# leaves the machine (digest resolution, upstream USER probe, docker
# login, docker pull), and several scenarios name hosts nothing can
# resolve (registry.example.com:5000, harbor.invalid). Failing both
# instantly — non-fatal in build.sh — keeps the suite testing config
# resolution rather than the network, at the same speed on a laptop and
# on an offline runner.
#
# jf exits 0 because the scan scripts skip the whole Xray path when jf
# is absent: on a machine that happens to have the real JFrog CLI the
# xray scenarios ran against a live endpoint, and on one without it they
# passed by taking the skip. The stub pins both to the same path.
#
# Scenarios needing a richer stub (the xray re-tag ones) prepend their
# own directory, which therefore wins over this one.
STUB_BIN="${TMP_DIR}/stub-bin"
mkdir -p "${STUB_BIN}"
printf '#!/bin/sh\nexit 1\n' > "${STUB_BIN}/crane"
printf '#!/bin/sh\nexit 1\n' > "${STUB_BIN}/docker"
printf '#!/bin/sh\nexit 0\n' > "${STUB_BIN}/jf"
chmod +x "${STUB_BIN}/crane" "${STUB_BIN}/docker" "${STUB_BIN}/jf"
export PATH="${STUB_BIN}:${PATH}"

# Well-formed but obviously fake digests. Three distinct ones so an
# assertion can tell the upstream, the pushed image and the archived
# image apart — the whole point of the identity contract is that those
# are not interchangeable.
_D1="sha256:1111111111111111111111111111111111111111111111111111111111111111"
_D2="sha256:2222222222222222222222222222222222222222222222222222222222222222"
_D3="sha256:3333333333333333333333333333333333333333333333333333333333333333"

# ── The one docker stub ─────────────────────────────────────────────
# Every scenario that runs a real build needs the same three things from
# docker: record the argv, answer the buildx probes, and write an OCI
# archive when asked to export one (build.sh checks the export is real,
# so an exit-0 that produces no file is not enough). Scenarios steer it
# with STUB_* variables instead of each keeping its own copy.
DOCKER_STUB="${TMP_DIR}/docker-stub"
cat > "${DOCKER_STUB}" <<'STUB'
#!/bin/sh
printf 'DOCKER %s\n' "$*" >> "${PIN_LOG:-/dev/null}"
if [ "$1" = "buildx" ]; then
  case "$2" in
    version) exit 0 ;;
    inspect) [ -n "${STUB_NO_BUILDER:-}" ] && exit 1; exit 0 ;;
    create)  printf 'create %s\n' "$*" >> "${STUB_BUILDER_LOG:-/dev/null}"; exit 0 ;;
    build)
      dest=""; meta=""; prev=""
      for a in "$@"; do
        case "${prev}" in
          --output)        dest="${a#*dest=}"; [ "${dest}" = "${a}" ] && dest="" ;;
          --metadata-file) meta="${a}" ;;
        esac
        prev="${a}"
      done
      if [ -n "${dest}" ]; then
        d=$(mktemp -d)
        printf '{"schemaVersion":2,"manifests":[{"mediaType":"application/vnd.oci.image.manifest.v1+json","digest":"%s","size":7,"platform":{"os":"linux","architecture":"amd64"}}]}\n' \
          "${STUB_ARCHIVE_DIGEST:-sha256:2222222222222222222222222222222222222222222222222222222222222222}" \
          > "${d}/index.json"
        tar -cf "${dest}" -C "${d}" index.json
        rm -rf "${d}"
      fi
      [ -n "${meta}" ] && printf '{"containerimage.digest":"%s"}\n' \
        "${STUB_META_DIGEST:-${STUB_ARCHIVE_DIGEST:-sha256:2222222222222222222222222222222222222222222222222222222222222222}}" \
        > "${meta}"
      exit 0 ;;
  esac
  exit 0
fi
if [ "$1" = "push" ]; then
  printf 'The push refers to repository [%s]\n' "$2"
  [ -n "${STUB_PUSH_DIGEST:-}" ] && printf 'stub: digest: %s size: 1234\n' "${STUB_PUSH_DIGEST}"
  exit 0
fi
exit 0
STUB
chmod +x "${DOCKER_STUB}"

# A build.env in the shape a push backend writes it.
_write_registry_build_env() {
  local proj="$1" digest="$2"
  cat > "${proj}/build.env" <<EOF
IMAGE_REF=reg.example.com/img@${digest}
IMAGE_TAG_REF=reg.example.com/img:1.2.3
IMAGE_TAG=1.2.3
IMAGE_DIGEST=${digest}
IMAGE_TRANSPORT=registry
EOF
}

# Drive scan-common's subject resolution on its own: no scanner, no
# network, no tool install — just the contract between what the build
# wrote and what a consumer is allowed to scan.
_resolve_subject() {
  local proj="$1"
  env -i HOME="${HOME}" PATH="${PATH}" LOAD_ENV_LOG=false \
    TEMPLATE_ROOT="${TEMPLATE_ROOT}" PROJECT_ROOT="${proj}" \
    bash -c 'cd "${PROJECT_ROOT}" && . "${TEMPLATE_ROOT}/scripts/lib/scan-common.sh" \
             && scan_bootstrap && resolve_scan_ref ""'
}

# An OCI layout archive holding nothing but an index.json naming
# ${2}. Enough for every consumer-side check: the digest lives in
# index.json, so that is what has to be read and compared.
_make_oci_archive() {
  local path="$1" digest="$2" kind="${3:-manifest}"
  local media="application/vnd.oci.image.manifest.v1+json"
  [ "${kind}" = "index" ] && media="application/vnd.oci.image.index.v1+json"
  local d="${TMP_DIR}/oci-build"
  rm -rf "${d}"; mkdir -p "${d}"
  printf '{"schemaVersion":2,"manifests":[{"mediaType":"%s","digest":"%s","size":7,"platform":{"os":"linux","architecture":"amd64"}}]}\n' \
    "${media}" "${digest}" > "${d}/index.json"
  tar -cf "${path}" -C "${d}" index.json
  rm -rf "${d}"
}

# Quiet-mode dry-run: just resolves config and stops before docker build
_run() {
  local out="${TMP_DIR}/out"
  ( "$@" ) > "${out}" 2>&1
  local rc=$?
  cat "${out}"
  return ${rc}
}

# Assert STDOUT contains a substring; mark failure otherwise.
_must_contain() {
  local what="$1"
  if ! grep -qF -- "${what}" "${TMP_DIR}/out" 2>/dev/null; then
    FAILURES+=("${CURRENT_NAME}: expected to find: ${what}")
    return 1
  fi
  return 0
}

# Assert STDOUT does NOT contain a substring.
_must_not_contain() {
  local what="$1"
  if grep -qF -- "${what}" "${TMP_DIR}/out" 2>/dev/null; then
    FAILURES+=("${CURRENT_NAME}: should NOT contain: ${what}")
    return 1
  fi
  return 0
}

# Scenario gate. Call sites are `if scenario "name"; then … end_scenario; fi`
# because a scenario body is top-level code: returning from this function
# does not skip what follows it, so the caller has to do the skipping.
# Returns 1 when a name filter is set and this scenario does not match.
scenario() {
  local name="$1"
  CURRENT_NAME="${name}"
  if [ -n "${FILTER}" ] && [[ "${name}" != *"${FILTER}"* ]]; then
    SKIPPED=$((SKIPPED + 1))
    return 1
  fi
  printf '\n══════════════════════════════════════════════════════════════════\n'
  printf '  Scenario: %s\n' "${name}"
  printf '══════════════════════════════════════════════════════════════════\n'
  cp "${SNAPSHOT}" image.env  # reset
}

end_scenario() {
  local pre_failures=0
  if [ "${#FAILURES[@]}" -gt 0 ]; then
    pre_failures=$(printf '%s\n' "${FAILURES[@]}" | grep -c "^${CURRENT_NAME}:" || true)
  fi
  if [ "${pre_failures}" -eq 0 ]; then
    printf '  ✓ PASS\n'
    PASSES=$((PASSES + 1))
  else
    printf '  ✗ FAIL (%d assertion(s) failed)\n' "${pre_failures}" >&2
  fi
}

# ════════════════════════════════════════════════════════════════════
# image.env precedence + sourcing
# ════════════════════════════════════════════════════════════════════

if scenario "missing-image-env"; then
mv image.env image.env.tmp
out=$(env -i HOME="$HOME" PATH="$PATH" ./scripts/build.sh --dry-run 2>&1) ; rc=$?
mv image.env.tmp image.env
echo "${out}" > "${TMP_DIR}/out"
echo "${out}" | head -10
[ "${rc}" -ne 0 ] || FAILURES+=("${CURRENT_NAME}: expected non-zero exit")
_must_contain "ERROR: image.env not found"
_must_contain "cp image.env.example image.env"
end_scenario; fi

if scenario "default-no-overrides"; then
_run env -i HOME="$HOME" PATH="$PATH" ./scripts/build.sh --dry-run >/dev/null
# load_image_env resolves to ${PROJECT_ROOT}/image.env (absolute), so
# assert the marker prefix + the filename suffix separately rather than
# the old relative "→ Sourcing image.env" string.
_must_contain "→ Sourcing "
_must_contain "/image.env"
_must_contain "Vendor:             example.com"
_must_not_contain "ERROR"
end_scenario; fi

if scenario "build-debug-flag-on"; then
_run env -i HOME="$HOME" PATH="$PATH" BUILD_DEBUG=true ./scripts/build.sh --dry-run >/dev/null
_must_contain "[debug]"
# Reliable [debug] line that's always present regardless of image.env:
# applied-default messages fire during defaults-and-normalise phase.
_must_contain "[debug] default applied:"
end_scenario; fi

# ════════════════════════════════════════════════════════════════════
# Dockerfile selection
# ════════════════════════════════════════════════════════════════════
# A consumer repo owns ./Dockerfile. This template ships none, so its
# own self-test falls back to Dockerfile.example. Both scenarios build
# in a scratch project root so the template's own tree is untouched,
# and both assert the DOCKERFILE= line the CI log reads.

if scenario "dockerfile-consumer-file-wins"; then
DF_ROOT="${TMP_DIR}/consumer-with-dockerfile"
mkdir -p "${DF_ROOT}"
cp "${SNAPSHOT}" "${DF_ROOT}/image.env"
printf 'FROM scratch\n' > "${DF_ROOT}/Dockerfile"
printf 'FROM scratch\n' > "${DF_ROOT}/Dockerfile.example"
_run env -i HOME="$HOME" PATH="$PATH" \
  ./scripts/build.sh --dry-run --project-root "${DF_ROOT}" >/dev/null
_must_contain "DOCKERFILE=Dockerfile"
_must_not_contain "DOCKERFILE=Dockerfile.example"
end_scenario; fi

if scenario "dockerfile-falls-back-to-example"; then
DF_ROOT="${TMP_DIR}/template-self-test"
mkdir -p "${DF_ROOT}"
cp "${SNAPSHOT}" "${DF_ROOT}/image.env"
printf 'FROM scratch\n' > "${DF_ROOT}/Dockerfile.example"
_run env -i HOME="$HOME" PATH="$PATH" \
  ./scripts/build.sh --dry-run --project-root "${DF_ROOT}" >/dev/null
_must_contain "DOCKERFILE=Dockerfile.example"
end_scenario; fi

if scenario "dockerfile-explicit-flag-wins"; then
DF_ROOT="${TMP_DIR}/consumer-named-dockerfile"
mkdir -p "${DF_ROOT}"
cp "${SNAPSHOT}" "${DF_ROOT}/image.env"
printf 'FROM scratch\n' > "${DF_ROOT}/Dockerfile"
printf 'FROM scratch\n' > "${DF_ROOT}/Dockerfile.slim"
_run env -i HOME="$HOME" PATH="$PATH" \
  ./scripts/build.sh --dry-run --project-root "${DF_ROOT}" \
  --dockerfile Dockerfile.slim >/dev/null
_must_contain "DOCKERFILE=Dockerfile.slim"
end_scenario; fi

# ════════════════════════════════════════════════════════════════════
# Cert sidecar behaviour
# ════════════════════════════════════════════════════════════════════
# The cert sidecar always runs, and the final stage always COPYs its
# trust files over the upstream's — an empty certs/ means no corp CA
# was added, not that the bundle survived. USER root from the sidecar
# never escapes because final re-bases FROM base.

if scenario "ca-cert-env-materialises-to-certs-dir"; then
# Setting CA_CERT in env writes certs/ci-injected.crt so the sidecar
# stage picks it up at build time.
CA_PEM="$(printf -- '-----BEGIN CERTIFICATE-----\nMIITest\n-----END CERTIFICATE-----\n')"
_run env -i HOME="$HOME" PATH="$PATH" CA_CERT="${CA_PEM}" ./scripts/build.sh --dry-run >/dev/null
[ -f certs/ci-injected.crt ] || FAILURES+=("${CURRENT_NAME}: certs/ci-injected.crt was NOT written")
rm -f certs/ci-injected.crt
_must_contain "→ Wrote CA_CERT to certs/ci-injected.crt"
end_scenario; fi

if scenario "no-ca-cert-empty-certs-dir-is-fine"; then
# When neither CA_CERT nor pre-existing certs/*.crt exist, build.sh
# logs a debug note and continues. The sidecar still rebuilds the
# bundle and the final stage still takes it — there is just no corp
# CA in it.
_run env -i HOME="$HOME" PATH="$PATH" BUILD_DEBUG=true ./scripts/build.sh --dry-run >/dev/null
_must_contain "no CA_CERT in env — using certs/ on disk as-is (bundle is still replaced)"
end_scenario; fi

if scenario "config-report-no-longer-shows-inject-certs-line"; then
# After the sidecar redesign, "Inject certs:" + "Original user:" are
# gone from the config report (no toggle; the user line reports the
# RESTORE_USER knob instead).
_run env -i HOME="$HOME" PATH="$PATH" ./scripts/build.sh --dry-run >/dev/null
_must_not_contain "Inject certs:"
_must_contain "Restore USER:       false"
end_scenario; fi

# ════════════════════════════════════════════════════════════════════
# APPEND_GIT_SHORT flag — tag-shape control
# ════════════════════════════════════════════════════════════════════

if scenario "append-git-short-default-true"; then
_run env -i HOME="$HOME" PATH="$PATH" ./scripts/build.sh --dry-run >/dev/null
# Default tag should have -<sha7> suffix on the configured upstream tag
_must_contain "1.25.3-alpine-"
end_scenario; fi

if scenario "append-git-short-false"; then
_run env -i HOME="$HOME" PATH="$PATH" APPEND_GIT_SHORT=false ./scripts/build.sh --dry-run >/dev/null
# Tag should end at the upstream tag with no SHA suffix, regardless
# of whether HARBOR_REGISTRY / HARBOR_PROJECT are set in image.env.
_must_contain "1.25.3-alpine"
_must_not_contain "1.25.3-alpine-"  # no SHA suffix
end_scenario; fi

if scenario "append-git-short-FALSE-uppercase"; then
_run env -i HOME="$HOME" PATH="$PATH" APPEND_GIT_SHORT=FALSE ./scripts/build.sh --dry-run >/dev/null
_must_not_contain "1.25.3-alpine-"
end_scenario; fi

if scenario "append-git-short-zero"; then
_run env -i HOME="$HOME" PATH="$PATH" APPEND_GIT_SHORT=0 ./scripts/build.sh --dry-run >/dev/null
_must_not_contain "1.25.3-alpine-"
end_scenario; fi

if scenario "append-git-short-no"; then
_run env -i HOME="$HOME" PATH="$PATH" APPEND_GIT_SHORT=no ./scripts/build.sh --dry-run >/dev/null
_must_not_contain "1.25.3-alpine-"
end_scenario; fi

# ════════════════════════════════════════════════════════════════════
# Required-field validation
# ════════════════════════════════════════════════════════════════════

if scenario "missing-upstream-identity-fails"; then
# Strip the UPSTREAM_REF line entirely — no identity at all → fail fast
# with a hint that points at UPSTREAM_REF (the canonical input).
sed -i.bak -E '/^UPSTREAM_REF=/d' image.env && rm image.env.bak
out=$(env -i HOME="$HOME" PATH="$PATH" ./scripts/build.sh --dry-run 2>&1) ; rc=$?
echo "${out}" > "${TMP_DIR}/out"
echo "${out}" | head -5
[ "${rc}" -ne 0 ] || FAILURES+=("${CURRENT_NAME}: expected non-zero exit")
_must_contain "UPSTREAM_REF"
end_scenario; fi

if scenario "upstream-ref-without-tag-fails"; then
# A URL with no :tag leaves UPSTREAM_TAG empty → fail with a tag hint.
out=$(env -i HOME="$HOME" PATH="$PATH" UPSTREAM_REF="nginx" ./scripts/build.sh --dry-run 2>&1) ; rc=$?
echo "${out}" > "${TMP_DIR}/out"
echo "${out}" | head -5
[ "${rc}" -ne 0 ] || FAILURES+=("${CURRENT_NAME}: expected non-zero exit")
_must_contain "UPSTREAM_TAG"
end_scenario; fi

# ════════════════════════════════════════════════════════════════════
# UPSTREAM_REF decomposition (single-URL form → registry / image / tag)
# ════════════════════════════════════════════════════════════════════
# build.sh splits one full image URL into the parts the build + the
# Dockerfile build-args consume. These pin the Docker reference-grammar
# edge cases. Assertions read the BUILD_DEBUG decomposition line (PHASE
# 1, before any network) plus the config banner. STUB_BIN neutralises
# crane/docker so digest resolution can't hang on unreachable hosts.

if scenario "upstream-ref-dockerhub-official"; then
out=$(env -i HOME="$HOME" PATH="${STUB_BIN}:$PATH" BUILD_DEBUG=true \
  UPSTREAM_REF="docker.io/library/nginx:1.25.3-alpine" \
  ./scripts/build.sh --dry-run 2>&1)
echo "${out}" > "${TMP_DIR}/out"; echo "${out}" | grep -E 'decomposed|Image:|Upstream:'
_must_contain "REGISTRY=docker.io IMAGE=library/nginx TAG=1.25.3-alpine"
_must_contain "Image:              nginx:1.25.3-alpine"
end_scenario; fi

if scenario "upstream-ref-shorthand-normalises-to-library"; then
# Bare "nginx:tag" → docker.io/library/nginx:tag (implicit host + namespace).
out=$(env -i HOME="$HOME" PATH="${STUB_BIN}:$PATH" BUILD_DEBUG=true \
  UPSTREAM_REF="nginx:1.25.3-alpine" \
  ./scripts/build.sh --dry-run 2>&1)
echo "${out}" > "${TMP_DIR}/out"; echo "${out}" | grep -E 'decomposed|Upstream:'
_must_contain "REGISTRY=docker.io IMAGE=library/nginx TAG=1.25.3-alpine"
_must_contain "Upstream:           docker.io/library/nginx:1.25.3-alpine"
end_scenario; fi

if scenario "upstream-ref-registry-with-port"; then
# A ":" before the last "/" is a PORT, not a tag.
out=$(env -i HOME="$HOME" PATH="${STUB_BIN}:$PATH" BUILD_DEBUG=true \
  UPSTREAM_REF="registry.example.com:5000/team/app:2.0" \
  ./scripts/build.sh --dry-run 2>&1)
echo "${out}" > "${TMP_DIR}/out"; echo "${out}" | grep -E 'decomposed|Image:|Upstream:'
_must_contain "REGISTRY=registry.example.com:5000 IMAGE=team/app TAG=2.0"
_must_contain "Image:              app:2.0"
end_scenario; fi

if scenario "upstream-ref-ghcr-namespaced"; then
# Non-Docker-Hub host with a single namespace segment — NOT library/-wrapped.
out=$(env -i HOME="$HOME" PATH="${STUB_BIN}:$PATH" BUILD_DEBUG=true \
  UPSTREAM_REF="ghcr.io/org/app:1.4.2" \
  ./scripts/build.sh --dry-run 2>&1)
echo "${out}" > "${TMP_DIR}/out"; echo "${out}" | grep -E 'decomposed|Image:|Upstream:'
_must_contain "REGISTRY=ghcr.io IMAGE=org/app TAG=1.4.2"
_must_contain "Image:              app:1.4.2"
end_scenario; fi

# ════════════════════════════════════════════════════════════════════
# Canonical immutable UPSTREAM_REF
# ════════════════════════════════════════════════════════════════════
# One identity — registry/image[:tag]@sha256:… — has to reach the
# Dockerfile's FROM, the crane digest lookup, the crane config USER
# probe and the OCI labels. A mismatch between any two of those means
# the image was built from bytes nothing else inspected.
#
# These scenarios run a FULL build (no --dry-run) against stubs that
# record what they were asked for: the crane stub answers digest/config
# and logs its target, the docker stub logs the whole builder argv and
# exits 0. Asserting the recorded argv is the only way to prove the
# build-args and the inspection target are the same string.
PIN_BASE_SHA="sha256:1111111111111111111111111111111111111111111111111111111111111111"
PIN_CERT_SHA="sha256:2222222222222222222222222222222222222222222222222222222222222222"
PSTUB="${TMP_DIR}/pin-stub"; mkdir -p "${PSTUB}"
# Unquoted heredoc so the digests above are baked in; \$ keeps the
# stub's own shell vars. The library/alpine arm is the cert builder, so
# base and cert builder get distinguishable digests and an identity
# assertion can't pass by accident. Matching the REPO, not just
# "alpine" — the nginx base's own tag is 1.25.3-alpine.
cat > "${PSTUB}/crane" <<STUB
#!/bin/sh
printf 'CRANE %s %s\n' "\$1" "\$2" >> "\${PIN_LOG:-/dev/null}"
case "\$1" in
  digest)
    case "\$2" in
      */library/alpine:*) echo "${PIN_CERT_SHA}" ;;
      *)        echo "${PIN_BASE_SHA}" ;;
    esac ;;
  config)
    # STUB_USER picks the answer: unset = "nginx", empty = a config
    # with no user, FAIL = an inspection that failed.
    case "\${STUB_USER-nginx}" in
      FAIL) exit 1 ;;
      *) printf '{"config":{"User":"%s"}}\n' "\${STUB_USER-nginx}" ;;
    esac ;;
  *) exit 1 ;;
esac
STUB
cp "${DOCKER_STUB}" "${PSTUB}/docker"
chmod +x "${PSTUB}/crane" "${PSTUB}/docker"

# Run a full build against a throwaway project root holding just the
# scenario's UPSTREAM_REF, then fold the stub log into the assertion
# buffer so _must_contain can see both the script's output and the
# recorded argv. $1 = UPSTREAM_REF, rest = extra KEY=VAL env.
_pin_build() {
  local ref="$1"; shift
  local proj="${TMP_DIR}/pin-proj"
  rm -rf "${proj}"; mkdir -p "${proj}"
  printf 'UPSTREAM_REF="%s"\nVENDOR="example.com"\n' "${ref}" > "${proj}/image.env"
  : > "${TMP_DIR}/pin.log"
  out=$(env -i HOME="$HOME" PATH="${PSTUB}:$PATH" PIN_LOG="${TMP_DIR}/pin.log" \
    BUILD_DEBUG=true "$@" ./scripts/build.sh --project-root "${proj}" 2>&1) ; rc=$?
  { printf '%s\n' "${out}"; cat "${TMP_DIR}/pin.log"; } > "${TMP_DIR}/out"
  [ "${rc}" -eq 0 ] || FAILURES+=("${CURRENT_NAME}: build should succeed against stubs, got ${rc}")
  grep -E 'pinned|CRANE|UPSTREAM_REF=' "${TMP_DIR}/out" | head -8
}

if scenario "upstream-ref-tag-plus-digest-preserved"; then
# The supplied digest IS the pin. It must survive into FROM, the USER
# probe and the labels, and must NOT trigger a second digest lookup —
# re-resolving the tag could hand back a different image.
_pin_build "docker.io/library/nginx:1.25.3-alpine@${PIN_BASE_SHA}" RESTORE_USER=true
_must_contain "Upstream already pinned: docker.io/library/nginx:1.25.3-alpine@${PIN_BASE_SHA}"
_must_contain "--build-arg UPSTREAM_REF=docker.io/library/nginx:1.25.3-alpine@${PIN_BASE_SHA}"
_must_contain "--label org.opencontainers.image.base.name=docker.io/library/nginx:1.25.3-alpine@${PIN_BASE_SHA}"
_must_contain "--label org.opencontainers.image.base.digest=${PIN_BASE_SHA}"
_must_contain "CRANE config docker.io/library/nginx:1.25.3-alpine@${PIN_BASE_SHA}"
_must_not_contain "CRANE digest docker.io/library/nginx"
# Cert builder gets the same treatment — its trust store ships in the
# final image, so an unpinned sidecar base is the same gap.
_must_contain "--build-arg CERT_BUILDER_IMAGE=docker.io/library/alpine:3.20@${PIN_CERT_SHA}"
end_scenario; fi

if scenario "upstream-ref-digest-only-keeps-digest-and-derives-version"; then
# No tag to inherit. The digest stays the identity and the image tag
# gets a short form of it — "latest" would name a different image.
_pin_build "docker.io/library/nginx@${PIN_BASE_SHA}" RESTORE_USER=true
_must_contain "--build-arg UPSTREAM_REF=docker.io/library/nginx@${PIN_BASE_SHA}"
_must_contain "CRANE config docker.io/library/nginx@${PIN_BASE_SHA}"
_must_contain "Image:              nginx:sha256-111111111111"
_must_not_contain "nginx:latest"
end_scenario; fi

if scenario "upstream-ref-registry-port-with-digest-preserved"; then
# A ":" before the last "/" is a PORT. Decomposition must keep it out
# of the tag and recomposition must put it back unchanged.
_pin_build "registry.example.com:5000/team/app:2.0@${PIN_BASE_SHA}" RESTORE_USER=true
_must_contain "REGISTRY=registry.example.com:5000 IMAGE=team/app TAG=2.0 DIGEST=${PIN_BASE_SHA}"
_must_contain "--build-arg UPSTREAM_REF=registry.example.com:5000/team/app:2.0@${PIN_BASE_SHA}"
_must_contain "CRANE config registry.example.com:5000/team/app:2.0@${PIN_BASE_SHA}"
_must_contain "Image:              app:2.0-"
end_scenario; fi

if scenario "upstream-ref-ordinary-tag-resolved-once-then-pinned"; then
# The tag-only case: resolve ONCE up front, then every later consumer
# sees the resolved ref. Two lookups of a moving tag can disagree.
_pin_build "docker.io/library/nginx:1.25.3-alpine" RESTORE_USER=true
_must_contain "CRANE digest docker.io/library/nginx:1.25.3-alpine"
_must_contain "pinned upstream: docker.io/library/nginx:1.25.3-alpine@${PIN_BASE_SHA}"
_must_contain "--build-arg UPSTREAM_REF=docker.io/library/nginx:1.25.3-alpine@${PIN_BASE_SHA}"
_must_contain "CRANE config docker.io/library/nginx:1.25.3-alpine@${PIN_BASE_SHA}"
_must_contain "--label org.opencontainers.image.base.digest=${PIN_BASE_SHA}"
n=$(grep -c 'CRANE digest docker.io/library/nginx' "${TMP_DIR}/out")
[ "${n}" -eq 1 ] || FAILURES+=("${CURRENT_NAME}: base digest resolved ${n} times, expected exactly 1")
end_scenario; fi

if scenario "upstream-ref-malformed-digest-rejected"; then
# A truncated or non-hex pin is a config bug. Catch it before the
# build, not as a confusing pull error halfway through.
for bad in "sha256:nothex" "sha256:1111" "sha256:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"; do
  out=$(env -i HOME="$HOME" PATH="${STUB_BIN}:$PATH" \
    UPSTREAM_REF="docker.io/library/nginx:1.25.3-alpine@${bad}" \
    ./scripts/build.sh --dry-run 2>&1) ; rc=$?
  echo "${out}" > "${TMP_DIR}/out"
  [ "${rc}" -ne 0 ] || FAILURES+=("${CURRENT_NAME}: expected non-zero exit for @${bad}")
  _must_contain "malformed digest"
done
end_scenario; fi

if scenario "upstream-ref-conflicting-segment-override-rejected"; then
# UPSTREAM_REF and a segment var naming two different bases is two
# answers to one question. Fail rather than silently pick one.
out=$(env -i HOME="$HOME" PATH="${STUB_BIN}:$PATH" \
  UPSTREAM_REF="docker.io/library/nginx:1.25.3-alpine" UPSTREAM_TAG="1.27.0" \
  ./scripts/build.sh --dry-run 2>&1) ; rc=$?
echo "${out}" > "${TMP_DIR}/out"; echo "${out}" | grep -i conflict
[ "${rc}" -ne 0 ] || FAILURES+=("${CURRENT_NAME}: expected non-zero exit on conflicting UPSTREAM_TAG")
_must_contain "UPSTREAM_TAG='1.27.0' conflicts with UPSTREAM_REF"
out=$(env -i HOME="$HOME" PATH="${STUB_BIN}:$PATH" \
  UPSTREAM_REF="docker.io/library/nginx:1.25.3-alpine" UPSTREAM_REGISTRY="mirror.example.com" \
  ./scripts/build.sh --dry-run 2>&1) ; rc=$?
echo "${out}" > "${TMP_DIR}/out"
[ "${rc}" -ne 0 ] || FAILURES+=("${CURRENT_NAME}: expected non-zero exit on conflicting UPSTREAM_REGISTRY")
_must_contain "UPSTREAM_REGISTRY='mirror.example.com' conflicts with UPSTREAM_REF"
end_scenario; fi

if scenario "upstream-digest-resolution-failure-fails-the-build"; then
# Fail-closed. With crane and docker both unable to resolve, a real
# build must stop — proceeding would build from whatever the tag points
# at now, which is not what the labels and the SBOM would claim.
_PROJ="${TMP_DIR}/pin-fail"; rm -rf "${_PROJ}"; mkdir -p "${_PROJ}"
cp "${SNAPSHOT}" "${_PROJ}/image.env"
out=$(env -i HOME="$HOME" PATH="${STUB_BIN}:$PATH" \
  ./scripts/build.sh --project-root "${_PROJ}" 2>&1) ; rc=$?
echo "${out}" > "${TMP_DIR}/out"; echo "${out}" | tail -6
[ "${rc}" -ne 0 ] || FAILURES+=("${CURRENT_NAME}: expected non-zero exit when the digest cannot be resolved")
_must_contain "could not pin an immutable digest"
_must_not_contain "build complete"
rm -rf "${_PROJ}"
end_scenario; fi

if scenario "upstream-digest-resolution-failure-only-warns-on-dry-run"; then
# --dry-run resolves config and produces no image, so an unreachable
# registry there is a warning. Without this split, every offline
# config check would be impossible.
out=$(env -i HOME="$HOME" PATH="${STUB_BIN}:$PATH" ./scripts/build.sh --dry-run 2>&1) ; rc=$?
echo "${out}" > "${TMP_DIR}/out"
[ "${rc}" -eq 0 ] || FAILURES+=("${CURRENT_NAME}: --dry-run should still exit 0, got ${rc}")
_must_contain "continuing UNPINNED"
_must_contain "--dry-run: stopping before docker build"
end_scenario; fi

# ════════════════════════════════════════════════════════════════════
# Runtime user resolution (RESTORE_USER)
# ════════════════════════════════════════════════════════════════════
# The template emits no USER line of its own: the final stage re-bases
# FROM base, so the image keeps the upstream's user. Only a fork whose
# edit region enters root needs one back, and RESTORE_USER=true is how
# it says so. These reuse the pin stubs above and run a FULL build,
# because whether ORIGINAL_USER reaches `docker build` is the point —
# a dry-run would prove nothing about the argv.

if scenario "no-restore-user-inherits-base-user"; then
# The certificate-only recipe. Nothing is inspected and no
# ORIGINAL_USER build-arg is passed, so a non-root base stays non-root
# without the build having to know what its user is.
_pin_build "docker.io/library/nginx:1.25.3-alpine@${PIN_BASE_SHA}"
_must_contain "RESTORE_USER is not true → final image inherits the base image's USER"
_must_not_contain "--build-arg ORIGINAL_USER"
_must_not_contain "CRANE config"
end_scenario; fi

if scenario "restore-user-explicit-value-passed-verbatim"; then
# An explicit user skips the probe and must survive byte-for-byte:
# "1000:1000" and "app:app" mean different things to Docker, and a
# numeric pair mangled into a name is a container that won't start.
for _u in "1000:1000" "app:app" "postgres"; do
  _pin_build "docker.io/library/nginx:1.25.3-alpine@${PIN_BASE_SHA}" RESTORE_USER=true ORIGINAL_USER="${_u}"
  _must_contain "ORIGINAL_USER explicitly set: ${_u} (skipping auto-detect)"
  _must_contain "--build-arg ORIGINAL_USER=${_u}"
  _must_not_contain "CRANE config"
done
end_scenario; fi

if scenario "restore-user-empty-upstream-user-is-explicit-root"; then
# A config that declares no user is a real answer, not a failure: the
# upstream runs as root, so root is what we restore — and the build
# says which of the two cases it took.
_pin_build "docker.io/library/nginx:1.25.3-alpine@${PIN_BASE_SHA}" RESTORE_USER=true STUB_USER=""
_must_contain "upstream config declares no USER — it runs as root → ORIGINAL_USER=root"
_must_contain "--build-arg ORIGINAL_USER=root"
end_scenario; fi

if scenario "restore-user-inspection-failure-fails-the-build"; then
# The regression this knob exists for. The build is about to enter
# root; if the user to come back to cannot be read, falling back to
# root would ship a root image nobody asked for. Stop instead.
_UPROJ="${TMP_DIR}/user-proj"; rm -rf "${_UPROJ}"; mkdir -p "${_UPROJ}"
printf 'UPSTREAM_REF="docker.io/library/nginx:1.25.3-alpine@%s"\nVENDOR="example.com"\n' \
  "${PIN_BASE_SHA}" > "${_UPROJ}/image.env"
: > "${TMP_DIR}/pin.log"
out=$(env -i HOME="$HOME" PATH="${PSTUB}:$PATH" PIN_LOG="${TMP_DIR}/pin.log" \
  RESTORE_USER=true STUB_USER=FAIL \
  ./scripts/build.sh --project-root "${_UPROJ}" 2>&1) ; rc=$?
{ printf '%s\n' "${out}"; cat "${TMP_DIR}/pin.log"; } > "${TMP_DIR}/out"
printf '%s\n' "${out}" | tail -5
[ "${rc}" -ne 0 ] || FAILURES+=("${CURRENT_NAME}: expected non-zero exit when the USER probe fails")
_must_contain "could not inspect docker.io/library/nginx:1.25.3-alpine@${PIN_BASE_SHA}"
_must_not_contain "ORIGINAL_USER=root"
_must_not_contain "--build-arg"     # no docker build was reached at all

# --dry-run produces no image, so there the same failure is a warning.
# Without the split, an offline config check would be impossible.
out=$(env -i HOME="$HOME" PATH="${PSTUB}:$PATH" RESTORE_USER=true STUB_USER=FAIL \
  ./scripts/build.sh --project-root "${_UPROJ}" --dry-run 2>&1) ; rc=$?
printf '%s\n' "${out}" > "${TMP_DIR}/out"
[ "${rc}" -eq 0 ] || FAILURES+=("${CURRENT_NAME}: --dry-run should still exit 0, got ${rc}")
_must_contain "continuing UNRESOLVED"
end_scenario; fi

if scenario "no-restore-user-builds-without-crane"; then
# crane absent entirely, not just failing. With the base already
# pinned nothing needs a digest either, so the certificate-only recipe
# builds on a machine that has no crane at all. The curl stub is there
# so build.sh's crane auto-install cannot reach the network.
_NOCRANE="${TMP_DIR}/nocrane"; mkdir -p "${_NOCRANE}"
cp "${PSTUB}/docker" "${_NOCRANE}/docker"
printf '#!/bin/sh\nexit 1\n' > "${_NOCRANE}/curl"
chmod +x "${_NOCRANE}/curl"
_UPROJ2="${TMP_DIR}/user-proj-nocrane"; mkdir -p "${_UPROJ2}"
printf 'UPSTREAM_REF="docker.io/library/nginx:1.25.3-alpine@%s"\nVENDOR="example.com"\n' \
  "${PIN_BASE_SHA}" > "${_UPROJ2}/image.env"
: > "${TMP_DIR}/pin.log"
out=$(env -i HOME="$HOME" PATH="${_NOCRANE}:/usr/bin:/bin" PIN_LOG="${TMP_DIR}/pin.log" \
  CERT_BUILDER_IMAGE="docker.io/library/alpine:3.20@${PIN_CERT_SHA}" \
  ./scripts/build.sh --project-root "${_UPROJ2}" 2>&1) ; rc=$?
{ printf '%s\n' "${out}"; cat "${TMP_DIR}/pin.log"; } > "${TMP_DIR}/out"
[ "${rc}" -eq 0 ] || { printf '%s\n' "${out}" | tail -8; FAILURES+=("${CURRENT_NAME}: build should succeed without crane, got ${rc}"); }
_must_contain "final image inherits the base image's USER"
_must_contain "--build-arg UPSTREAM_REF=docker.io/library/nginx:1.25.3-alpine@${PIN_BASE_SHA}"
_must_not_contain "--build-arg ORIGINAL_USER"
end_scenario; fi

if scenario "renovate-extracts-repo-and-tag-from-a-pinned-ref"; then
# The pinned form is now what image.env carries, so Renovate's custom
# manager has to still see a depName and a currentValue in it — a
# regex that only matched the bare tag would silently stop tracking
# upstream releases the day someone pins. Runs renovate.json's OWN
# matchStrings rather than a hand-copied one; the only rewrite is JS
# (?<name>) → Python (?P<name>) named-group syntax.
out=$(python3 - <<'PY' 2>&1
import json, re, sys
rx = json.load(open("renovate.json"))["customManagers"][0]["matchStrings"][0]
pat = re.compile(rx.replace("(?<", "(?P<"))
cases = [
    ('UPSTREAM_REF="docker.io/library/nginx:1.25.3-alpine@sha256:%s"' % ("a" * 64),
     "docker.io/library/nginx", "1.25.3-alpine", "sha256:" + "a" * 64),
    ('UPSTREAM_REF="registry.example.com:5000/team/app:2.0@sha256:%s"' % ("b" * 64),
     "registry.example.com:5000/team/app", "2.0", "sha256:" + "b" * 64),
    ('UPSTREAM_REF="docker.io/library/nginx:1.25.3-alpine"',
     "docker.io/library/nginx", "1.25.3-alpine", None),
]
for line, dep, val, dig in cases:
    m = pat.search(line)
    if not m:
        print("NO MATCH: %s" % line); sys.exit(1)
    g = m.groupdict()
    if (g["depName"], g["currentValue"], g["currentDigest"]) != (dep, val, dig):
        print("WRONG: %s -> %s" % (line, g)); sys.exit(1)
print("renovate customManager extracts depName + currentValue + currentDigest")
PY
) ; rc=$?
echo "${out}" > "${TMP_DIR}/out"; echo "${out}"
[ "${rc}" -eq 0 ] || FAILURES+=("${CURRENT_NAME}: renovate.json regex check failed (rc=${rc})")
_must_contain "renovate customManager extracts depName + currentValue + currentDigest"
end_scenario; fi

if scenario "legacy-three-var-form-still-works"; then
# No UPSTREAM_REF at all — the pre-split three-var form keeps working.
sed -i.bak -E '/^UPSTREAM_REF=/d' image.env && rm image.env.bak
out=$(env -i HOME="$HOME" PATH="${STUB_BIN}:$PATH" \
  UPSTREAM_REGISTRY="docker.io/library" UPSTREAM_IMAGE="nginx" UPSTREAM_TAG="1.25.3-alpine" \
  ./scripts/build.sh --dry-run 2>&1)
echo "${out}" > "${TMP_DIR}/out"; echo "${out}" | grep -E 'Image:|Upstream:'
_must_contain "Upstream:           docker.io/library/nginx:1.25.3-alpine"
_must_contain "Image:              nginx:1.25.3-alpine"
end_scenario; fi

if scenario "harbor-push-without-required-vars-fails"; then
# REGISTRY_KIND defaults to harbor. With HARBOR_* unset, harbor.sh's
# own _harbor_require_env should fail loudly. build.sh itself no
# longer validates backend-specific vars — each backend owns that.
sed -i.bak -E '/^(HARBOR_REGISTRY|HARBOR_PROJECT|REGISTRY_KIND)=/d' image.env && rm image.env.bak
out=$(env -i HOME="$HOME" PATH="$PATH" ./scripts/build.sh --push 2>&1) ; rc=$?
echo "${out}" > "${TMP_DIR}/out"
echo "${out}" | tail -10
[ "${rc}" -ne 0 ] || FAILURES+=("${CURRENT_NAME}: expected non-zero exit on --push without HARBOR_*")
_must_contain "HARBOR_REGISTRY is required for the Harbor backend"
_must_contain "HARBOR_PROJECT is required for the Harbor backend"
end_scenario; fi

if scenario "argv-extra-args-rejected"; then
# build.sh takes no positionals — every unmatched token is reported by
# the arg parser as an unknown flag (and exits non-zero with usage).
out=$(env -i HOME="$HOME" PATH="$PATH" ./scripts/build.sh --push extra 2>&1) ; rc=$?
echo "${out}" > "${TMP_DIR}/out"
[ "${rc}" -ne 0 ] || FAILURES+=("${CURRENT_NAME}: expected non-zero exit on extra args")
_must_contain "unknown flag 'extra'"
end_scenario; fi

if scenario "argv-unknown-flag-rejected"; then
out=$(env -i HOME="$HOME" PATH="$PATH" ./scripts/build.sh --bogus 2>&1) ; rc=$?
echo "${out}" > "${TMP_DIR}/out"
[ "${rc}" -ne 0 ] || FAILURES+=("${CURRENT_NAME}: expected non-zero exit on unknown flag")
_must_contain "unknown flag"
end_scenario; fi

if scenario "help-flag"; then
out=$(env -i HOME="$HOME" PATH="$PATH" ./scripts/build.sh --help 2>&1) ; rc=$?
echo "${out}" > "${TMP_DIR}/out"
[ "${rc}" -eq 0 ] || FAILURES+=("${CURRENT_NAME}: --help should exit 0, got ${rc}")
_must_contain "Usage:"
_must_contain "--push"
_must_contain "--dry-run"
end_scenario; fi

# ════════════════════════════════════════════════════════════════════
# Backend selector
# ════════════════════════════════════════════════════════════════════

if scenario "registry-kind-artifactory_jcr-needs-creds-on-push"; then
# With REGISTRY_KIND=artifactory_jcr and no ARTIFACTORY_* config,
# artifactory_jcr.sh's _artifactory_jcr_require_env should fail loudly.
# build.sh doesn't cross-derive Harbor vars, so the error must
# be Artifactory-specific.
sed -i.bak -E '/^(HARBOR_REGISTRY|HARBOR_PROJECT|ARTIFACTORY_URL|ARTIFACTORY_USER|ARTIFACTORY_PUSH_HOST|ARTIFACTORY_TEAM|REGISTRY_KIND)=/d' image.env && rm image.env.bak
echo 'REGISTRY_KIND="artifactory_jcr"' >> image.env
out=$(env -i HOME="$HOME" PATH="$PATH" ./scripts/build.sh --push 2>&1) ; rc=$?
echo "${out}" > "${TMP_DIR}/out"
echo "${out}" | tail -10
[ "${rc}" -ne 0 ] || FAILURES+=("${CURRENT_NAME}: expected non-zero exit")
# Artifactory backend's own require_env names ARTIFACTORY_* vars (NOT HARBOR_*)
_must_contain "ARTIFACTORY_URL is required"
_must_contain "ARTIFACTORY_USER is required"
_must_contain "ARTIFACTORY_TEAM is required"
# Confirm there's no leakage of HARBOR_* error text — backends are independent
_must_not_contain "HARBOR_REGISTRY"
end_scenario; fi

if scenario "registry-kind-artifactory_pro-needs-creds-on-push"; then
# Same require_env contract as the JCR variant, but the error message
# names the Pro backend so users grep'ing logs find the right file.
sed -i.bak -E '/^(HARBOR_REGISTRY|HARBOR_PROJECT|ARTIFACTORY_URL|ARTIFACTORY_USER|ARTIFACTORY_PUSH_HOST|ARTIFACTORY_TEAM|REGISTRY_KIND)=/d' image.env && rm image.env.bak
echo 'REGISTRY_KIND="artifactory_pro"' >> image.env
out=$(env -i HOME="$HOME" PATH="$PATH" ./scripts/build.sh --push 2>&1) ; rc=$?
echo "${out}" > "${TMP_DIR}/out"
echo "${out}" | tail -10
[ "${rc}" -ne 0 ] || FAILURES+=("${CURRENT_NAME}: expected non-zero exit")
_must_contain "ARTIFACTORY_URL is required"
_must_contain "REGISTRY_KIND=artifactory_pro"
_must_not_contain "HARBOR_REGISTRY"
end_scenario; fi

if scenario "build-uses-simple-local-tag"; then
# build.sh should produce a registry-less local tag like
#   nginx:1.25.3-alpine-<sha>
# regardless of which backend is selected. Each backend retags to its
# own URL during push. Verifies build.sh doesn't compose
# backend-specific tags.
_run env -i HOME="$HOME" PATH="$PATH" REGISTRY_KIND=artifactory_jcr ./scripts/build.sh --dry-run >/dev/null
_must_contain "Image:              nginx:1.25.3-alpine-"
_must_not_contain "Image:              harbor"      # no Harbor host prefix leakage
_must_not_contain "Image:              artifactory" # no Artifactory host prefix leakage
end_scenario; fi

if scenario "harbor-and-artifactory-vars-independent"; then
# Setting HARBOR_* with REGISTRY_KIND=artifactory_jcr must NOT influence
# Artifactory behaviour, and vice-versa. build.sh must not blend the
# two namespaces.
_run env -i HOME="$HOME" PATH="$PATH" \
  REGISTRY_KIND=artifactory_jcr \
  HARBOR_REGISTRY="should-not-leak.example.com" \
  HARBOR_PROJECT="should-not-leak" \
  ./scripts/build.sh --dry-run >/dev/null
# FULL_IMAGE must remain the simple local tag — HARBOR_* MUST NOT
# appear in build.sh's resolved-config block.
_must_contain "Image:              nginx:1.25.3-alpine-"
_must_not_contain "should-not-leak"
end_scenario; fi

if scenario "pro-only-deployment-can-delete-jcr-files"; then
# Deletion safety: a Pro-only deployment (no JCR backend, no python
# build-info merger) should still work. Temporarily rename the JCR
# backend + merger out of the tree, run a dry-run against the Pro
# backend, restore. The dry-run stops before docker build so this is
# fast (~1s) and doesn't require any actual auth.
mv scripts/push-backends/artifactory_jcr.sh /tmp/_aj.sh.bak
mv scripts/lib/build-info-merge.py        /tmp/_bim.py.bak
_run env -i HOME="$HOME" PATH="$PATH" \
  REGISTRY_KIND=artifactory_pro \
  ARTIFACTORY_URL=https://art.example.com \
  ARTIFACTORY_USER=svc \
  ARTIFACTORY_TOKEN=tok \
  ARTIFACTORY_TEAM=platform \
  ./scripts/build.sh --dry-run >/dev/null
mv /tmp/_aj.sh.bak  scripts/push-backends/artifactory_jcr.sh
mv /tmp/_bim.py.bak scripts/lib/build-info-merge.py
_must_contain "--dry-run: stopping before docker build"
end_scenario; fi

# ════════════════════════════════════════════════════════════════════
# Scan scripts — graceful no-op when creds missing
# ════════════════════════════════════════════════════════════════════

if scenario "xray-vuln-no-creds-noop"; then
_run env -i HOME="$HOME" PATH="$PATH" ./scripts/scan/xray-vuln.sh 2>/dev/null || true
_must_contain "Xray-side Artifactory creds unset — no-op"
end_scenario; fi

if scenario "xray-sbom-no-creds-noop"; then
_run env -i HOME="$HOME" PATH="$PATH" ./scripts/scan/xray-sbom.sh 2>/dev/null || true
_must_contain "Xray-side Artifactory creds unset — no-op"
end_scenario; fi

if scenario "xray-sbom-opt-out-via-flag"; then
_run env -i HOME="$HOME" PATH="$PATH" XRAY_GENERATE_SBOM=false ./scripts/scan/xray-sbom.sh 2>/dev/null || true
_must_contain "XRAY_GENERATE_SBOM=false — skipping"
end_scenario; fi

if scenario "xray-vuln-target-resolution-image-digest-wins"; then
# When IMAGE_DIGEST is set (post-build), it should be the scan target
# (NOT UPSTREAM_REF, even though image.env defines that too).
_run env -i HOME="$HOME" PATH="$PATH" \
  IMAGE_TRANSPORT=registry \
  IMAGE_REF="harbor.example.com/team/img@${_D1}" \
  IMAGE_DIGEST="${_D1}" \
  ./scripts/scan/xray-vuln.sh 2>/dev/null || true
_must_contain "→ Scan target: harbor.example.com/team/img@${_D1}"
_must_not_contain "→ Scan target: docker.io/library/nginx:1.25.3-alpine"
end_scenario; fi

if scenario "xray-vuln-target-resolution-positional-arg-wins"; then
# Positional arg beats every env / image.env value.
_run env -i HOME="$HOME" PATH="$PATH" \
  IMAGE_TRANSPORT=registry IMAGE_REF="should-be-ignored@${_D1}" IMAGE_DIGEST="${_D1}" \
  ./scripts/scan/xray-vuln.sh "explicit:override" 2>/dev/null || true
_must_contain "→ Scan target: explicit:override"
end_scenario; fi

if scenario "xray-vuln-target-resolution-xray-scan-ref-wins-over-upstream"; then
# XRAY_SCAN_REF beats UPSTREAM_REF (prescan use case).
_run env -i HOME="$HOME" PATH="$PATH" \
  XRAY_SCAN_REF="prescan:target" \
  ./scripts/scan/xray-vuln.sh 2>/dev/null || true
_must_contain "→ Scan target: prescan:target"
end_scenario; fi

if scenario "xray-vuln-target-resolution-fallback-to-upstream"; then
# When neither IMAGE_DIGEST nor XRAY_SCAN_REF nor positional arg are
# set, fall back to UPSTREAM_REF. Set UPSTREAM_REF explicitly so the
# assertion is independent of whatever the live image.env points at.
_run env -i HOME="$HOME" PATH="$PATH" \
  UPSTREAM_REF="docker.io/library/nginx:1.25.3-alpine" \
  ./scripts/scan/xray-vuln.sh 2>/dev/null || true
_must_contain "→ Scan target: docker.io/library/nginx:1.25.3-alpine"
end_scenario; fi

if scenario "xray-vuln-fails-loudly-when-pull-fails"; then
# Guards the silent-fail mode: if docker pull fails (e.g. unauthorized
# to a private registry), the script must exit non-zero so CI marks the
# job failed — not WARN and exit 0, which would show the job green with
# no scan having run.
# We trigger this by passing a definitely-unreachable digest with
# Xray creds present (so Phase 1 passes through) and no docker login.
out=$(env -i HOME="$HOME" PATH="$PATH" \
  ARTIFACTORY_URL="https://example.invalid" \
  ARTIFACTORY_USER="x" \
  ARTIFACTORY_TOKEN="x" \
  ./scripts/scan/xray-vuln.sh "harbor.invalid/nope/nope@sha256:0000000000000000000000000000000000000000000000000000000000000000" 2>&1) ; rc=$?
echo "${out}" > "${TMP_DIR}/out"
[ "${rc}" -ne 0 ] || FAILURES+=("${CURRENT_NAME}: expected non-zero exit on docker pull / scan failure")
# The stubbed jf carries the script past config/install, so the pull is
# the failure point the scenario name promises. Assert it, or a future
# change that bails earlier would keep this green without ever reaching
# the pull.
_must_contain "docker pull failed"
end_scenario; fi

# ── Xray digest-ref re-tag (RepoTags:null → Xray under-indexes) ────
# `jf docker scan` indexes by `docker save <ref>`; a digest-only ref
# saves with RepoTags:null and Xray emits a tiny SBOM/vuln set. The
# xray scripts must re-tag the pulled image to a local alias and scan
# THAT. Stub docker+jf so we drive the full path (creds → pull →
# re-tag → scan) with no real registry/Xray. The docker stub records
# every `docker tag` so we can prove the alias was created.
XSTUB="${TMP_DIR}/xray-stub"; mkdir -p "${XSTUB}"
cat > "${XSTUB}/docker" <<'STUB'
#!/bin/sh
[ "$1" = "tag" ] && printf 'TAG %s %s\n' "$2" "$3" >> "${XRAY_TAG_LOG:-/dev/null}"
exit 0
STUB
cat > "${XSTUB}/jf" <<'STUB'
#!/bin/sh
[ "$1" = "docker" ] && [ "$2" = "scan" ] && printf '{"bomFormat":"CycloneDX","components":[],"vulnerabilities":[]}\n'
exit 0
STUB
chmod +x "${XSTUB}/docker" "${XSTUB}/jf"

if scenario "xray-vuln-digest-ref-retagged-to-image-ref"; then
: > "${TMP_DIR}/tag.log"
out=$(env -i HOME="$HOME" PATH="${XSTUB}:$PATH" \
  XRAY_TAG_LOG="${TMP_DIR}/tag.log" \
  ARTIFACTORY_URL="https://art.example.com" ARTIFACTORY_USER="u" ARTIFACTORY_TOKEN="t" \
  VULN_INLINE_POST=false \
  IMAGE_TRANSPORT=registry \
  IMAGE_DIGEST="${_D1}" \
  IMAGE_REF="harbor.example.com/team/img@${_D1}" \
  IMAGE_TAG_REF="harbor.example.com/team/img:1.2.3-deadbee" \
  ./scripts/scan/xray-vuln.sh 2>&1); echo "${out}" > "${TMP_DIR}/out"
_must_contain "scanning via local tag alias harbor.example.com/team/img:1.2.3-deadbee"
grep -qF "TAG harbor.example.com/team/img@${_D1} harbor.example.com/team/img:1.2.3-deadbee" "${TMP_DIR}/tag.log" \
  || FAILURES+=("${CURRENT_NAME}: docker tag <digest> <IMAGE_TAG_REF> was not invoked")
grep -Eq 'jf docker scan.*1\.2\.3-deadbee' "${TMP_DIR}/out" \
  || FAILURES+=("${CURRENT_NAME}: jf docker scan did not target the alias")
end_scenario; fi

if scenario "xray-vuln-digest-ref-without-image-ref-synthesizes-alias"; then
: > "${TMP_DIR}/tag.log"
out=$(env -i HOME="$HOME" PATH="${XSTUB}:$PATH" \
  XRAY_TAG_LOG="${TMP_DIR}/tag.log" \
  ARTIFACTORY_URL="https://art.example.com" ARTIFACTORY_USER="u" ARTIFACTORY_TOKEN="t" \
  VULN_INLINE_POST=false \
  IMAGE_TRANSPORT=registry \
  IMAGE_DIGEST="${_D1}" \
  IMAGE_REF="harbor.example.com/team/img@${_D1}" \
  ./scripts/scan/xray-vuln.sh 2>&1); echo "${out}" > "${TMP_DIR}/out"
# No IMAGE_TAG_REF → alias synthesised from the bare repo + :xray-scan tag.
_must_contain "scanning via local tag alias harbor.example.com/team/img:xray-scan"
end_scenario; fi

if scenario "xray-vuln-tag-ref-not-retagged"; then
: > "${TMP_DIR}/tag.log"
out=$(env -i HOME="$HOME" PATH="${XSTUB}:$PATH" \
  XRAY_TAG_LOG="${TMP_DIR}/tag.log" \
  ARTIFACTORY_URL="https://art.example.com" ARTIFACTORY_USER="u" ARTIFACTORY_TOKEN="t" \
  VULN_INLINE_POST=false \
  XRAY_SCAN_REF="harbor.example.com/team/img:1.2.3-deadbee" \
  ./scripts/scan/xray-vuln.sh 2>&1); echo "${out}" > "${TMP_DIR}/out"
# Tag refs already carry RepoTags — must pass through unchanged.
_must_not_contain "scanning via local tag alias"
[ -s "${TMP_DIR}/tag.log" ] && FAILURES+=("${CURRENT_NAME}: tag ref must NOT be re-tagged") || true
end_scenario; fi

if scenario "xray-sbom-digest-ref-retagged-to-image-ref"; then
: > "${TMP_DIR}/tag.log"
out=$(env -i HOME="$HOME" PATH="${XSTUB}:$PATH" \
  XRAY_TAG_LOG="${TMP_DIR}/tag.log" \
  ARTIFACTORY_URL="https://art.example.com" ARTIFACTORY_USER="u" ARTIFACTORY_TOKEN="t" \
  SBOM_INLINE_POST=false \
  IMAGE_TRANSPORT=registry \
  IMAGE_DIGEST="${_D1}" \
  IMAGE_REF="harbor.example.com/team/img@${_D1}" \
  IMAGE_TAG_REF="harbor.example.com/team/img:1.2.3-deadbee" \
  ./scripts/scan/xray-sbom.sh 2>&1); echo "${out}" > "${TMP_DIR}/out"
_must_contain "scanning via local tag alias harbor.example.com/team/img:1.2.3-deadbee"
grep -Eq 'jf docker scan.*1\.2\.3-deadbee' "${TMP_DIR}/out" \
  || FAILURES+=("${CURRENT_NAME}: jf docker scan did not target the alias")
end_scenario; fi

if scenario "sbom-post-no-sinks-and-no-file"; then
out=$(env -i HOME="$HOME" PATH="$PATH" ./scripts/ingest/sbom-post.sh /nonexistent.cdx.json 2>&1) ; rc=$?
echo "${out}" > "${TMP_DIR}/out"
[ "${rc}" -ne 0 ] || FAILURES+=("${CURRENT_NAME}: expected non-zero on missing input file")
_must_contain "SBOM file missing or empty"
end_scenario; fi

if scenario "sbom-post-no-sinks-with-valid-file"; then
echo '{"bomFormat":"CycloneDX","specVersion":"1.6","components":[]}' > "${TMP_DIR}/test.cdx.json"
out=$(env -i HOME="$HOME" PATH="$PATH" ./scripts/ingest/sbom-post.sh "${TMP_DIR}/test.cdx.json" 2>&1) ; rc=$?
echo "${out}" > "${TMP_DIR}/out"
[ "${rc}" -eq 0 ] || FAILURES+=("${CURRENT_NAME}: expected zero exit when no sinks configured, got ${rc}")
_must_contain "no sinks configured"
# Both Artifactory sinks must appear in the hint list so users know
# the difference between Xray-indexed and plain archive.
_must_contain "ARTIFACTORY_SBOM_REPO"
_must_contain "ARTIFACTORY_SBOM_ARCHIVE_REPO"
end_scenario; fi

if scenario "sbom-post-dt-provenance-channels"; then
# DT git-provenance is carried via THREE channels DT actually keeps (verified
# against DT 4.13.6 source): projectVersion=IMAGE_TAG, the BOM's
# metadata.component.externalReferences (→ project External References), and
# post-upload project properties (needs PORTFOLIO_MANAGEMENT, best-effort).
# DT DROPS metadata.properties + rejects extra top-level keys, so those are
# NOT used. Guard each channel + the upload method so they can't silently
# revert. (Enrichment is a DT-local copy — other sinks get the raw SBOM.)
fails=0
# 1. projectVersion = IMAGE_TAG (git short sha), UPSTREAM_TAG fallback
grep -qE 'ver="\$\{IMAGE_TAG:-\$\{UPSTREAM_TAG:-latest\}\}"' scripts/ingest/sbom-post.sh \
  || { FAILURES+=("${CURRENT_NAME}: DT projectVersion no longer prefers IMAGE_TAG"); fails=1; }
# 2. JSON upload uses PUT (POST consumes only multipart → 415). Walk DT sink.
dt_method=$(awk '
  /^[[:space:]]*#/ {next}
  /dependency-track.*POST .*DEPENDENCY_TRACK_URL/ {indt=1}
  indt && /-X PUT/  {m="PUT"}
  indt && /-X POST/ {m="POST"}
  indt && /api\/v1\/bom/ {print m; exit}
' scripts/ingest/sbom-post.sh)
[ "${dt_method}" = "PUT" ] || { FAILURES+=("${CURRENT_NAME}: DT upload must use -X PUT (got '${dt_method:-none}')"); fails=1; }
# 3. (B) BOM-native: enrich metadata.component.externalReferences
grep -q 'metadata.component.externalReferences' scripts/ingest/sbom-post.sh \
  || { FAILURES+=("${CURRENT_NAME}: DT sink no longer enriches externalReferences"); fails=1; }
# 4. (A) post-upload project properties via the /property endpoint
grep -qE 'project/.*/property' scripts/ingest/sbom-post.sh \
  || { FAILURES+=("${CURRENT_NAME}: DT sink no longer posts project properties"); fails=1; }
# 5. property posting degrades gracefully when the key lacks PORTFOLIO_MANAGEMENT
grep -q 'lacks PORTFOLIO_MANAGEMENT' scripts/ingest/sbom-post.sh \
  || { FAILURES+=("${CURRENT_NAME}: DT property posting must skip gracefully on 403"); fails=1; }
# 6. enrichment is DT-LOCAL — the Splunk envelope is unchanged
grep -q "git_commit:\$gitsha, cyclonedx:\$bom\[0\]" scripts/ingest/sbom-post.sh \
  || { FAILURES+=("${CURRENT_NAME}: Splunk envelope shape changed — regression risk"); fails=1; }
[ "${fails}" -eq 0 ] && echo "DT provenance channels intact (version + externalReferences + properties, PUT, graceful)" > "${TMP_DIR}/out"
end_scenario; fi

if scenario "sbom-post-archive-sink-graceful-skip"; then
# SINK 4 must skip cleanly when ARTIFACTORY_SBOM_ARCHIVE_REPO unset.
echo '{"bomFormat":"CycloneDX","specVersion":"1.6","components":[]}' > "${TMP_DIR}/test.cdx.json"
out=$(env -i HOME="$HOME" PATH="$PATH" ./scripts/ingest/sbom-post.sh "${TMP_DIR}/test.cdx.json" 2>&1) ; rc=$?
echo "${out}" > "${TMP_DIR}/out"
_must_contain "artifactory-archive  skip"
end_scenario; fi

# ════════════════════════════════════════════════════════════════════
# Modular push backends — REGISTRY_KIND dispatch
# ════════════════════════════════════════════════════════════════════

if scenario "modular-harbor-backend-exists"; then
[ -f scripts/push-backends/harbor.sh ] || FAILURES+=("${CURRENT_NAME}: scripts/push-backends/harbor.sh missing")
bash -n scripts/push-backends/harbor.sh || FAILURES+=("${CURRENT_NAME}: harbor.sh fails bash -n")
grep -q '^push_to_backend()' scripts/push-backends/harbor.sh \
  || FAILURES+=("${CURRENT_NAME}: harbor.sh must export push_to_backend()")
echo "harbor.sh present + push_to_backend() defined" > "${TMP_DIR}/out"
end_scenario; fi

if scenario "modular-artifactory-backends-exist"; then
# The artifactory backend is split into 3 files:
#   artifactory_common.sh   shared helpers (sourced by both tiers)
#   artifactory_jcr.sh      Free tier
#   artifactory_pro.sh      Pro tier
# Both _jcr and _pro must define push_to_backend(); _common holds the
# shared internals and doesn't export the entry point itself.
for f in artifactory_common.sh artifactory_jcr.sh artifactory_pro.sh; do
  [ -f "scripts/push-backends/${f}" ] || FAILURES+=("${CURRENT_NAME}: scripts/push-backends/${f} missing")
  bash -n "scripts/push-backends/${f}"  || FAILURES+=("${CURRENT_NAME}: ${f} fails bash -n")
done
for f in artifactory_jcr.sh artifactory_pro.sh; do
  grep -q '^push_to_backend()' "scripts/push-backends/${f}" \
    || FAILURES+=("${CURRENT_NAME}: ${f} must export push_to_backend()")
done
# Pro must NOT depend on JCR or the python merger — Pro-only repos
# need to be able to delete those without breaking. Check non-comment
# lines only (docstring comparison tables legitimately mention them).
grep -vE '^[[:space:]]*#' scripts/push-backends/artifactory_pro.sh \
  | grep -qE 'artifactory_jcr|build-info-merge' \
  && FAILURES+=("${CURRENT_NAME}: artifactory_pro.sh code references JCR / build-info-merge — must stay deletion-safe") \
  || true
echo "artifactory_{common,jcr,pro}.sh present + push_to_backend() defined on jcr+pro" > "${TMP_DIR}/out"
end_scenario; fi

if scenario "registry-kind-default-resolves-to-harbor"; then
# When REGISTRY_KIND is unset, dispatch must resolve to harbor.sh.
# Strip Artifactory-derivation sources so the test exercises the
# default (Harbor) path. Use --push to actually trigger dispatch.
sed -i.bak -E '/^(HARBOR_REGISTRY|HARBOR_PROJECT|ARTIFACTORY_URL|ARTIFACTORY_PUSH_HOST|ARTIFACTORY_TEAM|REGISTRY_KIND)=/d' image.env && rm image.env.bak
echo 'HARBOR_REGISTRY="harbor.example.com"' >> image.env
echo 'HARBOR_PROJECT="apps/test"'           >> image.env
out=$(env -i HOME="$HOME" PATH="$PATH" BUILD_DEBUG=true ./scripts/build.sh --push 2>&1) ; rc=$?
echo "${out}" > "${TMP_DIR}/out"
echo "${out}" | grep -E "(dispatching push|Harbor push|harbor\.sh)" | head -5
# We expect the build to fail later (no docker daemon access in
# regression env), but it must reach the dispatch step naming harbor.
_must_contain "backend=harbor"
end_scenario; fi

if scenario "registry-kind-explicit-harbor"; then
sed -i.bak -E '/^(HARBOR_REGISTRY|HARBOR_PROJECT|REGISTRY_KIND)=/d' image.env && rm image.env.bak
echo 'HARBOR_REGISTRY="harbor.example.com"' >> image.env
echo 'HARBOR_PROJECT="apps/test"'           >> image.env
echo 'REGISTRY_KIND="harbor"'             >> image.env
out=$(env -i HOME="$HOME" PATH="$PATH" BUILD_DEBUG=true ./scripts/build.sh --push 2>&1) ; rc=$?
echo "${out}" > "${TMP_DIR}/out"
_must_contain "backend=harbor"
end_scenario; fi

if scenario "registry-kind-unknown-fails-with-listing"; then
# REGISTRY_KIND set to a backend that doesn't exist must fail loudly
# AND list the available backends so the user sees the typo.
sed -i.bak -E '/^(HARBOR_REGISTRY|HARBOR_PROJECT|REGISTRY_KIND)=/d' image.env && rm image.env.bak
echo 'HARBOR_REGISTRY="harbor.example.com"' >> image.env
echo 'HARBOR_PROJECT="apps/test"'           >> image.env
echo 'REGISTRY_KIND="nexus"'              >> image.env
out=$(env -i HOME="$HOME" PATH="$PATH" ./scripts/build.sh --push 2>&1) ; rc=$?
echo "${out}" > "${TMP_DIR}/out"
echo "${out}" | tail -10
[ "${rc}" -ne 0 ] || FAILURES+=("${CURRENT_NAME}: expected non-zero exit on unknown backend")
_must_contain "REGISTRY_KIND='nexus'"
_must_contain "Available backends:"
_must_contain "harbor"
_must_contain "artifactory"
end_scenario; fi

# ════════════════════════════════════════════════════════════════════
# Canonical artifact filenames (scripts/lib/artifact-names.sh)
# ════════════════════════════════════════════════════════════════════

if scenario "artifact-names-lib-defines-defaults"; then
out=$(bash -c '. scripts/lib/artifact-names.sh; echo "SBOM_FILE=${SBOM_FILE} VULN_SCAN_FILE=${VULN_SCAN_FILE}"')
echo "${out}" > "${TMP_DIR}/out"
echo "${out}"
_must_contain "SBOM_FILE=sbom.cdx.json"
_must_contain "VULN_SCAN_FILE=vuln-scan.json"
end_scenario; fi

if scenario "artifact-names-shell-override-wins"; then
# Shell-set value must win over the lib's default — that's how forks
# customise per-job (e.g. running both Trivy and Xray in one pipeline).
out=$(SBOM_FILE=alt.json VULN_SCAN_FILE=alt-vuln.json bash -c '. scripts/lib/artifact-names.sh; echo "SBOM_FILE=${SBOM_FILE} VULN_SCAN_FILE=${VULN_SCAN_FILE}"')
echo "${out}" > "${TMP_DIR}/out"
_must_contain "SBOM_FILE=alt.json"
_must_contain "VULN_SCAN_FILE=alt-vuln.json"
end_scenario; fi

if scenario "load-image-env-preserves-shell-canonical-names"; then
# Behavioural test: a shell-set SBOM_FILE / VULN_SCAN_FILE must SURVIVE
# load_image_env (sourcing image.env must not clobber it). This is the
# property that matters — and it holds without any hardcoded __extras
# list, because image.env doesn't define these names so sourcing leaves
# the shell value untouched.
out=$(env -i HOME="$HOME" PATH="$PATH" \
        SBOM_FILE="custom.cdx.json" \
        VULN_SCAN_FILE="custom-vuln.json" \
        bash -c 'cd "'"${PROJECT_ROOT}"'"; . scripts/lib/load-image-env.sh; LOAD_ENV_LOG=false load_image_env >/dev/null 2>&1; echo "SBOM_FILE=${SBOM_FILE} VULN_SCAN_FILE=${VULN_SCAN_FILE}"')
echo "${out}" > "${TMP_DIR}/out"
_must_contain "SBOM_FILE=custom.cdx.json"
_must_contain "VULN_SCAN_FILE=custom-vuln.json"
end_scenario; fi

# ════════════════════════════════════════════════════════════════════
# Modular scan scripts — existence + syntax + canonical wiring
# ════════════════════════════════════════════════════════════════════

if scenario "scan-scripts-exist-and-parse"; then
all_ok=1
for s in syft-sbom xray-sbom xray-vuln grype-vuln trivy-vuln trivy-sbom; do
  if [ ! -f "scripts/scan/${s}.sh" ]; then
    FAILURES+=("${CURRENT_NAME}: scripts/scan/${s}.sh missing"); all_ok=0; continue
  fi
  if ! bash -n "scripts/scan/${s}.sh" 2>/dev/null; then
    FAILURES+=("${CURRENT_NAME}: scripts/scan/${s}.sh fails bash -n"); all_ok=0
  fi
done
[ "${all_ok}" -eq 1 ] && echo "all 6 scan scripts present + parse" > "${TMP_DIR}/out" \
                       || echo "scan-scripts check: see failures" > "${TMP_DIR}/out"
end_scenario; fi

if scenario "scan-scripts-bootstrap-via-scan-common"; then
# Every scan producer must bootstrap via lib/scan-common.sh's
# scan_bootstrap, which sources artifact-names.sh (canonical SBOM_FILE/
# VULN_SCAN_FILE) + load-image-env + self-sources build.env. A single
# rename in artifact-names.sh then propagates everywhere through one path.
all_ok=1
for s in syft-sbom xray-sbom xray-vuln grype-vuln trivy-vuln trivy-sbom; do
  if ! grep -q 'lib/scan-common.sh' "scripts/scan/${s}.sh" \
     || ! grep -q 'scan_bootstrap' "scripts/scan/${s}.sh"; then
    FAILURES+=("${CURRENT_NAME}: scripts/scan/${s}.sh doesn't bootstrap via scan-common.sh"); all_ok=0
  fi
done
# And scan-common.sh itself must source the canonical names.
if ! grep -q 'lib/artifact-names.sh' scripts/lib/scan-common.sh; then
  FAILURES+=("${CURRENT_NAME}: scan-common.sh doesn't source artifact-names.sh"); all_ok=0
fi
[ "${all_ok}" -eq 1 ] && echo "all scan scripts bootstrap via scan-common.sh" > "${TMP_DIR}/out" \
                       || echo "scan-scripts scan-common wiring check: see failures" > "${TMP_DIR}/out"
end_scenario; fi

if scenario "scan-common-has-no-tool-specific-logic"; then
# The shared lib must stay generic — naming a specific tool would mean
# deleting that tool could require editing the lib. Override-var names
# are passed in as ARGUMENTS, so the lib never mentions a scanner.
if grep -qiE 'trivy|syft|grype|xray' scripts/lib/scan-common.sh; then
  FAILURES+=("${CURRENT_NAME}: scan-common.sh references a specific tool — breaks deletability")
  grep -niE 'trivy|syft|grype|xray' scripts/lib/scan-common.sh > "${TMP_DIR}/out"
else
  echo "scan-common.sh is tool-agnostic" > "${TMP_DIR}/out"
fi
end_scenario; fi

if scenario "scan-bootstrap-preserves-output-filename-override"; then
# An explicit shell/CI SBOM_FILE / VULN_SCAN_FILE override must survive the
# build.env self-source (which carries the canonical names) — so two SBOM
# producers can write distinct files in one pipeline. Build OUTPUTS
# (IMAGE_DIGEST) still come from build.env.
_PROJ="${TMP_DIR}/proj-outfile"; mkdir -p "${_PROJ}"
printf 'UPSTREAM_REF="docker.io/library/nginx:1.25.3-alpine"\nVENDOR="example.com"\n' > "${_PROJ}/image.env"
printf 'SBOM_FILE=sbom.cdx.json\nVULN_SCAN_FILE=vuln-scan.json\nIMAGE_DIGEST=ex/img@sha256:abc\n' > "${_PROJ}/build.env"
out=$(env -i HOME="$HOME" PATH="$PATH" LOAD_ENV_LOG=false \
  SBOM_FILE="sbom-trivy.cdx.json" VULN_SCAN_FILE="vuln-scan-trivy.json" \
  TEMPLATE_ROOT="${TEMPLATE_ROOT}" PROJECT_ROOT="${_PROJ}" \
  bash -c 'cd "${PROJECT_ROOT}"; . "${TEMPLATE_ROOT}/scripts/lib/scan-common.sh"; scan_bootstrap; echo "R SBOM_FILE=${SBOM_FILE} VULN_SCAN_FILE=${VULN_SCAN_FILE} IMAGE_DIGEST=${IMAGE_DIGEST}"') ; rc=$?
echo "${out}" > "${TMP_DIR}/out"
# Rooted at the image instance (artifact_paths), with the operator's
# filename intact — that is what "the override survived" now looks like.
_must_contain "SBOM_FILE=artifacts/nginx/sbom-trivy.cdx.json"
_must_contain "VULN_SCAN_FILE=artifacts/nginx/vuln-scan-trivy.json"
_must_contain "IMAGE_DIGEST=ex/img@sha256:abc"         # build OUTPUT still from build.env
rm -rf "${_PROJ}"
end_scenario; fi

if scenario "tool-deletability-delete-trivy-leaves-framework-intact"; then
# Management says "Trivy is banned — delete all traces." Deleting the
# trivy scan scripts must NOT affect the shared lib or any other scanner.
_DEL="${TMP_DIR}/del-trivy"; rm -rf "${_DEL}"; mkdir -p "${_DEL}"
cp -R scripts "${_DEL}/scripts"
cp image.env "${_DEL}/image.env"
rm -f "${_DEL}/scripts/scan/trivy-vuln.sh" "${_DEL}/scripts/scan/trivy-sbom.sh"
# A surviving scanner still resolves its target with trivy gone.
out=$(cd "${_DEL}" && env -i HOME="$HOME" PATH="$PATH" \
  LOAD_ENV_LOG=false \
  IMAGE_TRANSPORT=registry \
  IMAGE_REF="harbor.example.com/team/img@${_D1}" \
  IMAGE_DIGEST="${_D1}" \
  TEMPLATE_ROOT="${_DEL}" PROJECT_ROOT="${_DEL}" \
  bash "${_DEL}/scripts/scan/xray-vuln.sh" 2>&1 | head -3) ; rc=$?
echo "${out}" > "${TMP_DIR}/out"
_must_contain "→ Scan target: harbor.example.com/team/img@${_D1}"
# No surviving script EXECUTES/sources a deleted trivy script (comment
# mentions are harmless docs — only functional references break things).
if grep -rn 'trivy-vuln\.sh\|trivy-sbom\.sh' "${_DEL}/scripts" 2>/dev/null \
     | grep -v '/test/' | grep -vE ':[[:space:]]*#' | grep -q .; then
  FAILURES+=("${CURRENT_NAME}: a surviving script functionally references a deleted trivy script")
fi
rm -rf "${_DEL}"
end_scenario; fi

if scenario "syft-sbom-no-target-fails-loudly"; then
# Strip image.env's UPSTREAM_* (including the single-URL UPSTREAM_REF)
# so the resolution chain has nothing to fall back to — script must
# exit non-zero with the chain message.
sed -i.bak -E '/^UPSTREAM_(REF|REGISTRY|IMAGE|TAG)=/d' image.env && rm image.env.bak
out=$(env -i HOME="$HOME" PATH="$PATH" ./scripts/scan/syft-sbom.sh 2>&1) ; rc=$?
echo "${out}" > "${TMP_DIR}/out"
[ "${rc}" -ne 0 ] || FAILURES+=("${CURRENT_NAME}: expected non-zero exit when no target available")
_must_contain "no scan target available"
_must_contain "Resolution chain"
end_scenario; fi

if scenario "syft-sbom-target-resolution-image-digest-wins"; then
# IMAGE_DIGEST (build.env) beats UPSTREAM_REF — same chain as Xray.
# LOAD_ENV_LOG=false silences load_image_env's multi-line config listing
# so the "→ Scan target:" marker lands within the head window.
out=$(env -i HOME="$HOME" PATH="$PATH" \
  LOAD_ENV_LOG=false \
  IMAGE_TRANSPORT=registry \
  IMAGE_REF="harbor.example.com/team/img@${_D1}" \
  IMAGE_DIGEST="${_D1}" \
  ./scripts/scan/syft-sbom.sh 2>&1 | head -3) ; rc=$?
echo "${out}" > "${TMP_DIR}/out"
_must_contain "→ Scan target: harbor.example.com/team/img@${_D1}"
end_scenario; fi

if scenario "syft-sbom-source-target-mode"; then
# SBOM_TARGET=source switches to dir:${PROJECT_ROOT} so forks scanning
# Ansible/pip/npm sources still work. LOAD_ENV_LOG=false keeps the
# "→ Scan target:" marker within the head window.
out=$(env -i HOME="$HOME" PATH="$PATH" \
  LOAD_ENV_LOG=false \
  SBOM_TARGET=source \
  ./scripts/scan/syft-sbom.sh 2>&1 | head -3) ; rc=$?
echo "${out}" > "${TMP_DIR}/out"
_must_contain "→ Scan target: dir:"
end_scenario; fi

if scenario "grype-vuln-missing-sbom-fails"; then
# Grype needs an SBOM as input — must exit non-zero with a useful hint.
out=$(env -i HOME="$HOME" PATH="$PATH" \
  SBOM_FILE="/tmp/definitely-not-here.cdx.json" \
  ./scripts/scan/grype-vuln.sh 2>&1) ; rc=$?
echo "${out}" > "${TMP_DIR}/out"
echo "${out}" | head -5
[ "${rc}" -ne 0 ] || FAILURES+=("${CURRENT_NAME}: expected non-zero exit on missing SBOM input")
_must_contain "SBOM not found"
_must_contain "syft-sbom.sh"  # pointer to a producer
end_scenario; fi

if scenario "trivy-version-safety-guard"; then
# Hard-coded check in both trivy scripts must refuse compromised
# v0.69.4-v0.69.6. Confirm by grepping the script source.
all_ok=1
for s in scripts/scan/trivy-vuln.sh scripts/scan/trivy-sbom.sh; do
  if ! grep -q '0\.69\.4|0\.69\.5|0\.69\.6' "${s}"; then
    FAILURES+=("${CURRENT_NAME}: ${s} missing compromised-version guard"); all_ok=0
  fi
  if ! grep -q 'TRIVY_VERSION:-0\.69\.3' "${s}"; then
    FAILURES+=("${CURRENT_NAME}: ${s} not pinned to safe v0.69.3 default"); all_ok=0
  fi
done
[ "${all_ok}" -eq 1 ] && echo "trivy scripts pinned + guarded" > "${TMP_DIR}/out" \
                       || echo "trivy safety check: see failures" > "${TMP_DIR}/out"
end_scenario; fi

if scenario "trivy-kill-switch-off-by-default"; then
# Trivy is OPT-IN: with ALLOW_TRIVY unset (default 0) the script must
# refuse up front (exit non-zero) BEFORE any target resolution / install.
out=$(env -i HOME="$HOME" PATH="$PATH" ./scripts/scan/trivy-vuln.sh 2>&1) ; rc=$?
echo "${out}" > "${TMP_DIR}/out"
[ "${rc}" -ne 0 ] || FAILURES+=("${CURRENT_NAME}: ALLOW_TRIVY unset should disable (exit non-zero)")
_must_contain "disabled (ALLOW_TRIVY="
end_scenario; fi

if scenario "trivy-toggle-on-no-target-fails-loudly"; then
# With ALLOW_TRIVY=1 the gate passes; with no resolvable target (strip
# UPSTREAM_REF AND the legacy triple) it must then fail loudly with the
# chain message BEFORE attempting an install.
sed -i.bak -E '/^UPSTREAM_(REF|REGISTRY|IMAGE|TAG)=/d' image.env && rm image.env.bak
out=$(env -i HOME="$HOME" PATH="$PATH" ALLOW_TRIVY=1 ./scripts/scan/trivy-vuln.sh 2>&1) ; rc=$?
echo "${out}" > "${TMP_DIR}/out"
[ "${rc}" -ne 0 ] || FAILURES+=("${CURRENT_NAME}: expected non-zero exit when no target")
_must_not_contain "disabled (ALLOW_TRIVY="
_must_contain "no scan target available"
end_scenario; fi

if scenario "scan-scripts-write-canonical-filenames"; then
# Verify each producer script's output-resolution code references the
# canonical env var (SBOM_FILE for SBOM producers, VULN_SCAN_FILE for
# vuln scanners). Catches future regressions where someone hardcodes a
# custom name and breaks the swap-out contract.
all_ok=1
for s in syft-sbom xray-sbom trivy-sbom; do
  if ! grep -q '"\${SBOM_FILE}"' "scripts/scan/${s}.sh"; then
    FAILURES+=("${CURRENT_NAME}: scripts/scan/${s}.sh doesn't honour \${SBOM_FILE}"); all_ok=0
  fi
done
for s in xray-vuln grype-vuln trivy-vuln; do
  if ! grep -q '"\${VULN_SCAN_FILE}"' "scripts/scan/${s}.sh"; then
    FAILURES+=("${CURRENT_NAME}: scripts/scan/${s}.sh doesn't honour \${VULN_SCAN_FILE}"); all_ok=0
  fi
done
[ "${all_ok}" -eq 1 ] && echo "all scan scripts honour canonical names" > "${TMP_DIR}/out" \
                       || echo "canonical-names wiring check: see failures" > "${TMP_DIR}/out"
end_scenario; fi

# ════════════════════════════════════════════════════════════════════
# Script-driven / idempotent / repeat-run safety
# ════════════════════════════════════════════════════════════════════
# Scan scripts self-source build.env so `build.sh` → `scan` works with no
# manual `. ./build.env`. These pin that contract + the override precedence.

if scenario "scan-self-sources-build-env"; then
# Standalone scan (caller did NOT source build.env) must pick up the
# built image's identity from build.env on its own.
_PROJ="${TMP_DIR}/proj-selfsrc"; mkdir -p "${_PROJ}"; cp "${SNAPSHOT}" "${_PROJ}/image.env"
_write_registry_build_env "${_PROJ}" "${_D1}"
out=$(env -i HOME="$HOME" PATH="$PATH" PROJECT_ROOT="${_PROJ}" LOAD_ENV_LOG=false ./scripts/scan/xray-vuln.sh 2>&1)
echo "${out}" > "${TMP_DIR}/out"
_must_contain "→ Scan target: reg.example.com/img@${_D1}"
rm -rf "${_PROJ}"
end_scenario; fi

if scenario "scan-prescan-ref-wins-over-build-env"; then
# Explicit XRAY_SCAN_REF (prescan / override) must beat build.env's digest.
_PROJ="${TMP_DIR}/proj-prescan"; mkdir -p "${_PROJ}"; cp "${SNAPSHOT}" "${_PROJ}/image.env"
_write_registry_build_env "${_PROJ}" "${_D1}"
out=$(env -i HOME="$HOME" PATH="$PATH" PROJECT_ROOT="${_PROJ}" LOAD_ENV_LOG=false \
        XRAY_SCAN_REF="upstream.example.com/nginx:1.0" ./scripts/scan/xray-vuln.sh 2>&1)
echo "${out}" > "${TMP_DIR}/out"
_must_contain "→ Scan target: upstream.example.com/nginx:1.0"
_must_not_contain "${_D1}"
rm -rf "${_PROJ}"
end_scenario; fi

if scenario "scan-no-build-env-falls-to-upstream"; then
# No build.env (clean / prescan) → falls to UPSTREAM_REF from image.env.
# Self-contained image.env so the assertion is environment-independent.
_PROJ="${TMP_DIR}/proj-nobe"; mkdir -p "${_PROJ}"
printf 'UPSTREAM_REF="docker.io/library/nginx:1.25.3-alpine"\nVENDOR="example.com"\n' > "${_PROJ}/image.env"
out=$(env -i HOME="$HOME" PATH="$PATH" PROJECT_ROOT="${_PROJ}" LOAD_ENV_LOG=false ./scripts/scan/xray-vuln.sh 2>&1)
echo "${out}" > "${TMP_DIR}/out"
_must_contain "→ Scan target: docker.io/library/nginx:1.25.3-alpine"
rm -rf "${_PROJ}"
end_scenario; fi

# ════════════════════════════════════════════════════════════════════
# Image identity hand-off — build.env + image.json
# ════════════════════════════════════════════════════════════════════
# The build has to name the image IT produced, in both transports, and a
# consumer has to refuse a record it cannot check. The docker stub below
# plays both exporters: --load for a push, and an OCI archive otherwise,
# writing the index.json BuildKit would write. Digests come in through
# the environment so the stub stays a quoted heredoc with nothing in it
# to escape.

IDSTUB="${TMP_DIR}/identity-stub"; mkdir -p "${IDSTUB}"
cat > "${IDSTUB}/crane" <<'STUB'
#!/bin/sh
# Knows the upstream and nothing else, so a push digest that the push
# itself did not report stays unresolved rather than quietly becoming
# the upstream's.
case "$1" in
  digest)
    case "$2" in
      docker.io/*) printf '%s\n' "${STUB_UPSTREAM_DIGEST}" ;;
      *) exit 1 ;;
    esac ;;
  config) printf '{"config":{"User":"nginx"}}\n' ;;
  *) exit 1 ;;
esac
STUB
cp "${DOCKER_STUB}" "${IDSTUB}/docker"
chmod +x "${IDSTUB}/crane" "${IDSTUB}/docker"

# A project root the build can write into without touching the template.
_id_project() {
  local p="${TMP_DIR}/$1"
  rm -rf "${p}"; mkdir -p "${p}"
  cp "${SNAPSHOT}" "${p}/image.env"
  printf 'FROM scratch\n' > "${p}/Dockerfile.example"
  printf '%s' "${p}"
}

# Assert a plain KEY=VALUE line in a build.env.
_build_env_has() {
  local file="$1" line="$2"
  grep -qxF -- "${line}" "${file}" 2>/dev/null \
    || FAILURES+=("${CURRENT_NAME}: ${file##*/} missing line: ${line}")
}

if scenario "no-push-hands-over-the-oci-archive-it-built"; then
# No registry involved, so the hand-off is the archive: its own manifest
# digest, its path, its sha256. Never the upstream reference, which names
# an image without this build's changes in it.
_P="$(_id_project proj-nopush-archive)"
_run env -i HOME="$HOME" PATH="${IDSTUB}:$PATH" \
  STUB_UPSTREAM_DIGEST="${_D1}" STUB_ARCHIVE_DIGEST="${_D2}" \
  ./scripts/build.sh --project-root "${_P}" >/dev/null
_must_contain "transport=oci-archive"
_build_env_has "${_P}/build.env" "IMAGE_TRANSPORT=oci-archive"
_build_env_has "${_P}/build.env" "IMAGE_DIGEST=${_D2}"
_build_env_has "${_P}/build.env" "IMAGE_REF=oci-archive:image.oci.tar"
_build_env_has "${_P}/build.env" "IMAGE_ARCHIVE=image.oci.tar"
_build_env_has "${_P}/build.env" "IMAGE_SUBJECT_KIND=manifest"
_build_env_has "${_P}/build.env" "IMAGE_PLATFORMS=linux/amd64"
[ -s "${_P}/image.oci.tar" ] || FAILURES+=("${CURRENT_NAME}: no OCI archive was exported")
# The recorded sha256 has to be the archive's, or a consumer cannot tell
# a truncated artifact from a whole one.
_arch_sha=$(shasum -a 256 "${_P}/image.oci.tar" 2>/dev/null | cut -d' ' -f1)
[ -z "${_arch_sha}" ] && _arch_sha=$(sha256sum "${_P}/image.oci.tar" | cut -d' ' -f1)
_build_env_has "${_P}/build.env" "IMAGE_ARCHIVE_SHA256=${_arch_sha}"
end_scenario; fi

if scenario "no-push-image-ref-is-never-the-upstream-ref"; then
# The defect this replaces: build.env used to set IMAGE_REF=UPSTREAM_REF,
# so every postscan job described the upstream image.
_P="$(_id_project proj-nopush-notupstream)"
_run env -i HOME="$HOME" PATH="${IDSTUB}:$PATH" \
  STUB_UPSTREAM_DIGEST="${_D1}" STUB_ARCHIVE_DIGEST="${_D2}" \
  ./scripts/build.sh --project-root "${_P}" >/dev/null
grep -q '^IMAGE_REF=docker.io/library/nginx' "${_P}/build.env" \
  && FAILURES+=("${CURRENT_NAME}: IMAGE_REF still names the upstream image")
grep -q "^IMAGE_DIGEST=${_D1}\$" "${_P}/build.env" \
  && FAILURES+=("${CURRENT_NAME}: IMAGE_DIGEST is the upstream digest")
end_scenario; fi

if scenario "no-push-agrees-between-build-env-and-image-json"; then
# build.env is the dotenv convenience; image.json is authoritative. A
# consumer compares them, so they have to say the same thing.
_P="$(_id_project proj-agree)"
_run env -i HOME="$HOME" PATH="${IDSTUB}:$PATH" \
  STUB_UPSTREAM_DIGEST="${_D1}" STUB_ARCHIVE_DIGEST="${_D2}" \
  ./scripts/build.sh --project-root "${_P}" >/dev/null
if [ ! -s "${_P}/image.json" ]; then
  FAILURES+=("${CURRENT_NAME}: no image.json was written")
else
  set -a; . "${_P}/build.env"; set +a
  for _pair in "schema_version 1" "digest ${IMAGE_DIGEST}" "reference ${IMAGE_REF}" \
               "transport ${IMAGE_TRANSPORT}" "archive ${IMAGE_ARCHIVE}" \
               "archive_sha256 ${IMAGE_ARCHIVE_SHA256}" "tag ${IMAGE_TAG}" \
               "subject_kind ${IMAGE_SUBJECT_KIND}" "repository ${IMAGE_REPOSITORY}" \
               "upstream_ref ${UPSTREAM_REF}" "source_commit ${GIT_SHA}"; do
    _field="${_pair%% *}"; _want="${_pair#* }"
    _got=$(jq -r ".${_field} | tostring" "${_P}/image.json")
    [ "${_got}" = "${_want}" ] \
      || FAILURES+=("${CURRENT_NAME}: image.json .${_field}=${_got}, build.env says ${_want}")
  done
  _got=$(jq -r '.platforms | join(",")' "${_P}/image.json")
  [ "${_got}" = "linux/amd64" ] \
    || FAILURES+=("${CURRENT_NAME}: image.json .platforms=${_got}")
fi
end_scenario; fi

if scenario "no-push-without-buildx-builder-creates-one"; then
# The OCI exporter is unavailable on buildx's default driver, so the
# no-push path provisions a docker-container builder when none exists.
_P="$(_id_project proj-builder)"
: > "${TMP_DIR}/builder.log"
_run env -i HOME="$HOME" PATH="${IDSTUB}:$PATH" \
  STUB_UPSTREAM_DIGEST="${_D1}" STUB_ARCHIVE_DIGEST="${_D2}" \
  STUB_NO_BUILDER=1 STUB_BUILDER_LOG="${TMP_DIR}/builder.log" \
  ./scripts/build.sh --project-root "${_P}" >/dev/null
grep -q -- "--driver docker-container" "${TMP_DIR}/builder.log" \
  || FAILURES+=("${CURRENT_NAME}: no docker-container builder was created")
end_scenario; fi

if scenario "push-digest-comes-from-the-push-not-the-upstream"; then
# The pushed manifest digest is the registry's answer, read from the push
# itself. The upstream digest is a different number and must not leak
# into the field a downstream job scans by.
_P="$(_id_project proj-push)"
_run env -i HOME="$HOME" PATH="${IDSTUB}:$PATH" \
  STUB_UPSTREAM_DIGEST="${_D1}" STUB_PUSH_DIGEST="${_D2}" \
  REGISTRY_KIND=harbor HARBOR_REGISTRY=harbor.example.com HARBOR_PROJECT=team \
  ./scripts/build.sh --push --project-root "${_P}" >/dev/null
_build_env_has "${_P}/build.env" "IMAGE_TRANSPORT=registry"
_build_env_has "${_P}/build.env" "IMAGE_DIGEST=${_D2}"
_build_env_has "${_P}/build.env" "IMAGE_REF=harbor.example.com/team/nginx@${_D2}"
_build_env_has "${_P}/build.env" "IMAGE_ARCHIVE="
grep -q "^IMAGE_TAG_REF=harbor.example.com/team/nginx:1.25.3-alpine" "${_P}/build.env" \
  || FAILURES+=("${CURRENT_NAME}: IMAGE_TAG_REF is not the pushed tag reference")
grep -q "^IMAGE_REF=docker.io/library/nginx" "${_P}/build.env" \
  && FAILURES+=("${CURRENT_NAME}: IMAGE_REF still names the upstream image")
grep -q "^IMAGE_DIGEST=${_D1}\$" "${_P}/build.env" \
  && FAILURES+=("${CURRENT_NAME}: IMAGE_DIGEST is the upstream digest")
[ -s "${_P}/image.json" ] || FAILURES+=("${CURRENT_NAME}: the push path wrote no image.json")
end_scenario; fi

if scenario "push-with-unresolvable-digest-fails"; then
# A push whose digest cannot be resolved has produced no identity worth
# handing on. Better a red build job than a green scan of nothing.
_P="$(_id_project proj-push-nodigest)"
_run env -i HOME="$HOME" PATH="${IDSTUB}:$PATH" \
  STUB_UPSTREAM_DIGEST="${_D1}" STUB_PUSH_DIGEST="" \
  REGISTRY_KIND=harbor HARBOR_REGISTRY=harbor.example.com HARBOR_PROJECT=team \
  ./scripts/build.sh --push --project-root "${_P}" >/dev/null
rc=$?
[ "${rc}" -ne 0 ] || FAILURES+=("${CURRENT_NAME}: expected non-zero exit")
_must_contain "is not a manifest digest"
end_scenario; fi

# ── Consumer side: what a scan job accepts as its subject ───────────

if scenario "subject-resolves-to-the-archive-the-build-wrote"; then
_P="$(_id_project proj-subject-ok)"
_make_oci_archive "${_P}/image.oci.tar" "${_D2}"
printf 'IMAGE_TRANSPORT=oci-archive\nIMAGE_ARCHIVE=image.oci.tar\nIMAGE_DIGEST=%s\nIMAGE_REF=oci-archive:image.oci.tar\n' \
  "${_D2}" > "${_P}/build.env"
_run _resolve_subject "${_P}" >/dev/null
_must_contain "oci-archive:image.oci.tar"
_must_not_contain "ERROR"
end_scenario; fi

if scenario "subject-missing-archive-fails"; then
# The build.env claims an archive the job never received. Scanning the
# upstream instead would report a pass for an image nobody will run.
_P="$(_id_project proj-subject-missing)"
printf 'IMAGE_TRANSPORT=oci-archive\nIMAGE_ARCHIVE=image.oci.tar\nIMAGE_DIGEST=%s\n' \
  "${_D2}" > "${_P}/build.env"
_run _resolve_subject "${_P}" >/dev/null
_must_contain "the archive is missing or empty"
_must_not_contain "docker.io/library/nginx"
end_scenario; fi

if scenario "subject-digest-mismatch-fails"; then
# The archive on disk is not the one the record describes.
_P="$(_id_project proj-subject-mismatch)"
_make_oci_archive "${_P}/image.oci.tar" "${_D3}"
printf 'IMAGE_TRANSPORT=oci-archive\nIMAGE_ARCHIVE=image.oci.tar\nIMAGE_DIGEST=%s\n' \
  "${_D2}" > "${_P}/build.env"
_run _resolve_subject "${_P}" >/dev/null
_must_contain "does not describe the archive it points at"
end_scenario; fi

if scenario "subject-archive-sha256-mismatch-fails"; then
# Right digest inside, wrong bytes on disk — a truncated artifact fetch.
_P="$(_id_project proj-subject-sha)"
_make_oci_archive "${_P}/image.oci.tar" "${_D2}"
printf 'IMAGE_TRANSPORT=oci-archive\nIMAGE_ARCHIVE=image.oci.tar\nIMAGE_DIGEST=%s\nIMAGE_ARCHIVE_SHA256=%s\n' \
  "${_D2}" "0000000000000000000000000000000000000000000000000000000000000000" > "${_P}/build.env"
_run _resolve_subject "${_P}" >/dev/null
_must_contain "sha256 is"
end_scenario; fi

if scenario "subject-empty-digest-fails"; then
_P="$(_id_project proj-subject-empty)"
_make_oci_archive "${_P}/image.oci.tar" "${_D2}"
printf 'IMAGE_TRANSPORT=oci-archive\nIMAGE_ARCHIVE=image.oci.tar\nIMAGE_DIGEST=\n' > "${_P}/build.env"
_run _resolve_subject "${_P}" >/dev/null
_must_contain "no usable IMAGE_DIGEST"
_must_not_contain "docker.io/library/nginx"
end_scenario; fi

if scenario "subject-image-json-disagreement-fails"; then
# image.json is authoritative. A build.env that says something else is a
# leftover from an earlier run.
_P="$(_id_project proj-subject-stale)"
_make_oci_archive "${_P}/image.oci.tar" "${_D2}"
printf 'IMAGE_TRANSPORT=oci-archive\nIMAGE_ARCHIVE=image.oci.tar\nIMAGE_DIGEST=%s\n' \
  "${_D2}" > "${_P}/build.env"
printf '{"schema_version":1,"digest":"%s"}\n' "${_D3}" > "${_P}/image.json"
_run _resolve_subject "${_P}" >/dev/null
_must_contain "disagrees with"
end_scenario; fi

if scenario "subject-build-env-without-transport-fails"; then
# A record written before this contract existed names an image, but not
# how to reach it or how to check it.
_P="$(_id_project proj-subject-legacy)"
printf 'IMAGE_REF=docker.io/library/nginx:1.25.3-alpine\nIMAGE_DIGEST=\n' > "${_P}/build.env"
_run _resolve_subject "${_P}" >/dev/null
_must_contain "no IMAGE_TRANSPORT"
end_scenario; fi

if scenario "subject-registry-ref-without-its-digest-fails"; then
_P="$(_id_project proj-subject-tagonly)"
printf 'IMAGE_TRANSPORT=registry\nIMAGE_REF=reg.example.com/img:1.2.3\nIMAGE_DIGEST=%s\n' \
  "${_D2}" > "${_P}/build.env"
_run _resolve_subject "${_P}" >/dev/null
_must_contain "is not the digest reference"
end_scenario; fi

# ── Upstream reference parsing inside the JCR backend ───────────────

if scenario "jcr-pinned-upstream-ref-not-mangled-into-the-repo-path"; then
# A UPSTREAM_REF of the form repo:tag@sha256:… used to be split with
# string surgery that left the tag inside the repository path, so the
# registry call asked for /v2/library/nginx:1.25.3-alpine/manifests/….
# The decomposed variables build.sh already exports are the answer.
CSTUB="${TMP_DIR}/curl-stub"; mkdir -p "${CSTUB}"
cat > "${CSTUB}/curl" <<'STUB'
#!/bin/sh
for a in "$@"; do
  case "$a" in https://*) printf '%s\n' "$a" >> "${CURL_URL_LOG:-/dev/null}" ;; esac
done
exit 1
STUB
chmod +x "${CSTUB}/curl"
: > "${TMP_DIR}/curl.log"
mkdir -p "${TMP_DIR}/jcr-merge"
env -i HOME="$HOME" PATH="${CSTUB}:${STUB_BIN}:$PATH" \
  CURL_URL_LOG="${TMP_DIR}/curl.log" \
  ARTIFACTORY_USER=u ARTIFACTORY_TOKEN=t \
  UPSTREAM_REGISTRY=docker.io UPSTREAM_IMAGE=library/nginx \
  UPSTREAM_TAG=1.25.3-alpine UPSTREAM_DIGEST="${_D1}" \
  UPSTREAM_REF="docker.io/library/nginx:1.25.3-alpine@${_D1}" \
  MERGE_DIR="${TMP_DIR}/jcr-merge" \
  bash -c '. ./scripts/push-backends/artifactory_jcr.sh
           _artifactory_jcr_fetch_manifests_for_merge \
             "art.example.com/team/nginx:1.0" "${MERGE_DIR}"' >/dev/null 2>&1
cp "${TMP_DIR}/curl.log" "${TMP_DIR}/out"
_must_contain "https://docker.io/v2/library/nginx/manifests/${_D1}"
_must_not_contain "library/nginx:1.25.3-alpine"
_must_not_contain "@sha256"
# The push target is ours to compose, and still splits correctly.
_must_contain "https://art.example.com/v2/team/nginx/manifests/1.0"
end_scenario; fi

# ── Which answer names the image a push created ─────────────────────

if scenario "artifactory-digest-comes-from-the-push-not-the-tag"; then
# crane digest reads a MUTABLE tag, so on a repository two pipelines push
# the same tag to, it can answer with the other pipeline's image. The
# push's own "digest: sha256:…" line is about this push, so it wins, and
# a disagreement between the two means the tag moved under this build.
DIG_STUB="${TMP_DIR}/digest-stub"; mkdir -p "${DIG_STUB}"
printf '#!/bin/sh\nprintf "%%s\\n" "${STUB_TAG_DIGEST:-}"\n' > "${DIG_STUB}/crane"
chmod +x "${DIG_STUB}/crane"
cat > "${TMP_DIR}/resolve-digest.sh" <<'RUN'
. "${TEMPLATE_ROOT}/scripts/push-backends/artifactory_common.sh"
d=$(_artifactory_resolve_push_digest "${TARGET}" "${PUSH_OUT}") ; rc=$?
printf 'DIGEST %s\nRC %s\n' "${d:-<none>}" "${rc}"
RUN
_resolve_digest() {
  out=$(env -i HOME="$HOME" PATH="${DIG_STUB}:${STUB_BIN}:$PATH" \
    TEMPLATE_ROOT="${TEMPLATE_ROOT}" TARGET="art.example.com/team/nginx:1.0" \
    "$@" bash "${TMP_DIR}/resolve-digest.sh" 2>&1)
  printf '%s\n' "${out}" > "${TMP_DIR}/out"
  cat "${TMP_DIR}/out"
}
# Both answers agree — the ordinary push.
_resolve_digest PUSH_OUT="1.0: digest: ${_D2} size: 1234" STUB_TAG_DIGEST="${_D2}"
_must_contain "DIGEST ${_D2}"
_must_contain "RC 0"
# The tag has moved since the push. Neither answer is safely this build's
# image, so the resolver refuses instead of naming the newer one.
_resolve_digest PUSH_OUT="1.0: digest: ${_D2} size: 1234" STUB_TAG_DIGEST="${_D3}"
_must_contain "Another push moved the tag"
_must_contain "DIGEST <none>"
_must_contain "RC 1"
_must_not_contain "DIGEST ${_D3}"
# No push output to read (the Pro path) — the tag read-back is the fallback.
_resolve_digest PUSH_OUT="" STUB_TAG_DIGEST="${_D2}"
_must_contain "DIGEST ${_D2}"
_must_contain "RC 0"
end_scenario; fi

if scenario "build-dry-run-twice-identical"; then
# Same commit, two consecutive subshell dry-runs → byte-identical config
# (proves reproducible CREATED + no cross-run drift). STUB_BIN avoids net.
out1=$(env -i HOME="$HOME" PATH="${STUB_BIN}:$PATH" ./scripts/build.sh --dry-run 2>&1 | grep -E 'Upstream:|Image:|Created \(UTC\):')
out2=$(env -i HOME="$HOME" PATH="${STUB_BIN}:$PATH" ./scripts/build.sh --dry-run 2>&1 | grep -E 'Upstream:|Image:|Created \(UTC\):')
printf '%s\n' "${out1}" > "${TMP_DIR}/out"
if [ "${out1}" != "${out2}" ]; then FAILURES+=("${CURRENT_NAME}: dry-run config differs between runs"); fi
_must_contain "Created (UTC):"
end_scenario; fi

if scenario "ingest-empty-file-fails-loudly"; then
# 0-byte SBOM/vuln artifact must fail (the -s guard), not ship empty data.
: > "${TMP_DIR}/empty.cdx.json"
out=$(env -i HOME="$HOME" PATH="$PATH" ./scripts/ingest/sbom-post.sh "${TMP_DIR}/empty.cdx.json" 2>&1) ; rc=$?
echo "${out}" > "${TMP_DIR}/out"
[ "${rc}" -ne 0 ] || FAILURES+=("${CURRENT_NAME}: expected non-zero on empty SBOM input")
_must_contain "missing or empty"
end_scenario; fi

# ════════════════════════════════════════════════════════════════════
# bamboo_* auto-import
# ════════════════════════════════════════════════════════════════════

if scenario "bamboo-auto-import"; then
# Set a bamboo_VENDOR in env; build.sh's import_bamboo_vars should
# auto-import it as VENDOR (and the config-report block prints it).
_run env -i HOME="$HOME" PATH="$PATH" \
  bamboo_VENDOR="bamboo-detected" \
  ./scripts/build.sh --dry-run >/dev/null
_must_contain "Auto-imported"
_must_contain "Vendor:             bamboo-detected"
end_scenario; fi

if scenario "bamboo-auto-import-shell-wins"; then
# Explicit shell export of the bare name should beat the bamboo_* import.
_run env -i HOME="$HOME" PATH="$PATH" \
  bamboo_VENDOR="bamboo-loses" \
  VENDOR="shell-wins" \
  ./scripts/build.sh --dry-run >/dev/null
_must_contain "Vendor:             shell-wins"
_must_not_contain "Vendor:             bamboo-loses"
end_scenario; fi

# ════════════════════════════════════════════════════════════════════
# Evidence binding + gate enforcement
# ════════════════════════════════════════════════════════════════════
# A gate is only worth having if it cannot pass by accident. These pin
# the four ways that used to happen: no build record, evidence about a
# different image, a tool or report that is not there, and a policy
# naming a severity nothing reports.

# Stands in for grype: answers the version + DB probes with the pinned
# version so no install is attempted, and writes whatever report the
# scenario asked for to the -o json=<path> target.
GRYPE_STUB="${TMP_DIR}/grype-bin"; mkdir -p "${GRYPE_STUB}"
cat > "${GRYPE_STUB}/grype" <<'STUB'
#!/bin/sh
case "$1" in
  version) printf 'Application:   grype\nVersion:       0.114.0\n'; exit 0 ;;
  db)      printf 'Built:     2026-01-01T00:00:00Z\nStatus:    valid\n'; exit 0 ;;
esac
[ -n "${STUB_GRYPE_RC:-}" ] && exit "${STUB_GRYPE_RC}"
for a in "$@"; do
  case "$a" in json=*) printf '%s' "${STUB_GRYPE_REPORT:-}" > "${a#json=}" ;; esac
done
exit 0
STUB
chmod +x "${GRYPE_STUB}/grype"

# scan_gate on its own: no scanner, no image, just a report and a policy.
cat > "${TMP_DIR}/gate-driver.sh" <<'DRV'
#!/usr/bin/env bash
# usage: gate-driver.sh <report-path> <fail-on>
# shellcheck source=/dev/null
. "${TEMPLATE_ROOT}/scripts/lib/scan-common.sh"
scan_gate demo 1.0 "" "$1" matches \
  '[.matches[] | select((.vulnerability.severity // "" | ascii_downcase) == $s)] | length' \
  "$2"
DRV

# Reports scan_bootstrap's resolved output paths, for the collision test.
cat > "${TMP_DIR}/paths-driver.sh" <<'DRV'
#!/usr/bin/env bash
# shellcheck source=/dev/null
cd "${PROJECT_ROOT}" || exit 1   # as every scan script does before bootstrapping
. "${TEMPLATE_ROOT}/scripts/lib/scan-common.sh"
scan_bootstrap
echo "PATHS ${SBOM_FILE} ${VULN_SCAN_FILE}"
DRV

_GATE_ROOT="${TMP_DIR}/gate-root"; mkdir -p "${_GATE_ROOT}"

# _run_gate <report> <fail-on> [KEY=VALUE …] — scan_gate in a bare env.
_run_gate() {
  local report="$1" fail_on="$2"; shift 2
  env -i HOME="${HOME}" PATH="${PATH}" \
    TEMPLATE_ROOT="${TEMPLATE_ROOT}" PROJECT_ROOT="${TMP_DIR}" \
    SCAN_ARTIFACT_ROOT="${_GATE_ROOT}" "$@" \
    bash "${TMP_DIR}/gate-driver.sh" "${report}" "${fail_on}"
}

printf '{"matches":[]}' > "${TMP_DIR}/clean-report.json"
printf '{"matches":[{"vulnerability":{"id":"CVE-0000-0001","severity":"Critical"}}]}' \
  > "${TMP_DIR}/dirty-report.json"
printf 'this is not json' > "${TMP_DIR}/malformed-report.json"

if scenario "gate-clean-report-passes"; then
_run_gate "${TMP_DIR}/clean-report.json" "critical,high" > "${TMP_DIR}/out" 2>&1 ; rc=$?
cat "${TMP_DIR}/out"
[ "${rc}" -eq 0 ] || FAILURES+=("${CURRENT_NAME}: expected exit 0 on a clean report, got ${rc}")
_must_contain "✓ PASS"
grep -q '"outcome": "pass"' "${_GATE_ROOT}/scan-result.json" \
  || FAILURES+=("${CURRENT_NAME}: scan-result.json does not record outcome=pass")
grep -q '"mode": "enforcing"' "${_GATE_ROOT}/scan-result.json" \
  || FAILURES+=("${CURRENT_NAME}: enforcing is not the default mode")
end_scenario; fi

if scenario "gate-policy-violation-fails"; then
_run_gate "${TMP_DIR}/dirty-report.json" "critical,high" > "${TMP_DIR}/out" 2>&1 ; rc=$?
cat "${TMP_DIR}/out"
[ "${rc}" -eq 2 ] || FAILURES+=("${CURRENT_NAME}: expected exit 2 on a policy violation, got ${rc}")
_must_contain "✗ FAIL"
grep -q '"outcome": "fail"' "${_GATE_ROOT}/scan-result.json" \
  || FAILURES+=("${CURRENT_NAME}: scan-result.json does not record outcome=fail")
end_scenario; fi

if scenario "gate-advisory-is-an-explicit-setting"; then
# The same violating report, with the operator having asked for advisory
# reporting. Recorded as a fail, job not failed.
_run_gate "${TMP_DIR}/dirty-report.json" "critical" SCAN_ADVISORY=true > "${TMP_DIR}/out" 2>&1 ; rc=$?
cat "${TMP_DIR}/out"
[ "${rc}" -eq 0 ] || FAILURES+=("${CURRENT_NAME}: advisory mode must not fail the job, got ${rc}")
_must_contain "ADVISORY"
grep -q '"outcome": "fail"' "${_GATE_ROOT}/scan-result.json" \
  || FAILURES+=("${CURRENT_NAME}: advisory mode must still record the verdict")
end_scenario; fi

if scenario "gate-invalid-severity-rejected"; then
_run_gate "${TMP_DIR}/clean-report.json" "critical,kritical" > "${TMP_DIR}/out" 2>&1 ; rc=$?
cat "${TMP_DIR}/out"
[ "${rc}" -eq 1 ] || FAILURES+=("${CURRENT_NAME}: expected exit 1 on an unknown severity, got ${rc}")
_must_contain "names a severity no scanner reports"
end_scenario; fi

if scenario "gate-missing-report-fails"; then
_run_gate "${TMP_DIR}/no-such-report.json" "critical" > "${TMP_DIR}/out" 2>&1 ; rc=$?
cat "${TMP_DIR}/out"
[ "${rc}" -ne 0 ] || FAILURES+=("${CURRENT_NAME}: a missing report must fail the gate")
_must_contain "left no report"
end_scenario; fi

if scenario "gate-malformed-report-fails"; then
_run_gate "${TMP_DIR}/malformed-report.json" "critical" > "${TMP_DIR}/out" 2>&1 ; rc=$?
cat "${TMP_DIR}/out"
[ "${rc}" -ne 0 ] || FAILURES+=("${CURRENT_NAME}: a malformed report must fail the gate")
_must_contain "not valid JSON"
end_scenario; fi

if scenario "gate-missing-jq-fails-enforcing"; then
# jq off PATH used to print a warning and skip the gate. A gate that
# cannot read its own report is not a gate.
NOJQ="${TMP_DIR}/nojq-bin"; rm -rf "${NOJQ}"; mkdir -p "${NOJQ}"
for _b in bash sh tr date cat sed grep wc mktemp rm; do
  _p="$(command -v "${_b}" 2>/dev/null)" && ln -sf "${_p}" "${NOJQ}/${_b}"
done
env -i HOME="${HOME}" PATH="${NOJQ}" \
  TEMPLATE_ROOT="${TEMPLATE_ROOT}" PROJECT_ROOT="${TMP_DIR}" \
  SCAN_ARTIFACT_ROOT="${_GATE_ROOT}" \
  bash "${TMP_DIR}/gate-driver.sh" "${TMP_DIR}/clean-report.json" critical \
  > "${TMP_DIR}/out" 2>&1 ; rc=$?
cat "${TMP_DIR}/out"
[ "${rc}" -ne 0 ] || FAILURES+=("${CURRENT_NAME}: missing jq must fail an enforcing gate")
_must_contain "jq is not on PATH"
_must_not_contain "PASS"
end_scenario; fi

if scenario "scan-subject-built-without-a-build-record-fails"; then
# A postscan job that never received the build's artifacts. It has to
# say so, not scan the upstream and call that a pass.
_P="${TMP_DIR}/proj-built-norecord"; rm -rf "${_P}"; mkdir -p "${_P}"
printf 'UPSTREAM_REF="docker.io/library/nginx:1.25.3-alpine"\nVENDOR="example.com"\n' > "${_P}/image.env"
out=$(env -i HOME="$HOME" PATH="$PATH" LOAD_ENV_LOG=false \
  PROJECT_ROOT="${_P}" SCAN_SUBJECT=built ./scripts/scan/syft-sbom.sh 2>&1) ; rc=$?
echo "${out}" > "${TMP_DIR}/out"
echo "${out}" | head -6
[ "${rc}" -ne 0 ] || FAILURES+=("${CURRENT_NAME}: expected non-zero exit with no build record")
_must_contain "SCAN_SUBJECT=built, but this job holds no build record"
_must_not_contain "docker.io/library/nginx:1.25.3-alpine"
rm -rf "${_P}"
end_scenario; fi

# A project root holding a checkable build record plus an SBOM, ready
# for the scanner. $2 is the digest the SBOM document claims as its
# subject, $3 the one its subject.json records.
_evidence_project() {
  local p="${TMP_DIR}/$1" sbom_digest="$2" subject_digest="$3"
  rm -rf "${p}"; mkdir -p "${p}/artifacts/demo"
  cp "${SNAPSHOT}" "${p}/image.env"
  _make_oci_archive "${p}/image.oci.tar" "${_D2}"
  printf 'IMAGE_TRANSPORT=oci-archive\nIMAGE_ARCHIVE=image.oci.tar\nIMAGE_DIGEST=%s\nIMAGE_REF=oci-archive:image.oci.tar\nIMAGE_NAME=demo\n' \
    "${_D2}" > "${p}/build.env"
  printf '{"bomFormat":"CycloneDX","metadata":{"component":{"type":"container","name":"demo","version":"%s"}},"components":[]}' \
    "${sbom_digest}" > "${p}/artifacts/demo/sbom.cdx.json"
  printf '{"schema_version":1,"reference":"oci-archive:image.oci.tar","digest":"%s","transport":"oci-archive","subject_kind":"manifest","archive_sha256":"","scan_subject":"built"}' \
    "${subject_digest}" > "${p}/artifacts/demo/subject.json"
  printf '%s' "${p}"
}

# _run_grype <project-root> [KEY=VALUE …]
_run_grype() {
  local p="$1"; shift
  env -i HOME="$HOME" PATH="${GRYPE_STUB}:$PATH" LOAD_ENV_LOG=false \
    PROJECT_ROOT="${p}" SCAN_SUBJECT=built "$@" ./scripts/scan/grype-vuln.sh 2>&1
}

if scenario "gate-scores-the-sbom-that-matches-the-build"; then
_P="$(_evidence_project proj-ev-ok "${_D2}" "${_D2}")"
out=$(_run_grype "${_P}" STUB_GRYPE_REPORT='{"matches":[]}' GRYPE_FAIL_ON_SEVERITY=critical) ; rc=$?
echo "${out}" > "${TMP_DIR}/out"
echo "${out}" | tail -8
[ "${rc}" -eq 0 ] || FAILURES+=("${CURRENT_NAME}: expected exit 0, got ${rc}")
_must_contain "✓ SBOM subject is ${_D2}"
_must_contain "✓ PASS"
[ -s "${_P}/artifacts/demo/scan-result.json" ] \
  || FAILURES+=("${CURRENT_NAME}: no scan-result.json under the instance root")
end_scenario; fi

if scenario "gate-sbom-subject-mismatch-fails"; then
# The SBOM's own subject record names a different image than the one
# this job built. Scoring it would gate the wrong image.
_P="$(_evidence_project proj-ev-subj "${_D2}" "${_D3}")"
out=$(_run_grype "${_P}" STUB_GRYPE_REPORT='{"matches":[]}') ; rc=$?
echo "${out}" > "${TMP_DIR}/out"
echo "${out}" | tail -6
[ "${rc}" -ne 0 ] || FAILURES+=("${CURRENT_NAME}: a subject mismatch must fail")
_must_contain "records digest ${_D3}"
_must_not_contain "PASS"
end_scenario; fi

if scenario "gate-cyclonedx-subject-mismatch-fails"; then
# subject.json agrees, but the SBOM document itself names another image.
_P="$(_evidence_project proj-ev-cdx "${_D3}" "${_D2}")"
out=$(_run_grype "${_P}" STUB_GRYPE_REPORT='{"matches":[]}') ; rc=$?
echo "${out}" > "${TMP_DIR}/out"
echo "${out}" | tail -6
[ "${rc}" -ne 0 ] || FAILURES+=("${CURRENT_NAME}: a CycloneDX subject mismatch must fail")
_must_contain "subject component is ${_D3}"
end_scenario; fi

if scenario "gate-scanner-error-is-not-a-pass"; then
# The scanner exits non-zero. There is no report, so there is no verdict.
_P="$(_evidence_project proj-ev-rc "${_D2}" "${_D2}")"
out=$(_run_grype "${_P}" STUB_GRYPE_RC=3) ; rc=$?
echo "${out}" > "${TMP_DIR}/out"
echo "${out}" | tail -4
[ "${rc}" -ne 0 ] || FAILURES+=("${CURRENT_NAME}: a failed scanner run must fail the job")
_must_contain "the scan did not complete"
end_scenario; fi

if scenario "two-instances-get-two-artifact-roots"; then
# Two images in one pipeline. Their evidence has to land in two places,
# or the second one silently overwrites the first one's verdict.
_A="${TMP_DIR}/proj-inst-a"; _B="${TMP_DIR}/proj-inst-b"
mkdir -p "${_A}" "${_B}"
cp "${SNAPSHOT}" "${_A}/image.env"; cp "${SNAPSHOT}" "${_B}/image.env"
printf 'IMAGE_NAME=alpha\n' > "${_A}/build.env"
printf 'IMAGE_NAME=beta\n'  > "${_B}/build.env"
{
  env -i HOME="$HOME" PATH="$PATH" LOAD_ENV_LOG=false TEMPLATE_ROOT="${TEMPLATE_ROOT}" \
    PROJECT_ROOT="${_A}" bash "${TMP_DIR}/paths-driver.sh"
  env -i HOME="$HOME" PATH="$PATH" LOAD_ENV_LOG=false TEMPLATE_ROOT="${TEMPLATE_ROOT}" \
    PROJECT_ROOT="${_B}" bash "${TMP_DIR}/paths-driver.sh"
} > "${TMP_DIR}/out" 2>&1
grep '^PATHS' "${TMP_DIR}/out"
_must_contain "PATHS artifacts/alpha/sbom.cdx.json artifacts/alpha/vuln-scan.json"
_must_contain "PATHS artifacts/beta/sbom.cdx.json artifacts/beta/vuln-scan.json"
{ [ -d "${_A}/artifacts/alpha" ] && [ -d "${_B}/artifacts/beta" ]; } \
  || FAILURES+=("${CURRENT_NAME}: per-instance roots were not created")
end_scenario; fi

if scenario "prescan-and-postscan-do-not-share-a-root"; then
# A prescan describes the image we rebuild FROM. Same pipeline, same
# image name, different subject, so different evidence.
_P="${TMP_DIR}/proj-presub"; mkdir -p "${_P}"
cp "${SNAPSHOT}" "${_P}/image.env"
printf 'IMAGE_NAME=alpha\n' > "${_P}/build.env"
env -i HOME="$HOME" PATH="$PATH" LOAD_ENV_LOG=false TEMPLATE_ROOT="${TEMPLATE_ROOT}" \
  PROJECT_ROOT="${_P}" SCAN_SUBJECT=upstream bash "${TMP_DIR}/paths-driver.sh" \
  > "${TMP_DIR}/out" 2>&1
cat "${TMP_DIR}/out"
_must_contain "PATHS artifacts/alpha-upstream/sbom.cdx.json"
end_scenario; fi

# ════════════════════════════════════════════════════════════════════
# Promotion — scripts/publish/promote.sh
# ════════════════════════════════════════════════════════════════════
# Promotion copies bytes that already passed the gates. Every scenario
# here is a way for the released image to stop being that image: a copy
# that landed on something else, evidence that is missing, failed, or
# about a different digest, or a build that never produced a candidate
# a registry could copy from.
#
# The crane stub records its argv, so the assertions read what promote.sh
# actually asked the registry for — the source is a digest reference, the
# read-back is the destination, and the password never appears on a
# command line.

PROMO_STUB="${TMP_DIR}/promote-stub"; mkdir -p "${PROMO_STUB}"
cat > "${PROMO_STUB}/crane" <<'STUB'
#!/bin/sh
printf 'CRANE %s\n' "$*" >> "${PROMOTE_LOG:-/dev/null}"
case "$1" in
  auth)   cat >/dev/null; exit 0 ;;   # drain --password-stdin like real crane, or printf dies on EPIPE under pipefail
  copy)   [ -n "${STUB_COPY_FAIL:-}" ] && exit 1; exit 0 ;;
  digest) printf '%s\n' "${STUB_DEST_DIGEST:-}" ;;
  *)      exit 1 ;;
esac
STUB
chmod +x "${PROMO_STUB}/crane"

# The branch-head check reads the branch through git, so a bare repository
# stands in for the project. _PROMO_HEAD is its release/1.x head.
_PROMO_GIT="${TMP_DIR}/promote-origin.git"
git init -q --bare "${_PROMO_GIT}"
( cd "${TMP_DIR}" && rm -rf promote-src && git init -q promote-src \
  && cd promote-src && git -c user.name=t -c user.email=t@t commit -q --allow-empty -m head \
  && git push -q "${_PROMO_GIT}" HEAD:refs/heads/release/1.x )
_PROMO_HEAD="$(git --git-dir="${_PROMO_GIT}" rev-parse refs/heads/release/1.x)"

PROMO_SRC="candidate.example.com/team/nginx"
PROMO_DST="release.example.com/prod/nginx"

# A promotion-ready project root: a registry candidate plus the evidence
# both mandatory gates leave behind. The three tail arguments are what
# each failure scenario bends — the SBOM's subject, the gate's outcome,
# and the digest the gate says it scored.
_promote_fixture() {
  local proj="$1" digest="$2"
  local subject_digest="${3:-$2}" outcome="${4:-pass}" result_digest="${5:-$2}"
  rm -rf "${proj:?}"
  mkdir -p "${proj}/artifacts/nginx"
  cp "${SNAPSHOT}" "${proj}/image.env"
  cat > "${proj}/image.json" <<EOF
{"schema_version": 1,
 "repository": "${PROMO_SRC}",
 "digest": "${digest}",
 "reference": "${PROMO_SRC}@${digest}",
 "tag": "1.25.3-alpine",
 "tag_reference": "${PROMO_SRC}:1.25.3-alpine",
 "transport": "registry",
 "subject_kind": "manifest",
 "platforms": ["linux/amd64"]}
EOF
  printf '{"schema_version":1,"digest":"%s","reference":"%s@%s"}\n' \
    "${subject_digest}" "${PROMO_SRC}" "${subject_digest}" > "${proj}/artifacts/nginx/subject.json"
  printf '{"bomFormat":"CycloneDX","components":[]}\n' > "${proj}/artifacts/nginx/sbom.cdx.json"
  printf '{"matches":[]}\n'                            > "${proj}/artifacts/nginx/vuln-scan.json"
  printf '{"schema_version":1,"outcome":"%s","subject":{"digest":"%s"}}\n' \
    "${outcome}" "${result_digest}" > "${proj}/artifacts/nginx/scan-result.json"
}

# Run promote.sh against the stub, folding the recorded argv into the
# assertion buffer. $1 = project root, rest = extra KEY=VAL env.
_promote_run() {
  local proj="$1"; shift
  : > "${TMP_DIR}/promote.log"
  out=$(env -i HOME="$HOME" PATH="${PROMO_STUB}:$PATH" LOAD_ENV_LOG=false \
    PROMOTE_LOG="${TMP_DIR}/promote.log" PROJECT_ROOT="${proj}" \
    CI_PIPELINE_ID=4242 CI_JOB_ID=9001 CI_COMMIT_SHA=abc123 \
    RELEASE_REGISTRY="release.example.com" RELEASE_REPOSITORY="prod/nginx" \
    RELEASE_REGISTRY_USER="release-bot" RELEASE_REGISTRY_PASSWORD="s3cret" \
    "$@" bash "${TEMPLATE_ROOT}/scripts/publish/promote.sh" 2>&1) ; local rc=$?
  { printf '%s\n' "${out}"; cat "${TMP_DIR}/promote.log"; } > "${TMP_DIR}/out"
  return "${rc}"
}

_PROMO_P="${TMP_DIR}/promote-proj"

if scenario "promote-copies-by-digest-and-verifies-the-destination"; then
# The whole contract in one run: the source is the digest reference, both
# destination forms get the same bytes, and the destination's own answer
# is read back before anything is called released.
_promote_fixture "${_PROMO_P}" "${_D2}"
_promote_run "${_PROMO_P}" STUB_DEST_DIGEST="${_D2}" ; rc=$?
grep -E '^CRANE|Promoted' "${TMP_DIR}/out"
[ "${rc}" -eq 0 ] || FAILURES+=("${CURRENT_NAME}: expected exit 0, got ${rc}")
_must_contain "CRANE copy ${PROMO_SRC}@${_D2} ${PROMO_DST}:1.25.3-alpine"
_must_contain "CRANE copy ${PROMO_SRC}@${_D2} ${PROMO_DST}@${_D2}"
_must_contain "CRANE digest ${PROMO_DST}:1.25.3-alpine"
_must_contain "Promoted: ${PROMO_DST}:1.25.3-alpine (${_D2})"
# Release credentials only, and the secret goes over stdin, never argv.
_must_contain "CRANE auth login release.example.com -u release-bot --password-stdin"
_must_not_contain "s3cret"
# Promotion is a copy. Nothing here rebuilds, and nothing re-pushes.
_must_not_contain "docker build"
_must_not_contain "docker push"
end_scenario; fi

if scenario "promote-release-json-records-both-ends"; then
# The release record is the audit answer to "what shipped, and on what
# evidence". Assert the fields rather than the file's existence.
_promote_fixture "${_PROMO_P}" "${_D2}"
_promote_run "${_PROMO_P}" STUB_DEST_DIGEST="${_D2}" ; rc=$?
[ "${rc}" -eq 0 ] || FAILURES+=("${CURRENT_NAME}: expected exit 0, got ${rc}")
_REL="${_PROMO_P}/artifacts/nginx/release.json"
if [ -s "${_REL}" ]; then
  {
    jq -r 'paths(scalars) | join(".")' "${_REL}" | sed 's/^/FIELD /' | sort -u
    jq -r '"SOURCE      " + .source.reference,
           "SOURCEDIG   " + .source.digest,
           "DEST        " + .destination.reference,
           "DESTDIG     " + .destination.digest,
           "GATES       " + (.gates | map(.name + "=" + .outcome) | join(",")),
           "SIGNATURE   " + .signature.mode,
           "PIPELINE    " + .pipeline_id + "/" + .job_id,
           "EVIDENCE    " + (.evidence | map(.path) | join(",")),
           "HASHES      " + (.evidence | map(.sha256 | length | tostring) | join(","))' "${_REL}"
  } > "${TMP_DIR}/out" 2>&1
  cat "${TMP_DIR}/out"
  _must_contain "SOURCE      ${PROMO_SRC}@${_D2}"
  _must_contain "SOURCEDIG   ${_D2}"
  _must_contain "DEST        ${PROMO_DST}:1.25.3-alpine"
  _must_contain "DESTDIG     ${_D2}"
  _must_contain "GATES       sbom-subject=pass,vuln-scan=pass"
  _must_contain "SIGNATURE   disabled"
  _must_contain "PIPELINE    4242/9001"
  _must_contain "EVIDENCE    artifacts/nginx/subject.json,artifacts/nginx/sbom.cdx.json,artifacts/nginx/scan-result.json,artifacts/nginx/vuln-scan.json"
  _must_contain "HASHES      64,64,64,64"
  _must_contain "FIELD source_commit"
  _must_contain "FIELD promoted_at"
else
  FAILURES+=("${CURRENT_NAME}: no release.json at ${_REL}")
fi
end_scenario; fi

if scenario "promote-destination-digest-mismatch-fails"; then
# crane said the copy worked; the destination resolves to something else.
# The destination's answer wins, and nothing is called released.
_promote_fixture "${_PROMO_P}" "${_D2}"
_promote_run "${_PROMO_P}" STUB_DEST_DIGEST="${_D3}" ; rc=$?
tail -4 "${TMP_DIR}/out"
[ "${rc}" -ne 0 ] || FAILURES+=("${CURRENT_NAME}: expected non-zero exit")
_must_contain "does not hold the image the gates passed"
_must_not_contain "Promoted:"
[ -e "${_PROMO_P}/artifacts/nginx/release.json" ] \
  && FAILURES+=("${CURRENT_NAME}: wrote a release record for a copy that did not verify")
end_scenario; fi

if scenario "promote-missing-scan-result-fails"; then
# Missing evidence is not a pass. A gate whose artifacts never reached
# this job has not said anything about this image.
_promote_fixture "${_PROMO_P}" "${_D2}"
rm -f "${_PROMO_P:?}/artifacts/nginx/scan-result.json"
_promote_run "${_PROMO_P}" STUB_DEST_DIGEST="${_D2}" ; rc=$?
tail -3 "${TMP_DIR}/out"
[ "${rc}" -ne 0 ] || FAILURES+=("${CURRENT_NAME}: expected non-zero exit")
_must_contain "no vulnerability gate result"
_must_not_contain "CRANE copy"
end_scenario; fi

if scenario "promote-failed-gate-blocks-the-copy"; then
# outcome=fail is a verdict, and it is the one that counts.
_promote_fixture "${_PROMO_P}" "${_D2}" "${_D2}" "fail"
_promote_run "${_PROMO_P}" STUB_DEST_DIGEST="${_D2}" ; rc=$?
tail -3 "${TMP_DIR}/out"
[ "${rc}" -ne 0 ] || FAILURES+=("${CURRENT_NAME}: expected non-zero exit")
_must_contain "vulnerability gate outcome is 'fail'"
_must_not_contain "CRANE copy"
end_scenario; fi

if scenario "promote-evidence-about-another-digest-blocks"; then
# Evidence that passed, about a different image. Either half of the
# binding — the SBOM's subject or the gate's own subject — is enough to
# stop the release.
_promote_fixture "${_PROMO_P}" "${_D2}" "${_D3}"
_promote_run "${_PROMO_P}" STUB_DEST_DIGEST="${_D2}" ; rc=$?
[ "${rc}" -ne 0 ] || FAILURES+=("${CURRENT_NAME}: expected non-zero exit on an SBOM subject mismatch")
_must_contain "The SBOM describes a different image"
_must_not_contain "CRANE copy"
_promote_fixture "${_PROMO_P}" "${_D2}" "${_D2}" "pass" "${_D3}"
_promote_run "${_PROMO_P}" STUB_DEST_DIGEST="${_D2}" ; rc=$?
tail -3 "${TMP_DIR}/out"
[ "${rc}" -ne 0 ] || FAILURES+=("${CURRENT_NAME}: expected non-zero exit on a gate subject mismatch")
_must_contain "The verdict belongs to a different image"
_must_not_contain "CRANE copy"
end_scenario; fi

if scenario "promote-archive-only-candidate-is-refused"; then
# No candidate registry means no bytes to copy from. Promotion never
# rebuilds and never pushes an archive, so it says so and stops.
_promote_fixture "${_PROMO_P}" "${_D2}"
jq '.transport = "oci-archive" | .reference = "oci-archive:image.oci.tar" | .repository = "nginx"' \
  "${_PROMO_P}/image.json" > "${_PROMO_P}/image.json.tmp"
mv "${_PROMO_P}/image.json.tmp" "${_PROMO_P}/image.json"
_promote_run "${_PROMO_P}" STUB_DEST_DIGEST="${_D2}" ; rc=$?
tail -5 "${TMP_DIR}/out"
[ "${rc}" -ne 0 ] || FAILURES+=("${CURRENT_NAME}: expected non-zero exit")
_must_contain "this build produced an OCI archive, not a pushed candidate"
_must_not_contain "CRANE copy"
end_scenario; fi

if scenario "promote-without-a-release-credential-is-refused"; then
# The credentials are protected CI variables: on an unprotected ref they
# arrive empty. Copying anonymously would either fail late or push to a
# world-writable registry, so the job stops before crane is called.
_promote_fixture "${_PROMO_P}" "${_D2}"
_promote_run "${_PROMO_P}" STUB_DEST_DIGEST="${_D2}" \
  RELEASE_REGISTRY_USER="" RELEASE_REGISTRY_PASSWORD="" ; rc=$?
tail -5 "${TMP_DIR}/out"
[ "${rc}" -ne 0 ] || FAILURES+=("${CURRENT_NAME}: expected non-zero exit")
_must_contain "CBT_RELEASE_REGISTRY_USER"
_must_contain "CBT_RELEASE_REGISTRY_PASSWORD"
_must_not_contain "CRANE copy"
end_scenario; fi

if scenario "promote-refuses-a-pipeline-that-is-not-the-branch-head"; then
# Publish jobs are retryable and the resource group does not order them,
# so an old pipeline's retry can run last and put an older digest on the
# release tag. The branch head decides which pipeline is still current.
_promote_fixture "${_PROMO_P}" "${_D2}"
_promote_run "${_PROMO_P}" STUB_DEST_DIGEST="${_D2}" \
  CI_COMMIT_BRANCH="release/1.x" CI_REPOSITORY_URL="${_PROMO_GIT}" ; rc=$?
tail -5 "${TMP_DIR}/out"
[ "${rc}" -ne 0 ] || FAILURES+=("${CURRENT_NAME}: expected non-zero exit on a stale pipeline")
_must_contain "release/1.x is now at ${_PROMO_HEAD}"
_must_not_contain "CRANE copy"
# Same pipeline, still the head: the copy goes ahead.
_promote_fixture "${_PROMO_P}" "${_D2}"
_promote_run "${_PROMO_P}" STUB_DEST_DIGEST="${_D2}" \
  CI_COMMIT_BRANCH="release/1.x" CI_REPOSITORY_URL="${_PROMO_GIT}" CI_COMMIT_SHA="${_PROMO_HEAD}" ; rc=$?
[ "${rc}" -eq 0 ] || FAILURES+=("${CURRENT_NAME}: expected exit 0 at the branch head, got ${rc}")
_must_contain "Promoted: ${PROMO_DST}:1.25.3-alpine (${_D2})"
# Outside CI there is no repository URL, and the check says so rather
# than blocking a local promotion.
_promote_fixture "${_PROMO_P}" "${_D2}"
_promote_run "${_PROMO_P}" STUB_DEST_DIGEST="${_D2}" CI_COMMIT_BRANCH="main" ; rc=$?
[ "${rc}" -eq 0 ] || FAILURES+=("${CURRENT_NAME}: expected exit 0 without CI_REPOSITORY_URL, got ${rc}")
_must_contain "branch-head check is skipped"
end_scenario; fi

# ════════════════════════════════════════════════════════════════════
# The composition files
# ════════════════════════════════════════════════════════════════════

# include:remote fetches one file and gives its nested includes no project
# context, so a consumer on another host includes pipelines/container.flat.yml
# instead. That file is generated: a template edited without regenerating it
# would ship a flat form describing the previous behaviour.
if scenario "flat-composition-is-regenerated"; then
bash "${TEMPLATE_ROOT}/scripts/ci/flatten.sh" > "${TMP_DIR}/flat.yml"
_run diff -u "${TEMPLATE_ROOT}/pipelines/container.flat.yml" "${TMP_DIR}/flat.yml" ; rc=$?
[ "${rc}" -eq 0 ] || FAILURES+=("${CURRENT_NAME}: pipelines/container.flat.yml is stale — regenerate with: bash scripts/ci/flatten.sh --write")
end_scenario; fi

# Every job locates the template scripts with the same block. One that drifts
# is a job that cannot find scripts/ when the others can, or one that clones
# with a token where another clones from a URL.
if scenario "clone-block-is-identical-in-every-job"; then
_run python3 -c "
import glob, re
files = ['${TEMPLATE_ROOT}/pipelines/container.yml'] + sorted(glob.glob('${TEMPLATE_ROOT}/templates/*/template.yml'))
pat = re.compile(r'^      # Where the template scripts come from.*?^      export CBT\$', re.M | re.S)
blocks = [m.group(0) for f in files for m in pat.finditer(open(f).read())]
print('clone blocks: %d, distinct: %d' % (len(blocks), len(set(blocks))))
" ; rc=$?
[ "${rc}" -eq 0 ] || FAILURES+=("${CURRENT_NAME}: block extraction failed with ${rc}")
_must_contain "clone blocks: 7, distinct: 1"
end_scenario; fi

# ════════════════════════════════════════════════════════════════════
# Summary
# ════════════════════════════════════════════════════════════════════

printf '\n══════════════════════════════════════════════════════════════════\n'
printf '  Regression summary\n'
printf '══════════════════════════════════════════════════════════════════\n'
printf '  Passed:  %d\n' "${PASSES}"
printf '  Failed:  %d\n' "${#FAILURES[@]}"
[ "${SKIPPED}" -gt 0 ] && printf '  Skipped: %d (filter: %s)\n' "${SKIPPED}" "${FILTER}"
if [ "${#FAILURES[@]}" -gt 0 ]; then
  printf '\nFailures:\n'
  for f in "${FAILURES[@]}"; do
    printf '  - %s\n' "${f}"
  done
  exit 1
fi
# A mistyped filter matches nothing. Without this, that run prints
# ALL PASS and exits 0 having tested nothing at all.
if [ "${PASSES}" -eq 0 ]; then
  printf '  NO SCENARIOS RAN — filter %s matched nothing\n' "${FILTER}" >&2
  exit 1
fi
echo "  ALL PASS"
exit 0
