#!/usr/bin/env bash
# ─── DO NOT EDIT — template lib (shared by scripts/scan/*.sh) ───────
# Generic scan bootstrap + scan-target resolution. Contains NO tool-
# specific logic — every scan producer calls these the same way, so
# deleting any one scan script never touches this file and never affects
# the others. Override-var names are passed in as ARGUMENTS, so the
# resolution order is the caller's to decide.
# ───────────────────────────────────────────────────────────────────
# shellcheck disable=SC2148

# scan_bootstrap — the preamble every scan script shares.
# Sources load-image-env + artifact-names + image-identity, imports
# Bamboo plan vars, loads image.env, then self-sources build.env (the
# identity of the image the build produced) so build.sh → scan needs no
# manual `. build.env`. Requires TEMPLATE_ROOT + PROJECT_ROOT already
# exported by the caller.
scan_bootstrap() {
  # Capture operator-set output filenames BEFORE artifact-names.sh defaults
  # them, so an explicit shell/CI override survives the build.env self-source
  # below (build.env carries the canonical names and would otherwise clobber
  # them — defeating distinct per-producer outputs in a multi-scanner run).
  local __sbom_override="${SBOM_FILE:-}" __vuln_override="${VULN_SCAN_FILE:-}"
  # shellcheck source=./load-image-env.sh
  . "${TEMPLATE_ROOT}/scripts/lib/load-image-env.sh"
  # shellcheck source=./artifact-names.sh
  . "${TEMPLATE_ROOT}/scripts/lib/artifact-names.sh"
  # shellcheck source=./image-identity.sh
  . "${TEMPLATE_ROOT}/scripts/lib/image-identity.sh"
  import_bamboo_vars
  load_image_env
  if [ -f build.env ]; then set -a; . ./build.env; set +a; fi
  # build.env supplies build OUTPUTS (IMAGE_DIGEST/IMAGE_REF/…); but an
  # explicitly-overridden output FILENAME wins over its canonical default.
  [ -n "${__sbom_override}" ] && export SBOM_FILE="${__sbom_override}"
  [ -n "${__vuln_override}" ] && export VULN_SCAN_FILE="${__vuln_override}"
  # IMAGE_NAME is known by now (build.env for a postscan, image.env for a
  # prescan), so the outputs can be rooted at this image instance.
  artifact_paths
  return 0   # never let a falsy build.env test trip the caller's `set -e`
}

