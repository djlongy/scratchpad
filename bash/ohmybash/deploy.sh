#!/usr/bin/env bash
# deploy.sh - Install Oh My Bash + devops-powerline theme
#
# Local:
#   ./deploy.sh
#   ./deploy.sh --local
#   ./deploy.sh --local --with-blesh
#   ./deploy.sh --local --for-root              # also install for /root via sudo
#
# Remote:
#   ./deploy.sh --remote host01.example.com user
#   ./deploy.sh --remote host01.example.com user --with-blesh
#   ./deploy.sh --remote host01.example.com user --for-root
#
# Backward-compatible remote positional args:
#   ./deploy.sh host01.example.com user
#
# --for-root: also installs OMB+theme for /root, so the prompt rebuilds via
# PROMPT_COMMAND under `sudo -i` / `sudo -s` / `sudo bash` / `sudo su -`.

set -euo pipefail

THEME_NAME="devops-powerline"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
THEME_SRC="${SCRIPT_DIR}/theme/${THEME_NAME}/${THEME_NAME}.theme.bash"

usage() {
  cat <<'EOF'
Usage:
  ./deploy.sh
  ./deploy.sh --local
  ./deploy.sh --remote <host> [user]
  ./deploy.sh [--local|--remote <host> [user]] [--with-blesh|--without-blesh] [--for-root]

Examples:
  ./deploy.sh
  ./deploy.sh --remote host01.example.com user
  ./deploy.sh --local --with-blesh
  ./deploy.sh --local --for-root
  ./deploy.sh --remote host01.example.com user --for-root --with-blesh

Notes:
  - Default behavior is local install.
  - ble.sh autosuggestions are optional and off by default.
  - --for-root also installs OMB+theme for /root so the prompt rebuilds
    after sudo'ing to root (requires sudo locally / on the remote box).
  - Positional args are still accepted for remote mode:
      ./deploy.sh <host> [user]
EOF
}

ensure_theme_exists() {
  if [[ ! -f "${THEME_SRC}" ]]; then
    echo "ERROR: Theme file not found: ${THEME_SRC}"
    echo "Run from the ohmybash directory or check theme/ subdirectory."
    exit 1
  fi
}

