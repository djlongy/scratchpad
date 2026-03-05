#!/usr/bin/env bash
# load_vmware_env.sh — Populate VMWARE_* environment variables from HashiCorp Vault
#
# This script MUST be sourced (not executed) so the exports persist in your shell:
#   source scripts/load_vmware_env.sh [vcenter_host]
#
# Prerequisites:
#   - vault CLI in PATH and authenticated (vault login / VAULT_TOKEN set)
#   - Secrets stored in Vault KV v2 at the paths below
#   - community.vmware collection installed (ansible-galaxy collection install community.vmware)
#
# Vault secret layout (adjust paths to match your Vault policy):
#
#   Primary path  : kv/<PRIMARY_SECRET_PATH>
#     Fields      : username, password, [hostname]
#
#   Fallback path : kv/<FALLBACK_SECRET_PATH>
#     Fields      : admin_username, admin_password, [hostname]
#
# After sourcing, the following variables are exported:
#   VMWARE_HOST     — vCenter FQDN or IP
#   VMWARE_USER     — Service account username
#   VMWARE_PASSWORD — Service account password

set -euo pipefail

if [[ "${BASH_SOURCE[0]}" == "${0}" ]]; then
  echo "This script must be sourced so exports persist in your shell." >&2
  echo "Usage: source scripts/load_vmware_env.sh [vcenter_host]" >&2
  exit 1
fi

# ---------------------------------------------------------------------------
# Configuration — edit these to match your Vault KV paths
# ---------------------------------------------------------------------------
PRIMARY_SECRET_PATH="secret/vsphere/svc-account"     # e.g. a dedicated Terraform/Ansible svc account
FALLBACK_SECRET_PATH="secret/vsphere/admin"           # fallback to a shared admin credential

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
_get_field() {
  local path="$1" field="$2"
  vault kv get -mount=kv -field="$field" "$path" 2>/dev/null || true
}

# ---------------------------------------------------------------------------
# Validate vault CLI
# ---------------------------------------------------------------------------
if ! command -v vault >/dev/null 2>&1; then
  echo "[load_vmware_env] vault CLI not found in PATH" >&2
  return 1
fi

# ---------------------------------------------------------------------------
# Resolve credentials (primary → fallback)
# ---------------------------------------------------------------------------
_secret_path_used="$PRIMARY_SECRET_PATH"
VMWARE_USER="$(_get_field "$PRIMARY_SECRET_PATH" "username")"
VMWARE_PASSWORD="$(_get_field "$PRIMARY_SECRET_PATH" "password")"

if [[ -z "$VMWARE_USER" || -z "$VMWARE_PASSWORD" ]]; then
  _secret_path_used="$FALLBACK_SECRET_PATH"
  VMWARE_USER="$(_get_field "$FALLBACK_SECRET_PATH" "admin_username")"
  VMWARE_PASSWORD="$(_get_field "$FALLBACK_SECRET_PATH" "admin_password")"
fi

if [[ -z "$VMWARE_USER" ]]; then
  echo "[load_vmware_env] Failed to read VMware username from Vault" >&2
  echo "  Tried: kv/$PRIMARY_SECRET_PATH (username)" >&2
  echo "         kv/$FALLBACK_SECRET_PATH (admin_username)" >&2
  return 1
fi

if [[ -z "$VMWARE_PASSWORD" ]]; then
  echo "[load_vmware_env] Failed to read VMware password from Vault" >&2
  return 1
fi

# ---------------------------------------------------------------------------
# Resolve vCenter hostname (arg > env > Vault > default)
# ---------------------------------------------------------------------------
# Priority: CLI arg > existing env var > Vault field > default placeholder
VMWARE_HOST="${1:-${VMWARE_HOST:-}}"
if [[ -z "$VMWARE_HOST" ]]; then
  VMWARE_HOST="$(_get_field "$_secret_path_used" "hostname")"
fi
if [[ -z "$VMWARE_HOST" ]]; then
  # ── MULTI-VCENTER TIP ──────────────────────────────────────────────────
  # For multiple vCenters, pass the target as an argument:
  #   source load_vmware_env.sh vcenter-prod.example.com
  #   source load_vmware_env.sh vcenter-dr.example.com
  # Or set separate Vault paths per environment and wrap this script.
  # ───────────────────────────────────────────────────────────────────────
  VMWARE_HOST="vcenter.example.com"   # Replace with your default vCenter FQDN
fi

# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------
export VMWARE_HOST
export VMWARE_USER
export VMWARE_PASSWORD

echo "[load_vmware_env] Loaded VMWARE_HOST=${VMWARE_HOST} VMWARE_USER=${VMWARE_USER} from kv/${_secret_path_used}"

# ── MULTI-TENANT TIP ────────────────────────────────────────────────────────
# If you manage multiple tenants on the same vCenter, export an additional
# variable to scope inventory queries:
#
#   export VMWARE_TENANT="${2:-}"   # e.g. source load_vmware_env.sh vcenter-prod acme
#
# Then reference it in vmware.yml filters:
#   filters:
#     - "'${VMWARE_TENANT}' in tags.get('tenant', [])"
# ────────────────────────────────────────────────────────────────────────────
