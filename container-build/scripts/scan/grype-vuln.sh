#!/usr/bin/env bash
# ─── DO NOT EDIT — template scan job ───────────────────────────────
# Behaviour comes from image.env (GRYPE_FAIL_ON_SEVERITY,
# GRYPE_VERSION, GRYPE_INSTALLER_URL, ARTIFACTORY_GRYPE_DB_REPO).
# Edit those, not this file.
# ───────────────────────────────────────────────────────────────────
#
# scripts/scan/grype-vuln.sh — Anchore Grype SBOM-based vulnerability scan
#
# Single responsibility: run `grype sbom:<SBOM_FILE> -o json` and
# produce the canonical vuln-scan.json. Optional severity gate
# (GRYPE_FAIL_ON_SEVERITY) parallels xray-vuln.sh's gate so swapping
# scanners doesn't break downstream policy.
#
# Output filename is the SAME as scripts/scan/xray-vuln.sh — both
# write vuln-scan.json by default. That's the artifact contract:
# downstream stages (audit shippers, SecOps) consume vuln-scan.json
# without caring which scanner produced it. Swap one for the other
# by changing the script name in CI YAML; nothing else moves.
#
# Usage:
#   bash scripts/scan/grype-vuln.sh                # uses ${SBOM_FILE} from
#                                                  # build.env / artifact-names.sh
#   bash scripts/scan/grype-vuln.sh <sbom-path>    # scan an arbitrary SBOM
#
# Required upstream input: a CycloneDX SBOM at ${SBOM_FILE} (default
# sbom.cdx.json). Produced by scripts/scan/syft-sbom.sh OR
# scripts/scan/xray-sbom.sh — Grype reads either.
#
# Optional env:
#   SBOM_FILE                 input CycloneDX SBOM (default: sbom.cdx.json)
#   VULN_SCAN_FILE            output path (default: vuln-scan.json)
#   GRYPE_INSTALLER_URL       installer URL — .sh installer OR .tar.gz release
#                             (auto-detected; default: GitHub raw install.sh)
#   GRYPE_VERSION             default v0.114.0 (used only for the .sh installer)
#   GRYPE_DB_UPDATE_URL       override CVE DB source (air-gap mirror)
#   GRYPE_FAIL_ON_SEVERITY    comma-separated severities that trigger
#                             exit 2 (case-insensitive — critical,
#                             high, medium, low, negligible, unknown).
#                             Unset falls back to SCAN_FAIL_ON_SEVERITY,
#                             then to critical. A severity outside
#                             that vocabulary is rejected, not ignored.
#   SCAN_ADVISORY             true = report the verdict without failing
#                             the job. The only way to not gate; a
#                             missing jq or an unreadable report never
#                             downgrades a gate on its own.
# Sink shipping primarily happens in scripts/ingest/vuln-post.sh as
# its own CI stage (mirrors sbom-post.sh — clean producer/consumer
# split). Optional INLINE Splunk shipping is available via
# VULN_INLINE_POST=true for callers that need scan-time delivery
# without waiting for the ingest stage. Off by default.
#   VULN_INLINE_POST="false"                  default — ingest stage only
#   VULN_INLINE_POST="true"                   ALSO ship inline here
#
# Outputs, all under the per-instance root (artifacts/<image>/):
#   vuln-scan.json    grype's own JSON report
#   scan-result.json  tool + DB version, policy evaluated, counts by
#                     severity, outcome, subject digest
#
# Exit codes:
#   0  scan completed and the policy passed (or SCAN_ADVISORY=true)
#   1  hard error (missing SBOM, subject mismatch, install failure,
#      missing jq, unreadable report, grype itself failing)
#   2  policy gate failed (matching severity vulns present)

set -euo pipefail

TEMPLATE_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
PROJECT_ROOT="${PROJECT_ROOT:-$(pwd)}"
export TEMPLATE_ROOT PROJECT_ROOT
cd "${PROJECT_ROOT}"

# shellcheck source=../lib/scan-common.sh
. "${TEMPLATE_ROOT}/scripts/lib/scan-common.sh"
scan_bootstrap

# ── The subject this job is gating ─────────────────────────────────
# Grype reads an SBOM rather than an image, so it never resolves a
# reference. It still has to hold the build record to a postscan's
# standard: SCAN_SUBJECT=built means the record must be present and
# check out before any of its evidence is scored.
if [ "${SCAN_SUBJECT:-}" = "built" ]; then
  resolve_scan_ref "" >/dev/null || exit 1
fi