# resolve_scan_ref <positional-$1> [override-var-name ...]
# Echoes the resolved scan target on stdout, or prints the standard
# error block to stderr and returns 1. Precedence:
#   $1 > each named override var (in the order given)
#      > the image this pipeline built, from its identity record
#      > UPSTREAM_REF (prescan only — when there is no record at all)
#      > UPSTREAM_REGISTRY/UPSTREAM_IMAGE:UPSTREAM_TAG (assembled)
#
# SCAN_SUBJECT says which image the job is about, and CI sets it on
# every scan job:
#   built      the image this pipeline produced. No build record, or one
#              that does not check out, fails the job. There is no step
#              to the upstream, so a postscan cannot report a pass for an
#              image nobody is going to run.
#   upstream   the image we rebuild FROM. The build record, if a stale
#              one is lying around, is ignored.
#   unset      the chain below, so a manual run off a laptop still works.
#
# There is no step from broken build evidence back to the upstream. A
# postscan that quietly scanned the upstream would report on an image
# nobody is going to run, and report it as a pass.
#
# The archive transport is a file on disk and needs no daemon, so a
# producer that reads a local image archive consumes it as-is. One that
# can only take a registry reference belongs on a pipeline that pushes.
resolve_scan_ref() {
  local ref="$1"; shift
  local v chain='$1'
  for v in "$@"; do
    chain="${chain} > ${v}"
    [ -z "${ref}" ] && [ -n "${!v:-}" ] && ref="${!v}"
  done

  if [ "${SCAN_SUBJECT:-}" = "built" ]; then
    if [ -z "${IMAGE_TRANSPORT:-}" ]; then
      echo "ERROR: SCAN_SUBJECT=built, but this job holds no build record." >&2
      echo "  build.env and image.json name the image the build produced. Without" >&2
      echo "  them there is nothing this job is entitled to call the subject, so it" >&2
      echo "  fails instead of scanning the upstream image, which is a different" >&2
      echo "  image. Check the job fetched the build job's artifacts." >&2
      return 1
    fi
    if [ -n "${ref}" ]; then
      echo "ERROR: SCAN_SUBJECT=built takes the subject from the build record, but" >&2
      echo "  an explicit reference was given ('${ref}'). Drop it, or set" >&2
      echo "  SCAN_SUBJECT=upstream to scan something else on purpose." >&2
      return 1
    fi
  fi

  if [ -n "${ref}" ]; then
    printf '%s' "${ref}"
    return 0
  fi

  # The build handed something over: that IS the subject, and a record
  # that does not check out fails the job.
  if [ -n "${IMAGE_TRANSPORT:-}" ] && [ "${SCAN_SUBJECT:-}" != "upstream" ]; then
    verify_image_identity || return 1
    case "${IMAGE_TRANSPORT}" in
      oci-archive) printf 'oci-archive:%s' "${IMAGE_ARCHIVE}"; return 0 ;;
      registry)    printf '%s' "${IMAGE_REF}";                 return 0 ;;
    esac
  fi

  # Build outputs without a transport: an older or half-written build.env.
  # Its IMAGE_REF could name anything, so it does not get to stand in.
  if [ "${SCAN_SUBJECT:-}" != "upstream" ] \
     && { [ -n "${IMAGE_REF:-}" ] || [ -n "${IMAGE_DIGEST:-}" ]; }; then
    echo "ERROR: build.env names an image but no IMAGE_TRANSPORT." >&2
    echo "  IMAGE_REF=${IMAGE_REF:-<empty>} IMAGE_DIGEST=${IMAGE_DIGEST:-<empty>}" >&2
    echo "  That record predates this template's identity contract — rerun the build" >&2
    echo "  rather than scanning an image this pipeline cannot vouch for." >&2
    return 1
  fi

  # No build evidence at all — a prescan, which is about the upstream.
  if   [ -n "${UPSTREAM_REF:-}" ]; then ref="${UPSTREAM_REF}"
  elif [ -n "${UPSTREAM_REGISTRY:-}" ] && [ -n "${UPSTREAM_IMAGE:-}" ] && [ -n "${UPSTREAM_TAG:-}" ]; then
    ref="${UPSTREAM_REGISTRY}/${UPSTREAM_IMAGE}:${UPSTREAM_TAG}"
  fi
  if [ -z "${ref}" ]; then
    echo "ERROR: no scan target available." >&2
    echo "  Resolution chain: ${chain} > built image (build.env/image.json) > UPSTREAM_REF > UPSTREAM_REGISTRY/IMAGE:TAG" >&2
    echo "  All empty. To scan after build, ensure build.env and its image.json are on disk." >&2
    echo "  To scan upstream as a prescan, set UPSTREAM_REF in image.env, or pass a ref explicitly." >&2
    return 1
  fi
  printf '%s' "${ref}"
}

# ── Severity vocabulary ─────────────────────────────────────────────
# Every scanner's severities, normalised to lower case. A policy naming
# anything outside this list is a typo, and a typo that silently gates
# on nothing is worse than no gate.
SCAN_SEVERITIES='critical high medium low negligible unknown'
SCAN_SEVERITIES_JSON='["critical","high","medium","low","negligible","unknown"]'

scan_require_jq() {
  command -v jq >/dev/null 2>&1 && return 0
  echo "ERROR: jq is not on PATH, and ${1:-this step} needs it." >&2
  echo "       Install it (alpine: apk add jq). CI's .install-tools anchor does." >&2
  echo "       A gate that cannot read its own report does not get to pass." >&2
  return 1
}

# scan_write_subject <scan-ref>
# Writes subject.json beside the evidence: the identity the scan was
# actually run against. Downstream consumers compare THIS, not a
# filename, which is why every producer writes it.
scan_write_subject() {
  local ref="$1" digest transport kind archive_sha kindofsubject
  scan_require_jq "writing subject.json" || return 1
  if [ -n "${IMAGE_TRANSPORT:-}" ] \
     && { [ "${ref}" = "${IMAGE_REF:-}" ] || [ "${ref}" = "oci-archive:${IMAGE_ARCHIVE:-}" ]; }; then
    digest="${IMAGE_DIGEST:-}"
    transport="${IMAGE_TRANSPORT}"
    kind="${IMAGE_SUBJECT_KIND:-manifest}"
    archive_sha="${IMAGE_ARCHIVE_SHA256:-}"
    kindofsubject="built"
  else
    # An upstream, an explicit reference or a source tree. The digest is
    # whatever the caller pinned, which may be nothing.
    digest="${UPSTREAM_DIGEST:-${BASE_DIGEST:-}}"
    transport="registry"
    kind="manifest"
    archive_sha=""
    kindofsubject="${SCAN_SUBJECT:-upstream}"
  fi
  jq -n \
    --arg reference  "${ref}" \
    --arg digest     "${digest}" \
    --arg transport  "${transport}" \
    --arg kind       "${kind}" \
    --arg sha        "${archive_sha}" \
    --arg subject    "${kindofsubject}" \
    --arg generated  "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
    '{schema_version: 1, reference: $reference, digest: $digest,
      transport: $transport, subject_kind: $kind, archive_sha256: $sha,
      scan_subject: $subject, generated_at: $generated}' \
    > "${SCAN_ARTIFACT_ROOT}/subject.json" || {
      echo "ERROR: could not write ${SCAN_ARTIFACT_ROOT}/subject.json" >&2
      return 1
    }
  echo "  ✓ subject: ${SCAN_ARTIFACT_ROOT}/subject.json (${digest:-no digest})"
}