# Shared install body. Runs against ${HOME} with env: THEME_NAME, THEME_STAGE, WITH_BLESH.
# Captured with single-quoted heredoc so nothing expands at capture time.
OMB_INSTALL_BODY=$(cat <<'OMB_BODY'
set -euo pipefail

OMB_DIR="${HOME}/.oh-my-bash"
BASHRC="${HOME}/.bashrc"
BACKUP="${BASHRC}.pre-omb-$(date +%Y%m%d%H%M%S)"

echo "  → HOME=${HOME}  OMB_DIR=${OMB_DIR}"

if [[ ! -d "${OMB_DIR}" ]]; then
  command -v git >/dev/null 2>&1 || { echo "ERROR: git not found"; exit 1; }
  git clone --depth=1 https://github.com/ohmybash/oh-my-bash.git "${OMB_DIR}"
else
  echo "  - Oh My Bash already at ${OMB_DIR}; skipping clone"
fi

mkdir -p "${OMB_DIR}/custom/themes/${THEME_NAME}"
cp "${THEME_STAGE}" "${OMB_DIR}/custom/themes/${THEME_NAME}/${THEME_NAME}.theme.bash"
echo "  - Theme installed at ${OMB_DIR}/custom/themes/${THEME_NAME}/"

if grep -q 'oh-my-bash.sh' "${BASHRC}" 2>/dev/null; then
  cp "${BASHRC}" "${BACKUP}"
  echo "  - Existing OMB config detected; backup at ${BACKUP}"
  if grep -q '^OSH_THEME=' "${BASHRC}"; then
    sed -i "s|^OSH_THEME=.*|OSH_THEME=\"${THEME_NAME}\"|" "${BASHRC}"
    echo "  - Updated OSH_THEME to ${THEME_NAME}"
  else
    sed -i "/oh-my-bash.sh/i OSH_THEME=\"${THEME_NAME}\"" "${BASHRC}"
    echo "  - Inserted OSH_THEME=${THEME_NAME}"
  fi
else
  [[ -f "${BASHRC}" ]] && cp "${BASHRC}" "${BACKUP}" && echo "  - Backed up ${BASHRC} to ${BACKUP}"
  cat >>"${BASHRC}" <<'BASHRC_BLOCK'

# Oh My Bash
export OSH="${HOME}/.oh-my-bash"

OSH_THEME="devops-powerline"

plugins=(
  git
  bashmarks
  progress
)

completions=(
  git
  ssh
)

aliases=(
  general
)

OMB_USE_SUDO=true
OMB_PROMPT_SHOW_PYTHON_VENV=false

source "$OSH/oh-my-bash.sh"
BASHRC_BLOCK
  echo "  - Appended Oh My Bash block to ${BASHRC}"
fi

if [[ "${WITH_BLESH:-0}" == "1" ]]; then
  BLESH_DIR="${HOME}/.local/share/blesh"
  command -v git  >/dev/null 2>&1 || { echo "ERROR: --with-blesh: git missing";  exit 1; }
  command -v make >/dev/null 2>&1 || { echo "ERROR: --with-blesh: make missing (install via your OS package manager)"; exit 1; }
  mkdir -p "${HOME}/.local/share"
  if [[ ! -d "${BLESH_DIR}/.git" ]]; then
    git clone --recursive --depth 1 https://github.com/akinomyoga/ble.sh.git "${BLESH_DIR}"
  fi
  if [[ ! -f "${BLESH_DIR}/ble.sh" ]]; then
    (cd "${BLESH_DIR}" && make install PREFIX="${HOME}/.local")
  fi
  touch "${BASHRC}"
  sed -i '/local\/share\/blesh\/ble.sh/d;/ble-attach/d;/BLE_VERSION/d' "${BASHRC}"
  printf '%s\n' '[[ $- == *i* ]] && source -- "$HOME/.local/share/blesh/ble.sh" --attach=none' >>"${BASHRC}"
  printf '%s\n' '[[ ! ${BLE_VERSION-} ]] || ble-attach' >>"${BASHRC}"
  echo "  - Enabled ble.sh autosuggestions in ${BASHRC}"
fi

echo "  ✓ Install complete for ${HOME}"
OMB_BODY
)

# Build a self-contained install script with env assignments prepended.
# The result can be piped to any bash interpreter (local, sudo, ssh) without
# needing env-passthrough.
build_install_script() {
  local theme_stage="$1"
  printf 'THEME_NAME=%q\nTHEME_STAGE=%q\nWITH_BLESH=%q\nexport THEME_NAME THEME_STAGE WITH_BLESH\n\n%s\n' \
    "${THEME_NAME}" "${theme_stage}" "${WITH_BLESH}" "${OMB_INSTALL_BODY}"
}

install_local() {
  local stage_dir stage
  stage_dir="$(mktemp -d)"
  stage="${stage_dir}/${THEME_NAME}.theme.bash"
  cp "${THEME_SRC}" "${stage}"
  chmod 644 "${stage}"

  echo "======================================================================"
  echo "  Installing Oh My Bash + ${THEME_NAME} theme (LOCAL — $(whoami))"
  echo "  Host: $(hostname)"
  echo "======================================================================"
  build_install_script "${stage}" | bash

  if [[ "${FOR_ROOT}" == "1" ]]; then
    if [[ ${EUID} -eq 0 ]]; then
      echo ""
      echo "  - --for-root: current user is already root; nothing extra to do."
    else
      echo ""
      echo "======================================================================"
      echo "  Installing Oh My Bash + ${THEME_NAME} theme (LOCAL — root via sudo)"
      echo "======================================================================"
      # sudo reads its password prompt from /dev/tty, so piping the script
      # to its stdin is fine.
      build_install_script "${stage}" | sudo -H bash
    fi
  fi

  rm -rf "${stage_dir}"

  echo ""
  echo "Done. Open a new shell (or run: source ~/.bashrc) to activate."
  if [[ "${FOR_ROOT}" == "1" && ${EUID} -ne 0 ]]; then
    echo "For root: try \`sudo -i\` or \`sudo bash\` and the prompt should rebuild."
  fi
}

