#!/usr/bin/env bash
# ─── DO NOT EDIT — template lib ────────────────────────────────────
# Canonical filenames consumed by every CI stage. Override per-job
# via SBOM_FILE / VULN_SCAN_FILE shell vars, not by editing here.
# ───────────────────────────────────────────────────────────────────
#
# scripts/lib/artifact-names.sh — canonical artifact filename contract
#
# ONE source of truth for the filenames every CI stage produces and
# consumes. Sourced by:
#
#   build.sh                  (writes the names into build.env so
#                              CI's `. ./build.env` flow propagates
#                              them to downstream stages)
#   push-backends/*.sh        (pick up the names when emitting build.env)
#   scan/syft-sbom.sh         (writes SBOM_FILE)
#   scan/xray-sbom.sh         (writes SBOM_FILE)
#   scan/xray-vuln.sh         (writes VULN_SCAN_FILE)
#   sbom-post.sh              (reads SBOM_FILE)
#
# Shell-set values WIN over the defaults below, so a CI variable or
# `export SBOM_FILE=foo.json` still overrides per-job. The point is
# that no individual script has its own default to drift — they all
# read from this one file.
#
# Why a shared lib instead of just a constant in build.sh: prescan
# stages (xray-vuln-prescan, xray-sbom-prescan) run BEFORE build.sh
# and have no build.env to source. They source this file directly to
# get the same names. This way the prescan-vs-postscan codepath stays
# symmetric — both stages produce sbom.cdx.json / vuln-scan.json,
# and downstream consumers (Grype, sbom-post) never have to know
# which producer ran.

# shellcheck disable=SC2148

# Canonical CycloneDX SBOM filename. Consumed by Grype + sbom-post.sh.
# Producers: scan/syft-sbom.sh, scan/xray-sbom.sh.
export SBOM_FILE="${SBOM_FILE:-sbom.cdx.json}"

# Canonical vulnerability scan output. Consumed by audit shippers
# (Splunk HEC, etc.). Producers: scan/xray-vuln.sh (and any future
# trivy-vuln.sh / grype-vuln.sh swap).
export VULN_SCAN_FILE="${VULN_SCAN_FILE:-vuln-scan.json}"

# ── Per-instance artifact root ──────────────────────────────────────
# Evidence lands under artifacts/<image instance>/ instead of the
# project root. Two images built by one pipeline write two roots, so
# neither overwrites the other's SBOM, report or subject record.
# SCAN_SUBJECT=upstream gets its own root: a prescan describes the image
# we rebuilt FROM, which is not the image we built.
#
#   artifact_root   echoes the directory, relative to PROJECT_ROOT.
#   artifact_paths  re-roots the canonical names into it and creates it.
#
# scan_bootstrap calls artifact_paths once IMAGE_NAME is known. A caller
# that only sources this file keeps the bare filenames, so the contract
# above still reads as one filename per artifact type.
artifact_root() {
  local key="${IMAGE_NAME:-${UPSTREAM_IMAGE:-}}"
  [ -n "${key}" ] || key="${UPSTREAM_REF:-}"
  key="${key%@*}"          # drop any @sha256:… pin
  key="${key##*/}"         # leaf segment only
  key="${key%%:*}"         # drop any :tag
  key="$(printf '%s' "${key}" | tr -c 'A-Za-z0-9._-' '-')"
  [ -n "${key}" ] || key="image"
  [ "${SCAN_SUBJECT:-}" = "upstream" ] && key="${key}-upstream"
  printf '%s/%s' "${SCAN_ARTIFACT_DIR:-artifacts}" "${key}"
}

# A name carrying a slash is a path the operator chose and is left
# alone. A bare filename is canonical, and belongs in this instance's
# root.
artifact_paths() {
  SCAN_ARTIFACT_ROOT="$(artifact_root)"
  case "${SBOM_FILE}"      in */*) ;; *) SBOM_FILE="${SCAN_ARTIFACT_ROOT}/${SBOM_FILE}" ;; esac
  case "${VULN_SCAN_FILE}" in */*) ;; *) VULN_SCAN_FILE="${SCAN_ARTIFACT_ROOT}/${VULN_SCAN_FILE}" ;; esac
  export SCAN_ARTIFACT_ROOT SBOM_FILE VULN_SCAN_FILE
  mkdir -p "${SCAN_ARTIFACT_ROOT}"
}
