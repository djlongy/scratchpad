#!/usr/bin/env bash
# deploy.sh — Install Oh My Bash + devops-powerline theme on a remote host
#
# Usage:
#   ./deploy.sh [hostname] [username]
#   ./deploy.sh myserver.example.com ansible
#   ./deploy.sh myserver.example.com devops
#
# Defaults to: ansible@myserver.example.com
#
# What it does:
#   1. Backs up existing ~/.bashrc on the remote
#   2. Installs Oh My Bash (via git clone — no sudo, no packages)
#   3. Uploads the devops-powerline theme
#   4. Configures ~/.bashrc to use it
#
# Requirements (local machine):
#   - ssh access to the remote host (key-based preferred)
#   - scp available
#
# Requirements (remote machine):
#   - bash 4.2+  (RHEL/AlmaLinux 8/9, Ubuntu 20.04+ all ship bash 4.4+)
#   - git        (for cloning Oh My Bash)
#   - curl OR git to download OMB
#   - UTF-8 locale (standard on modern Linux)
#   - Terminal with Nerd Font on the CLIENT side (e.g. MesloLGS NF in iTerm2)

set -euo pipefail

TARGET_HOST="${1:-myserver.example.com}"
TARGET_USER="${2:-ansible}"
SSH_TARGET="${TARGET_USER}@${TARGET_HOST}"
THEME_NAME="devops-powerline"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
THEME_SRC="${SCRIPT_DIR}/theme/${THEME_NAME}/${THEME_NAME}.theme.bash"

# ── Preflight ─────────────────────────────────────────────────────────────────────
if [[ ! -f "$THEME_SRC" ]]; then
    echo "ERROR: Theme file not found: ${THEME_SRC}"
    echo "Run from the ohmybash/ directory or check theme/ subdirectory."
    exit 1
fi

echo "======================================================================"
echo "  Deploying Oh My Bash + ${THEME_NAME} theme"
echo "  Target: ${SSH_TARGET}"
echo "======================================================================"

# ── Step 1: Upload theme file ─────────────────────────────────────────────────────
echo ""
echo "==> Uploading theme..."
ssh "${SSH_TARGET}" "mkdir -p /tmp/${THEME_NAME}"
scp "${THEME_SRC}" "${SSH_TARGET}:/tmp/${THEME_NAME}/${THEME_NAME}.theme.bash"
echo "    Done."

# ── Step 2: Remote setup ──────────────────────────────────────────────────────────
echo ""
echo "==> Running remote setup..."

# Pass THEME_NAME as env var; use quoted heredoc so local shell does NOT expand anything
ssh "${SSH_TARGET}" THEME_NAME="${THEME_NAME}" bash <<'REMOTE_EOF'
set -euo pipefail

OMB_DIR="${HOME}/.oh-my-bash"
BASHRC="${HOME}/.bashrc"
BACKUP="${HOME}/.bashrc.pre-omb-$(date +%Y%m%d%H%M%S)"

echo ""
echo "--- Remote: $(hostname) | User: $(whoami) | Home: ${HOME}"

# ── Install Oh My Bash ──────────────────────────────────────────────────────────
if [[ -d "${OMB_DIR}" ]]; then
    echo "--- Oh My Bash already installed at ${OMB_DIR}, skipping clone."
else
    echo "--- Installing Oh My Bash via git..."
    if ! command -v git &>/dev/null; then
        echo "ERROR: git not found. Install git first (sudo dnf install git / sudo apt install git)."
        exit 1
    fi
    git clone --depth=1 https://github.com/ohmybash/oh-my-bash.git "${OMB_DIR}"
    echo "--- Cloned OK."
fi

# ── Install theme ───────────────────────────────────────────────────────────────
echo "--- Installing theme..."
mkdir -p "${OMB_DIR}/custom/themes/${THEME_NAME}"
cp "/tmp/${THEME_NAME}/${THEME_NAME}.theme.bash" \
   "${OMB_DIR}/custom/themes/${THEME_NAME}/"
rm -rf "/tmp/${THEME_NAME}"
echo "--- Theme installed at ${OMB_DIR}/custom/themes/${THEME_NAME}/"

# ── Configure ~/.bashrc ─────────────────────────────────────────────────────────
if grep -q 'oh-my-bash.sh' "${BASHRC}" 2>/dev/null; then
    echo "--- Oh My Bash already sourced in ${BASHRC}."
    echo "--- Backup: ${BACKUP}"
    cp "${BASHRC}" "${BACKUP}"

    # Update theme
    if grep -q '^OSH_THEME=' "${BASHRC}"; then
        sed -i "s|^OSH_THEME=.*|OSH_THEME=\"${THEME_NAME}\"|" "${BASHRC}"
        echo "--- Updated OSH_THEME to ${THEME_NAME}."
    else
        sed -i "/oh-my-bash.sh/i OSH_THEME=\"${THEME_NAME}\"" "${BASHRC}"
        echo "--- Inserted OSH_THEME=${THEME_NAME}."
    fi
else
    echo "--- Writing fresh Oh My Bash .bashrc config..."
    [[ -f "${BASHRC}" ]] && cp "${BASHRC}" "${BACKUP}" && echo "--- Backed up existing .bashrc to ${BACKUP}"

    # Write the OMB block — use a delimiter that won't appear in content
    cat >> "${BASHRC}" << 'BASHRC_BLOCK'

# ── Oh My Bash ────────────────────────────────────────────────────────────────────
export OSH="${HOME}/.oh-my-bash"

OSH_THEME="devops-powerline"

# Plugins to load (from ~/.oh-my-bash/plugins/)
plugins=(
  git
  bashmarks
  progress
)

# Completions to load
completions=(
  git
  ssh
)

# Aliases to load
aliases=(
  general
)

OMB_USE_SUDO=true
OMB_PROMPT_SHOW_PYTHON_VENV=false

source "$OSH/oh-my-bash.sh"
# ──────────────────────────────────────────────────────────────────────────────────
BASHRC_BLOCK
    echo "--- .bashrc configured."
fi

echo ""
echo "--- Setup complete on $(hostname)."
echo "--- Log out and SSH back in to activate the new prompt."
REMOTE_EOF

echo ""
echo "======================================================================"
echo "  Done! SSH back in to see the new prompt:"
echo "  ssh ${SSH_TARGET}"
echo "======================================================================"
