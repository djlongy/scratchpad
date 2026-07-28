#!/usr/bin/env bash
# freeipa-e2e.sh — portable walk-away FreeIPA pipeline (external-CA signed by Vault).
#
# Stages (resume with --from <name>; state skips completed ones):
#   preflight | vault | pki | prep | sign | converge | dns | complete
#
# User configures ONLY inventory + freeipa_server_*/vault_pki_* vars.
# This script never hard-codes hostnames, domains, or estate playbooks.
#
# Usage (from the ansible/ root of this package):
#   export ANSIBLE_VAULT_PASSWORD=...   # if inventory uses ansible-vault
#   INVENTORY=inventories/lab/hosts.yml \
#   IPA_HOST=idm-01 \
#     ./scripts/freeipa-e2e.sh
#
#   INVENTORY=... IPA_HOST=idm-01 ./scripts/freeipa-e2e.sh --from sign
#   SKIP_VAULT=1 SKIP_PREP=1 SKIP_DNS=1 INVENTORY=... IPA_HOST=idm-01 ./scripts/freeipa-e2e.sh
#   REDEPLOY_IPA=1 INVENTORY=... IPA_HOST=idm-01 ./scripts/freeipa-e2e.sh --from prep
#
# Playbook knobs (defaults = shipped playbooks in this package):
#   PLAYBOOK_VAULT     playbooks/vault_solo_e2e.yml   (only if stage vault runs)
#   PLAYBOOK_PKI       playbooks/vault_pki_issuer.yml
#   PLAYBOOK_PREP      playbooks/freeipa_prep.yml
#   PLAYBOOK_SIGN      playbooks/freeipa_signed_install.yml
#   PLAYBOOK_CONVERGE  playbooks/freeipa.yml
#   PLAYBOOK_DNS       ""  (empty = skip; set to your DNS playbook if any)
#
# Optional:
#   VAULT_HOST         --limit for vault/pki stages (default: whole vault group)
#   VAULT_ADDR         for health checks (else from inventory vault_pki_addr)
#   VAULT_TOKEN_FILE   path to a root/admin token for sign stage (mode 0600)
#   STATE_DIR          ~/.cache/freeipa-e2e
#   EXTRA_ANSIBLE_ARGS extra args appended to every playbook run
#   SKIP_VAULT|SKIP_PKI|SKIP_PREP|SKIP_CONVERGE|SKIP_DNS=1
#   REDEPLOY_IPA=1     force VM wipe+rebuild on prep
#   MAX_RETRIES=3 RETRY_SLEEP=20

set -euo pipefail

# ── defaults (all overridable) ───────────────────────────────────────────────
INVENTORY="${INVENTORY:-inventories/example/hosts.yml}"
IPA_HOST="${IPA_HOST:-}"
VAULT_HOST="${VAULT_HOST:-}"

PLAYBOOK_VAULT="${PLAYBOOK_VAULT:-playbooks/vault_solo_e2e.yml}"
PLAYBOOK_PKI="${PLAYBOOK_PKI:-playbooks/vault_pki_issuer.yml}"
PLAYBOOK_PREP="${PLAYBOOK_PREP:-playbooks/freeipa_prep.yml}"
PLAYBOOK_SIGN="${PLAYBOOK_SIGN:-playbooks/freeipa_signed_install.yml}"
PLAYBOOK_CONVERGE="${PLAYBOOK_CONVERGE:-playbooks/freeipa.yml}"
PLAYBOOK_DNS="${PLAYBOOK_DNS:-}"

STATE_DIR="${STATE_DIR:-${HOME}/.cache/freeipa-e2e}"
LOG_DIR="${LOG_DIR:-${STATE_DIR}/logs}"
VAULT_TOKEN_FILE="${VAULT_TOKEN_FILE:-}"
VAULT_ADDR="${VAULT_ADDR:-}"
SKIP_VAULT="${SKIP_VAULT:-0}"
SKIP_PKI="${SKIP_PKI:-0}"
SKIP_PREP="${SKIP_PREP:-0}"
SKIP_CONVERGE="${SKIP_CONVERGE:-0}"
SKIP_DNS="${SKIP_DNS:-1}"
REDEPLOY_IPA="${REDEPLOY_IPA:-0}"
MAX_RETRIES="${MAX_RETRIES:-3}"
RETRY_SLEEP="${RETRY_SLEEP:-20}"
FROM_STAGE=""
# shellcheck disable=SC2206
EXTRA_ANSIBLE_ARGS=( ${EXTRA_ANSIBLE_ARGS:-} )

usage() {
  sed -n '2,40p' "$0" | sed 's/^# \{0,1\}//'
  exit "${1:-2}"
}