# ── Resolve input SBOM ─────────────────────────────────────────────
SBOM_IN="${1:-${SBOM_FILE}}"
case "${SBOM_IN}" in
  /*) ;;
  *)  SBOM_IN="${PROJECT_ROOT}/${SBOM_IN}" ;;
esac
if [ ! -s "${SBOM_IN}" ]; then
  echo "ERROR: SBOM not found at ${SBOM_IN}" >&2
  echo "  Run scripts/scan/syft-sbom.sh (or xray-sbom.sh) first," >&2
  echo "  or pass an explicit SBOM path: bash scripts/scan/grype-vuln.sh <path>" >&2
  exit 1
fi

# ── Resolve output path ────────────────────────────────────────────
case "${VULN_SCAN_FILE}" in
  /*) SCAN_OUT="${VULN_SCAN_FILE}" ;;
  *)  SCAN_OUT="${PROJECT_ROOT}/${VULN_SCAN_FILE}" ;;
esac

# ── Auto-install grype (version-pinned, enforced) ──────────────────
# Two installer sources, auto-detected by the GRYPE_INSTALLER_URL suffix:
#   *.sh             → upstream install.sh, piped to sh (GRYPE_VERSION pins the
#                      release tag; install.sh selects the right OS/arch binary)
#   *.tar.gz | *.tgz → a grype release archive; the binary is extracted from it.
#                      The pinned version is read from the URL (grype_X.Y.Z_…).
#                      Use this for air-gapped / Artifactory mirrors.
#
# The pin is ENFORCED, not just "install if missing": a grype already on PATH
# at a DIFFERENT version (a runner's stale/baked-in build, or an exploited
# mutable tag) is reinstalled so the pinned version is exactly what runs —
# reproducible scans, and no trusting an unknown on-PATH binary. We compare the
# installed `grype version` to the desired version and (re)install on mismatch.
_url="${GRYPE_INSTALLER_URL:-https://raw.githubusercontent.com/anchore/grype/main/install.sh}"
_ver="${GRYPE_VERSION:-v0.114.0}"
_bindir="${HOME}/.local/bin"
# Desired version, normalised to bare X.Y.Z. From the URL for a tarball,
# else from GRYPE_VERSION. Empty = couldn't determine (unversioned tarball URL).
case "${_url}" in
  *.tar.gz|*.tgz) _want="$(printf '%s' "${_url##*/}" | grep -oE '[0-9]+\.[0-9]+\.[0-9]+' | head -1)" ;;
  *)              _want="${_ver#v}" ;;
esac
_have="$(command -v grype >/dev/null 2>&1 && grype version 2>/dev/null | sed -n 's/^Version:[[:space:]]*//p' | head -1 || true)"

if [ -n "${_have}" ] && { [ -z "${_want}" ] || [ "${_have}" = "${_want}" ]; }; then
  echo "→ grype ${_have} already on PATH${_want:+ (matches pin ${_want})} — skipping install"
else
  if [ -n "${_have}" ]; then
    echo "→ grype ${_have} on PATH but pin is ${_want:-<from URL>} — reinstalling to enforce pin"
  else
    echo "→ grype not on PATH — installing ${_want:+v${_want} }from ${_url}"
  fi
  mkdir -p "${_bindir}"
  _ok=0
  case "${_url}" in
    *.tar.gz|*.tgz)
      echo "  (tar.gz release archive)"
      _tmp="$(mktemp -d)"
      if curl -fsSL --max-time 120 "${_url}" -o "${_tmp}/grype.tgz" \
         && tar -xzf "${_tmp}/grype.tgz" -C "${_tmp}"; then
        _bin="$(find "${_tmp}" -type f -name grype | head -1)"
        if [ -n "${_bin}" ] && install -m 0755 "${_bin}" "${_bindir}/grype"; then
          _ok=1
        fi
      fi
      rm -rf "${_tmp}"
      ;;
    *)
      echo "  (install.sh, version v${_want})"
      if curl -fsSL --max-time 120 "${_url}" \
           | sh -s -- -b "${_bindir}" "v${_want}" >/dev/null 2>&1; then
        _ok=1
      fi
      ;;
  esac
  if [ "${_ok}" != 1 ] || [ ! -x "${_bindir}/grype" ]; then
    echo "ERROR: grype install failed — set GRYPE_INSTALLER_URL to a reachable .sh installer or .tar.gz release" >&2
    exit 1
  fi
  # Prepend our dir + drop any cached path so the pinned binary shadows a
  # system/brew grype, then VERIFY the pin actually took.
  export PATH="${_bindir}:${PATH}"
  hash -r 2>/dev/null || true
  _now="$(grype version 2>/dev/null | sed -n 's/^Version:[[:space:]]*//p' | head -1)"
  echo "  ✓ grype ${_now} installed"
  if [ -n "${_want}" ] && [ "${_now}" != "${_want}" ]; then
    echo "ERROR: installed grype ${_now} does not match pinned ${_want}" >&2
    exit 1
  fi
fi