# scan_verify_sbom_subject <sbom-path>
# An SBOM is evidence about one image. Before a scanner turns it into a
# verdict about the image this job built, the two have to be the same
# image: the producer's subject.json, and the CycloneDX subject
# component when it names a digest at all.
scan_verify_sbom_subject() {
  local sbom="$1" subj="${SCAN_ARTIFACT_ROOT}/subject.json" want="${IMAGE_DIGEST:-}"
  if [ -z "${want}" ]; then
    echo "→ subject binding: no built-image digest in this context, SBOM taken as given"
    return 0
  fi
  scan_require_jq "checking the SBOM's subject" || return 1

  if [ -s "${subj}" ]; then
    local have
    have="$(jq -r '.digest // ""' "${subj}" 2>/dev/null)"
    if [ "${have}" != "${want}" ]; then
      echo "ERROR: ${subj} records digest ${have:-<none>}, this job gates ${want}." >&2
      echo "  The SBOM describes a different image. Re-run the SBOM producer" >&2
      echo "  against this build rather than scoring the wrong evidence." >&2
      return 1
    fi
  elif [ "${SCAN_SUBJECT:-}" = "built" ]; then
    echo "ERROR: no subject record at ${subj}." >&2
    echo "  SCAN_SUBJECT=built means the SBOM has to prove which image it is" >&2
    echo "  about. Its producer writes subject.json next to it." >&2
    return 1
  fi

  # CycloneDX names its subject in metadata.component. Producers put the
  # manifest digest there for both transports this template hands over,
  # so when the field carries a digest at all it has to be ours.
  local in_bom
  in_bom="$(jq -r '.metadata.component.version // ""' "${sbom}" 2>/dev/null)"
  case "${in_bom}" in
    sha256:*)
      if [ "${in_bom}" != "${want}" ]; then
        echo "ERROR: the SBOM's subject component is ${in_bom}, this job gates ${want}." >&2
        return 1
      fi
      ;;
  esac
  echo "  ✓ SBOM subject is ${want}"
}