while [ $# -gt 0 ]; do
  case "$1" in
    -h|--help) usage 0 ;;
    --from) FROM_STAGE="$2"; shift 2 ;;
    --ipa-host) IPA_HOST="$2"; shift 2 ;;
    --vault-host) VAULT_HOST="$2"; shift 2 ;;
    --inventory) INVENTORY="$2"; shift 2 ;;
    --redeploy-ipa) REDEPLOY_IPA=1; shift ;;
    --skip-vault) SKIP_VAULT=1; shift ;;
    --skip-pki) SKIP_PKI=1; shift ;;
    --skip-prep) SKIP_PREP=1; shift ;;
    --skip-converge) SKIP_CONVERGE=1; shift ;;
    --skip-dns) SKIP_DNS=1; shift ;;
    *) echo "Unknown arg: $1" >&2; usage 2 ;;
  esac
done

ANSIBLE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ANSIBLE_DIR"
mkdir -p "$STATE_DIR" "$LOG_DIR"
chmod 700 "$STATE_DIR" 2>/dev/null || true

STATE_KEY="$(printf '%s' "$INVENTORY:$IPA_HOST" | tr '/ ' '__')"
STATE_FILE="${STATE_DIR}/state.${STATE_KEY}"
TOKEN_FILE="${VAULT_TOKEN_FILE:-${STATE_DIR}/.vault.token}"

# ── logging / stages ─────────────────────────────────────────────────────────
ts() { date '+%Y-%m-%dT%H:%M:%S'; }
log() { printf '[%s] %s\n' "$(ts)" "$*" | tee -a "${LOG_DIR}/pipeline.log"; }
die() { log "FATAL: $*"; exit 1; }
banner() {
  log "=============================================================================="
  log "$*"
  log "=============================================================================="
}

STAGES=(preflight vault pki prep sign converge dns complete)

stage_done() {
  local s="$1"
  [ -f "$STATE_FILE" ] && grep -qx "DONE ${s}" "$STATE_FILE"
}

mark_done() {
  local s="$1"
  grep -qx "DONE ${s}" "$STATE_FILE" 2>/dev/null || echo "DONE ${s}" >>"$STATE_FILE"
  log "STAGE OK: ${s}"
}

should_run() {
  local s="$1"
  if [ -n "$FROM_STAGE" ]; then
    local j target=-1 cur=-1
    for j in "${!STAGES[@]}"; do
      [ "${STAGES[$j]}" = "$FROM_STAGE" ] && target=$j
      [ "${STAGES[$j]}" = "$s" ] && cur=$j
    done
    [ "$target" -lt 0 ] && die "unknown --from stage: $FROM_STAGE (want: ${STAGES[*]})"
    [ "$cur" -lt "$target" ] && return 1
  fi
  stage_done "$s" && return 1
  return 0
}

retry() {
  local name="$1"
  shift
  local n=1 rc=0
  until "$@"; do
    rc=$?
    if [ "$n" -ge "$MAX_RETRIES" ]; then
      log "FAILED after ${MAX_RETRIES} tries: ${name} (rc=${rc})"
      return "$rc"
    fi
    log "retry ${n}/${MAX_RETRIES} ${name} in ${RETRY_SLEEP}s (rc=${rc})"
    sleep "$RETRY_SLEEP"
    n=$((n + 1))
  done
}

run_ap() {
  local tag="$1"
  shift
  local logf="${LOG_DIR}/$(ts | tr -d ':-')-${tag}.log"
  log "ansible-playbook $*  (log: $logf)"
  set +e
  # shellcheck disable=SC2086
  ansible-playbook "$@" 2>&1 | tee "$logf"
  local rc=${PIPESTATUS[0]}
  set -e
  return "$rc"
}

limit_args() {
  # emit --limit HOST when set
  if [ -n "${1:-}" ]; then
    printf '%s\n' --limit "$1"
  fi
}

