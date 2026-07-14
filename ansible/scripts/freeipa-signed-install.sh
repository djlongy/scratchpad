#!/usr/bin/env bash
# freeipa-signed-install.sh — turnkey wrapper for the two-PROCESS signed FreeIPA
# install (playbooks/freeipa_signed_install.yml).
#
# external-ca is inherently two-phase and freeipa.ansible_freeipa 1.16.0 only
# resumes "step two" across SEPARATE ansible-playbook processes (see the playbook
# header). So this runs the playbook TWICE, back-to-back:
#   phase 1 : emit the CSR + sign it off the org root (STOPS before completion)
#   phase 2 : push the signed cert + chain back, complete the install, distribute
# Phase 2 runs ONLY if phase 1 succeeds; a phase-1 failure aborts with a message.
#
# Usage:
#   [INVENTORY=inventories/example/hosts.yml] scripts/freeipa-signed-install.sh <host> [extra ansible args...]
#
#   <host>               inventory host/pattern for the IPA server (passed as --limit)
#   INVENTORY            inventory path (default: inventories/example/hosts.yml)
#   [extra ansible args] forwarded verbatim to both invocations, e.g.
#                        -e certificate_authority_pki_dir=/tmp/scratch-pki   (scratch PKI)
#
# Both phases stay documented and hand-runnable — this wrapper is only the turnkey
# convenience over:
#   ansible-playbook -i <inventory> playbooks/freeipa_signed_install.yml --limit <host> -e freeipa_signed_install_phase=1
#   ansible-playbook -i <inventory> playbooks/freeipa_signed_install.yml --limit <host> -e freeipa_signed_install_phase=2

set -euo pipefail

PLAYBOOK="playbooks/freeipa_signed_install.yml"
INVENTORY="${INVENTORY:-inventories/example/hosts.yml}"

usage() {
  sed -n '2,22p' "$0" | sed 's/^# \{0,1\}//'
  exit "${1:-2}"
}

if [ "$#" -lt 1 ] || [ "$1" = "-h" ] || [ "$1" = "--help" ]; then
  usage 0
fi

HOST="$1"
shift
EXTRA_ARGS=("$@")

# Run from the ansible/ root (parent of scripts/) so relative inventory/playbook
# paths resolve regardless of the caller's cwd.
ANSIBLE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ANSIBLE_DIR"

run_phase() {
  local phase="$1"
  echo "=============================================================================="
  echo "[freeipa-signed-install] host=${HOST} — PHASE ${phase}"
  echo "=============================================================================="
  ansible-playbook -i "$INVENTORY" "$PLAYBOOK" \
    --limit "$HOST" \
    -e "freeipa_signed_install_phase=${phase}" \
    "${EXTRA_ARGS[@]+"${EXTRA_ARGS[@]}"}"
}

if ! run_phase 1; then
  echo "ABORT: phase 1 (emit CSR + sign) failed — NOT proceeding to phase 2." >&2
  echo "       Inspect the failure above, fix it, and re-run this wrapper." >&2
  exit 1
fi

echo "[freeipa-signed-install] phase 1 succeeded — CSR signed; starting phase 2 (fresh process)."

if ! run_phase 2; then
  echo "ABORT: phase 2 (complete install + distribute) failed." >&2
  echo "       Phase 1 artifacts (signed cert + chain) are on the control node under" >&2
  echo "       pki/ipa/; re-running phase 2 by hand is safe once the cause is fixed:" >&2
  echo "         ansible-playbook -i ${INVENTORY} ${PLAYBOOK} --limit ${HOST} -e freeipa_signed_install_phase=2" >&2
  exit 1
fi

echo "[freeipa-signed-install] DONE — both phases completed for host ${HOST}."