# scan_gate <tool> <version> <db-built> <report> <shape-key> <count-expr> [fail-on]
#
# The one place a scan report becomes a verdict, and the one writer of
# scan-result.json. Shared by every vuln scanner, so fixing a gate fixes
# all of them.
#
#   shape-key   a top-level key the report must carry, or '-' to skip
#               the check when the shape is the vendor's to change.
#   count-expr  a jq expression taking $s, a lower-case severity, and
#               returning how many findings that report has at it.
#   fail-on     the tool's own severity list. Empty falls back to
#               SCAN_FAIL_ON_SEVERITY, then to critical.
#
# Enforcing is the default. Advisory reporting is something an operator
# turns on (SCAN_ADVISORY=true), never something a missing jq, an
# unreadable report or a crashed scanner decides on the job's behalf.
scan_gate() {
  local tool="$1" version="$2" db_built="$3" report="$4" shape="$5" expr="$6" fail_on="${7:-}"

  local advisory=false
  case "$(printf '%s' "${SCAN_ADVISORY:-false}" | tr '[:upper:]' '[:lower:]')" in
    true|1|yes|on) advisory=true ;;
  esac

  fail_on="${fail_on:-${SCAN_FAIL_ON_SEVERITY:-critical}}"
  fail_on="$(printf '%s' "${fail_on}" | tr '[:upper:]' '[:lower:]' | tr ',' ' ')"
  local sev bad=""
  for sev in ${fail_on}; do
    case " ${SCAN_SEVERITIES} " in
      *" ${sev} "*) ;;
      *) bad="${bad} ${sev}" ;;
    esac
  done
  if [ -n "${bad}" ]; then
    echo "ERROR: the ${tool} fail-on policy names a severity no scanner reports:${bad}" >&2
    echo "  Known severities: ${SCAN_SEVERITIES}" >&2
    return 1
  fi
  if [ -z "${fail_on}" ]; then
    echo "ERROR: the ${tool} fail-on policy is empty." >&2
    echo "  Set one, or set SCAN_ADVISORY=true to report without gating." >&2
    return 1
  fi

  scan_require_jq "evaluating the ${tool} policy gate" || return 1
  if [ ! -s "${report}" ]; then
    echo "ERROR: ${tool} left no report at ${report}, so there is nothing to evaluate." >&2
    return 1
  fi
  if ! jq -e . "${report}" >/dev/null 2>&1; then
    echo "ERROR: ${report} is not valid JSON. The ${tool} run did not complete." >&2
    return 1
  fi
  if [ "${shape}" != "-" ] && ! jq -e --arg k "${shape}" 'has($k)' "${report}" >/dev/null 2>&1; then
    echo "ERROR: ${report} carries no '${shape}' key, so it is not a ${tool} report." >&2
    return 1
  fi

  local counts
  counts="$(jq -c --argjson sevs "${SCAN_SEVERITIES_JSON}" \
    '. as $doc | reduce $sevs[] as $s ({}; . + {($s): ($doc | '"${expr}"')})' \
    "${report}" 2>/dev/null)"
  if ! printf '%s' "${counts}" | jq -e 'type == "object" and (to_entries | all(.value | type == "number"))' >/dev/null 2>&1; then
    echo "ERROR: could not count findings by severity in ${report}." >&2
    return 1
  fi

  local subject
  if [ -s "${SCAN_ARTIFACT_ROOT}/subject.json" ]; then
    subject="$(cat "${SCAN_ARTIFACT_ROOT}/subject.json")"
  else
    subject="$(jq -n --arg d "${IMAGE_DIGEST:-}" --arg r "${IMAGE_REF:-}" \
                 '{digest: $d, reference: $r}')"
  fi

  local result
  result="$(jq -n \
    --arg tool "${tool}" --arg version "${version}" --arg db "${db_built}" \
    --arg mode "$([ "${advisory}" = true ] && echo advisory || echo enforcing)" \
    --argjson fail_on "$(printf '%s' "${fail_on}" | jq -R 'split(" ") | map(select(length > 0))')" \
    --argjson counts "${counts}" \
    --argjson subject "${subject}" \
    --arg report "${report##*/}" \
    --arg generated "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
    '([$fail_on[] | $counts[.]] | add // 0) as $v |
     {schema_version: 1,
      tool: {name: $tool, version: $version,
             database: {built: (if $db == "" then null else $db end)}},
      policy: {mode: $mode, fail_on: $fail_on},
      counts: $counts,
      findings_total: ($counts | add // 0),
      violations: $v,
      outcome: (if $v > 0 then "fail" else "pass" end),
      subject: $subject,
      report: $report,
      generated_at: $generated}')" || {
      echo "ERROR: could not build the ${tool} scan result record." >&2
      return 1
    }
  printf '%s\n' "${result}" | jq . > "${SCAN_ARTIFACT_ROOT}/scan-result.json" || {
    echo "ERROR: could not write ${SCAN_ARTIFACT_ROOT}/scan-result.json" >&2
    return 1
  }

  echo ""
  echo "→ ${tool} ${version:-<unknown version>}${db_built:+ (vuln DB built ${db_built})}"
  printf '%s\n' "${result}" | jq -r '.counts | to_entries[] | "    \(.key + ":" + (" " * (13 - (.key | length))))\(.value)"'
  echo "→ Policy: $(printf '%s\n' "${result}" | jq -r '.policy.mode + " fail-on=" + (.policy.fail_on | join(","))')"
  echo "  → ${SCAN_ARTIFACT_ROOT}/scan-result.json"

  local violations
  violations="$(printf '%s' "${result}" | jq -r '.violations')"
  if [ "${violations}" -gt 0 ]; then
    if [ "${advisory}" = true ]; then
      echo "  ! ADVISORY: ${violations} finding(s) match the policy. SCAN_ADVISORY=true, so the job is not failed." >&2
      return 0
    fi
    echo "  ✗ FAIL: ${violations} finding(s) match the policy gate." >&2
    return 2
  fi
  echo "  ✓ PASS: nothing at the gated severities."
}