# ── preflight ────────────────────────────────────────────────────────────────
stage_preflight() {
  should_run preflight || { log "skip preflight (done)"; return 0; }
  banner "STAGE preflight"

  [ -n "$IPA_HOST" ] || die "Set IPA_HOST (or --ipa-host) to the FreeIPA inventory hostname"
  [ -f "$INVENTORY" ] || die "INVENTORY not found: $INVENTORY"
  command -v ansible-playbook >/dev/null || die "ansible-playbook not on PATH"
  command -v ansible >/dev/null || die "ansible not on PATH"

  for pb in "$PLAYBOOK_SIGN" "$PLAYBOOK_PREP" "$PLAYBOOK_PKI" "$PLAYBOOK_CONVERGE"; do
    [ -f "$pb" ] || die "playbook missing: $pb (override PLAYBOOK_* or run from package root)"
  done
  [ -n "$PLAYBOOK_DNS" ] && [ ! -f "$PLAYBOOK_DNS" ] && die "PLAYBOOK_DNS missing: $PLAYBOOK_DNS"
  [ -f "$PLAYBOOK_VAULT" ] || log "WARN: PLAYBOOK_VAULT missing ($PLAYBOOK_VAULT) — set SKIP_VAULT=1 if unused"

  export OBJC_DISABLE_INITIALIZE_FORK_SAFETY="${OBJC_DISABLE_INITIALIZE_FORK_SAFETY:-YES}"
  export ANSIBLE_HOST_KEY_CHECKING="${ANSIBLE_HOST_KEY_CHECKING:-False}"

  log "inventory=$INVENTORY ipa=$IPA_HOST vault_host=${VAULT_HOST:-<group>}"
  log "playbooks: vault=$PLAYBOOK_VAULT pki=$PLAYBOOK_PKI prep=$PLAYBOOK_PREP"
  log "           sign=$PLAYBOOK_SIGN converge=$PLAYBOOK_CONVERGE dns=${PLAYBOOK_DNS:-<skip>}"
  log "state=$STATE_FILE"
  mark_done preflight
}

# ── vault (optional full seed) ───────────────────────────────────────────────
stage_vault() {
  should_run vault || { log "skip vault (done)"; return 0; }
  if [ "$SKIP_VAULT" = "1" ] || [ "$SKIP_VAULT" = "true" ]; then
    log "SKIP_VAULT=1"
    mark_done vault
    return 0
  fi
  banner "STAGE vault — ensure Vault is up (${VAULT_HOST:-vault group})"
  local lim=()
  # shellcheck disable=SC2207
  lim=( $(limit_args "$VAULT_HOST") )
  retry vault run_ap vault -i "$INVENTORY" "$PLAYBOOK_VAULT" \
    "${lim[@]+"${lim[@]}"}" \
    "${EXTRA_ANSIBLE_ARGS[@]+"${EXTRA_ANSIBLE_ARGS[@]}"}" \
    || die "vault stage failed — fix Vault or set SKIP_VAULT=1 if already healthy"
  mark_done vault
}

# ── pki issuer import ────────────────────────────────────────────────────────
stage_pki() {
  should_run pki || { log "skip pki (done)"; return 0; }
  if [ "$SKIP_PKI" = "1" ] || [ "$SKIP_PKI" = "true" ]; then
    log "SKIP_PKI=1"
    mark_done pki
    return 0
  fi
  banner "STAGE pki — import offline CA issuer into Vault pki/"
  local lim=()
  # shellcheck disable=SC2207
  lim=( $(limit_args "$VAULT_HOST") )
  retry pki run_ap pki -i "$INVENTORY" "$PLAYBOOK_PKI" \
    --tags pki,pki_issuer \
    "${lim[@]+"${lim[@]}"}" \
    -e hashicorp_vault_pki_issuer_import=true \
    "${EXTRA_ANSIBLE_ARGS[@]+"${EXTRA_ANSIBLE_ARGS[@]}"}" \
    || die "pki issuer import failed"
  mark_done pki
}

# ── prep (VM + storage + baseline) ───────────────────────────────────────────
stage_prep() {
  should_run prep || { log "skip prep (done)"; return 0; }
  if [ "$SKIP_PREP" = "1" ] || [ "$SKIP_PREP" = "true" ]; then
    log "SKIP_PREP=1"
    mark_done prep
    return 0
  fi
  banner "STAGE prep — provision FreeIPA host (${IPA_HOST})"
  local redeploy=()
  if [ "$REDEPLOY_IPA" = "1" ] || [ "$REDEPLOY_IPA" = "true" ]; then
    redeploy=(-e vsphere_vm_force_redeploy=true)
    if [ -f "$STATE_FILE" ]; then
      grep -vE 'DONE (sign|converge|dns|complete)$' "$STATE_FILE" >"${STATE_FILE}.tmp" || true
      mv "${STATE_FILE}.tmp" "$STATE_FILE"
    fi
  fi
  retry prep run_ap prep -i "$INVENTORY" "$PLAYBOOK_PREP" \
    --limit "$IPA_HOST" \
    "${redeploy[@]+"${redeploy[@]}"}" \
    "${EXTRA_ANSIBLE_ARGS[@]+"${EXTRA_ANSIBLE_ARGS[@]}"}" \
    || die "prep failed"
  retry ping ansible "$IPA_HOST" -i "$INVENTORY" -m ping \
    || die "IPA host not reachable after prep"
  mark_done prep
}