install_remote() {
  local target_host="$1"
  local target_user="$2"
  local ssh_target="${target_user}@${target_host}"
  local remote_dir="/tmp/${THEME_NAME}-stage"
  local remote_theme="${remote_dir}/${THEME_NAME}.theme.bash"
  local remote_script="${remote_dir}/install.sh"

  echo "======================================================================"
  echo "  Installing Oh My Bash + ${THEME_NAME} theme (REMOTE)"
  echo "  Target: ${ssh_target}"
  echo "======================================================================"

  # Stage theme + install script on the remote host with world-readable perms
  # so both ${target_user} and root can read them.
  ssh "${ssh_target}" "mkdir -p ${remote_dir} && chmod 755 ${remote_dir}"
  scp -q "${THEME_SRC}" "${ssh_target}:${remote_theme}"

  local local_tmp
  local_tmp="$(mktemp)"
  build_install_script "${remote_theme}" >"${local_tmp}"
  scp -q "${local_tmp}" "${ssh_target}:${remote_script}"
  rm -f "${local_tmp}"
  ssh "${ssh_target}" "chmod 644 ${remote_theme} ${remote_script}"

  # User install — no sudo, no TTY needed.
  ssh "${ssh_target}" "bash ${remote_script}"

  if [[ "${FOR_ROOT}" == "1" ]]; then
    echo ""
    echo "======================================================================"
    echo "  Installing Oh My Bash + ${THEME_NAME} theme (REMOTE — root via sudo)"
    echo "  Target: root@${target_host} (via ${ssh_target} + sudo)"
    echo "======================================================================"
    # Allocate a TTY so sudo can prompt for a password if NOPASSWD is not set.
    ssh -t "${ssh_target}" "sudo -H bash ${remote_script}"
  fi

  ssh "${ssh_target}" "rm -rf ${remote_dir}"

  echo ""
  echo "Done. Reconnect to activate: ssh ${ssh_target}"
  if [[ "${FOR_ROOT}" == "1" ]]; then
    echo "For root: once SSH'd in, \`sudo -i\` or \`sudo bash\` will pick up the new prompt."
  fi
}

MODE="local"
TARGET_HOST=""
TARGET_USER="user"
WITH_BLESH="0"
FOR_ROOT="0"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --local)
      MODE="local"
      shift
      ;;
    --remote)
      MODE="remote"
      [[ $# -ge 2 ]] || {
        echo "ERROR: --remote requires <host> [user]"
        usage
        exit 1
      }
      TARGET_HOST="$2"
      shift 2
      if [[ $# -gt 0 && "$1" != --* ]]; then
        TARGET_USER="$1"
        shift
      fi
      ;;
    --with-blesh)
      WITH_BLESH="1"
      shift
      ;;
    --without-blesh)
      WITH_BLESH="0"
      shift
      ;;
    --for-root)
      FOR_ROOT="1"
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      MODE="remote"
      TARGET_HOST="$1"
      shift
      if [[ $# -gt 0 && "$1" != --* ]]; then
        TARGET_USER="$1"
        shift
      fi
      ;;
  esac
done

ensure_theme_exists

if [[ "${MODE}" == "local" ]]; then
  install_local
else
  [[ -n "${TARGET_HOST}" ]] || {
    echo "ERROR: remote mode requires a host"
    usage
    exit 1
  }
  install_remote "${TARGET_HOST}" "${TARGET_USER}"
fi