# ── Air-gap CVE DB redirect (Artifactory mirror) ───────────────────
# Same logic as the inline GitLab block we replaced — picks up an
# Artifactory-hosted Grype DB when ARTIFACTORY_GRYPE_DB_REPO is set.
if [ -n "${ARTIFACTORY_GRYPE_DB_REPO:-}" ] && [ -n "${ARTIFACTORY_URL:-}" ] \
   && [ -z "${GRYPE_DB_UPDATE_URL:-}" ]; then
  _art_host="${ARTIFACTORY_URL#https://}"
  _art_host="${_art_host#http://}"
  _art_host="${_art_host%%/*}"
  _art_secret="${ARTIFACTORY_TOKEN:-${ARTIFACTORY_PASSWORD:-}}"
  _subpath="${GRYPE_DB_MIRROR_SUBPATH:-grype-db/v6}"
  if [ -n "${ARTIFACTORY_USER:-}" ] && [ -n "${_art_secret}" ]; then
    # grype has no separate credential setting for the DB source: the
    # only documented auth for db.update-url is userinfo in the URL
    # (registry.auth is for the scanned image, not the DB). So the
    # mirror credential lives in this URL, and grype quotes that URL
    # back in its own error and debug output. -q below silences grype's
    # logger and _redact_userinfo scrubs whatever still reaches this
    # job's log. Residual: a grype build that printed the URL on a
    # channel this script does not read would still leak it.
    export GRYPE_DB_UPDATE_URL="https://${ARTIFACTORY_USER}:${_art_secret}@${_art_host}/artifactory/${ARTIFACTORY_GRYPE_DB_REPO}/${_subpath}/latest.json"
    export GRYPE_DB_AUTO_UPDATE=true
    echo "→ Grype DB source: ${ARTIFACTORY_URL}/artifactory/${ARTIFACTORY_GRYPE_DB_REPO}/${_subpath}/latest.json"
  fi
fi

# user:secret@host in anything grype prints, replaced before it is logged.
_redact_userinfo() { sed -E 's#(https?://)[^/@[:space:]]+@#\1***:***@#g'; }

# ── Check the SBOM is about the image this job gates ───────────────
scan_verify_sbom_subject "${SBOM_IN}" || exit 1

# ── Run the scan ───────────────────────────────────────────────────
echo "→ grype sbom:${SBOM_IN} → ${SCAN_OUT}"
# One invocation emits BOTH outputs — JSON to the file, table to stdout
# — so the CVE DB loads once (was two full grype runs). --fail-on is
# empty because the gate below owns the verdict; a non-zero rc here is
# grype failing to run, which is not a pass. -q drops grype's own logging
# (which quotes GRYPE_DB_UPDATE_URL) and leaves both reports; pipefail
# keeps grype's rc across the redaction pipe.
if ! grype "sbom:${SBOM_IN}" --fail-on "" -q -o "json=${SCAN_OUT}" -o table 2>&1 | _redact_userinfo; then
  echo "ERROR: grype exited non-zero — the scan did not complete, so there is" >&2
  echo "       no report to gate on." >&2
  exit 1
fi

# ── Optional inline hand-off to vuln-post.sh (off by default) ──────
# Set VULN_INLINE_POST=true to ship sinks here in the scan job.
# Default OFF — the vuln-ingest stage runs vuln-post.sh canonically.
# Inline is for callers that want scan-time delivery without waiting
# for the ingest stage (mirrors Xray's native scan+post pattern).
# vuln-post.sh handles ALL sinks (webhook / Artifactory archive /
# Splunk HEC) — no scanner-side hardcoding to a specific sink.
case "$(printf '%s' "${VULN_INLINE_POST:-false}" | tr '[:upper:]' '[:lower:]')" in
  true|1|yes|on)
    if [ -f "${TEMPLATE_ROOT}/scripts/ingest/vuln-post.sh" ]; then
      echo ""
      echo "→ VULN_INLINE_POST=true → handing off to scripts/ingest/vuln-post.sh"
      bash "${TEMPLATE_ROOT}/scripts/ingest/vuln-post.sh" "${SCAN_OUT}" || {
        echo "  WARN: vuln-post.sh exited non-zero — scan artifact still written" >&2
      }
    fi
    ;;
esac

# ── Policy gate ────────────────────────────────────────────────────
# scan_gate (scripts/lib/scan-common.sh) counts, writes scan-result.json
# and decides. Enforcing by default; SCAN_ADVISORY=true reports without
# blocking.
_tool_ver="$(grype version 2>/dev/null | sed -n 's/^Version:[[:space:]]*//p' | head -1)"
_db_built="$(grype db status 2>/dev/null | sed -n 's/^Built:[[:space:]]*//p' | head -1)"
scan_gate grype "${_tool_ver}" "${_db_built}" "${SCAN_OUT}" matches \
  '[.matches[] | select((.vulnerability.severity // "" | ascii_downcase) == $s)] | length' \
  "${GRYPE_FAIL_ON_SEVERITY:-}" || exit $?