# ── signed install (two processes) ───────────────────────────────────────────
stage_sign() {
  should_run sign || { log "skip sign (done)"; return 0; }
  banner "STAGE sign — FreeIPA external-CA (phase 1 → phase 2)"

  local token_args=()
  local ev=""
  if [ -f "$TOKEN_FILE" ] && [ -s "$TOKEN_FILE" ]; then
    # Keep token out of process argv; pass -e @file to ansible only.
    ev="$(mktemp "${STATE_DIR}/extra-XXXXXX.yml")"
    umask 077
    python3 - "$TOKEN_FILE" "$ev" <<'PY'
import sys
try:
    import yaml
except ImportError:
    token = open(sys.argv[1]).read().strip().replace("'", "''")
    with open(sys.argv[2], "w") as f:
        f.write("vault_pki_token: '%s'\n" % token)
        f.write("vault_pki_validate_certs: false\n")
    raise SystemExit(0)
token = open(sys.argv[1]).read().strip()
with open(sys.argv[2], "w") as f:
    yaml.safe_dump(
        {"vault_pki_token": token, "vault_pki_validate_certs": False},
        f,
        default_flow_style=False,
    )
PY
    chmod 600 "$ev"
    token_args=(-e "@${ev}")
  else
    log "no VAULT_TOKEN_FILE — vault_pki token must come from inventory/role defaults"
  fi

  set +e
  if [ -x ./scripts/freeipa-signed-install.sh ]; then
    INVENTORY="$INVENTORY" PLAYBOOK="$PLAYBOOK_SIGN" \
      ./scripts/freeipa-signed-install.sh "$IPA_HOST" \
      "${token_args[@]+"${token_args[@]}"}" \
      "${EXTRA_ANSIBLE_ARGS[@]+"${EXTRA_ANSIBLE_ARGS[@]}"}"
    rc=$?
  else
    run_ap sign1 -i "$INVENTORY" "$PLAYBOOK_SIGN" --limit "$IPA_HOST" \
      -e freeipa_signed_install_phase=1 \
      "${token_args[@]+"${token_args[@]}"}" \
      "${EXTRA_ANSIBLE_ARGS[@]+"${EXTRA_ANSIBLE_ARGS[@]}"}"
    rc=$?
    if [ "$rc" -eq 0 ]; then
      run_ap sign2 -i "$INVENTORY" "$PLAYBOOK_SIGN" --limit "$IPA_HOST" \
        -e freeipa_signed_install_phase=2 \
        "${token_args[@]+"${token_args[@]}"}" \
        "${EXTRA_ANSIBLE_ARGS[@]+"${EXTRA_ANSIBLE_ARGS[@]}"}"
      rc=$?
    fi
  fi
  set -e
  [ -n "$ev" ] && rm -f "$ev"
  [ "$rc" -eq 0 ] || die "signed install failed (rc=${rc})"
  mark_done sign
}

# ── converge (IAM/DNS/hardening after install-scoped signed install) ─────────
stage_converge() {
  should_run converge || { log "skip converge (done)"; return 0; }
  if [ "$SKIP_CONVERGE" = "1" ] || [ "$SKIP_CONVERGE" = "true" ]; then
    log "SKIP_CONVERGE=1"
    mark_done converge
    return 0
  fi
  banner "STAGE converge — freeipa_server full role on ${IPA_HOST}"
  retry converge run_ap converge -i "$INVENTORY" "$PLAYBOOK_CONVERGE" \
    --limit "$IPA_HOST" \
    "${EXTRA_ANSIBLE_ARGS[@]+"${EXTRA_ANSIBLE_ARGS[@]}"}" \
    || die "converge failed"
  mark_done converge
}

# ── optional DNS ─────────────────────────────────────────────────────────────
stage_dns() {
  should_run dns || { log "skip dns (done)"; return 0; }
  if [ "$SKIP_DNS" = "1" ] || [ "$SKIP_DNS" = "true" ] || [ -z "$PLAYBOOK_DNS" ]; then
    log "SKIP_DNS or empty PLAYBOOK_DNS"
    mark_done dns
    return 0
  fi
  banner "STAGE dns — ${PLAYBOOK_DNS}"
  if ! run_ap dns -i "$INVENTORY" "$PLAYBOOK_DNS" \
    "${EXTRA_ANSIBLE_ARGS[@]+"${EXTRA_ANSIBLE_ARGS[@]}"}"; then
    log "WARN: DNS play failed — IPA may still work by IP; fix later"
  fi
  mark_done dns
}

main() {
  touch "$STATE_FILE"
  log "pipeline start (from=${FROM_STAGE:-auto} redeploy_ipa=${REDEPLOY_IPA})"
  stage_preflight
  stage_vault
  stage_pki
  stage_prep
  stage_sign
  stage_converge
  stage_dns
  mark_done complete
  banner "ALL DONE"
  log "IPA host: ${IPA_HOST}"
  log "State:    ${STATE_FILE}"
  log "Logs:     ${LOG_DIR}"
  log "Re-run is idempotent; --from <stage> or delete state to force."
}

main "$@"
