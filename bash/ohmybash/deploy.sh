#!/usr/bin/env bash
# deploy.sh - Install Oh My Bash + devops-powerline theme
#
# Local first:
#   ./deploy.sh
#   ./deploy.sh --local
#
# Optional remote deploy:
#   ./deploy.sh --remote host01.example.com user
#
# Backward-compatible remote positional args:
#   ./deploy.sh host01.example.com user

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

Examples:
  ./deploy.sh
  ./deploy.sh --remote host01.example.com user

Notes:
  - Default behavior is local install.
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

install_omb_if_needed() {
  local omb_dir="$1"
  if [[ -d "${omb_dir}" ]]; then
    echo "- Oh My Bash already installed at ${omb_dir}; skipping clone"
    return
  fi

  if ! command -v git >/dev/null 2>&1; then
    echo "ERROR: git not found. Install git first and re-run."
    exit 1
  fi

  echo "- Cloning Oh My Bash into ${omb_dir}"
  git clone --depth=1 https://github.com/ohmybash/oh-my-bash.git "${omb_dir}"
}

install_theme() {
  local omb_dir="$1"
  local theme_src="$2"
  local theme_dir="${omb_dir}/custom/themes/${THEME_NAME}"
  mkdir -p "${theme_dir}"
  cp "${theme_src}" "${theme_dir}/${THEME_NAME}.theme.bash"
  echo "- Installed theme at ${theme_dir}"
}

configure_bashrc() {
  local bashrc="$1"
  local backup="${bashrc}.pre-omb-$(date +%Y%m%d%H%M%S)"

  if grep -q 'oh-my-bash.sh' "${bashrc}" 2>/dev/null; then
    cp "${bashrc}" "${backup}"
    echo "- Existing OMB config detected; backup saved to ${backup}"

    if grep -q '^OSH_THEME=' "${bashrc}"; then
      sed -i "s|^OSH_THEME=.*|OSH_THEME=\"${THEME_NAME}\"|" "${bashrc}"
      echo "- Updated OSH_THEME to ${THEME_NAME}"
    else
      sed -i "/oh-my-bash.sh/i OSH_THEME=\"${THEME_NAME}\"" "${bashrc}"
      echo "- Inserted OSH_THEME=${THEME_NAME}"
    fi
    return
  fi

  [[ -f "${bashrc}" ]] && cp "${bashrc}" "${backup}" && echo "- Backed up ${bashrc} to ${backup}"

  cat >>"${bashrc}" <<'BASHRC_BLOCK'

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

  echo "- Appended Oh My Bash block to ${bashrc}"
}

install_local() {
  local omb_dir="${HOME}/.oh-my-bash"
  local bashrc="${HOME}/.bashrc"

  echo "======================================================================"
  echo "  Installing Oh My Bash + ${THEME_NAME} theme (LOCAL)"
  echo "  Host: $(hostname)  User: $(whoami)"
  echo "======================================================================"

  install_omb_if_needed "${omb_dir}"
  install_theme "${omb_dir}" "${THEME_SRC}"
  configure_bashrc "${bashrc}"

  echo ""
  echo "Done. Open a new shell (or run: source ~/.bashrc)"
}

install_remote() {
  local target_host="$1"
  local target_user="$2"
  local ssh_target="${target_user}@${target_host}"

  echo "======================================================================"
  echo "  Installing Oh My Bash + ${THEME_NAME} theme (REMOTE)"
  echo "  Target: ${ssh_target}"
  echo "======================================================================"

  ssh "${ssh_target}" "mkdir -p /tmp/${THEME_NAME}"
  scp "${THEME_SRC}" "${ssh_target}:/tmp/${THEME_NAME}/${THEME_NAME}.theme.bash"

  ssh "${ssh_target}" THEME_NAME="${THEME_NAME}" bash <<'REMOTE_EOF'
set -euo pipefail

OMB_DIR="${HOME}/.oh-my-bash"
BASHRC="${HOME}/.bashrc"
BACKUP="${BASHRC}.pre-omb-$(date +%Y%m%d%H%M%S)"

if [[ ! -d "${OMB_DIR}" ]]; then
  command -v git >/dev/null 2>&1 || {
    echo "ERROR: git not found on remote host"
    exit 1
  }
  git clone --depth=1 https://github.com/ohmybash/oh-my-bash.git "${OMB_DIR}"
fi

mkdir -p "${OMB_DIR}/custom/themes/${THEME_NAME}"
cp "/tmp/${THEME_NAME}/${THEME_NAME}.theme.bash" "${OMB_DIR}/custom/themes/${THEME_NAME}/"
rm -rf "/tmp/${THEME_NAME}"

if grep -q 'oh-my-bash.sh' "${BASHRC}" 2>/dev/null; then
  cp "${BASHRC}" "${BACKUP}"
  if grep -q '^OSH_THEME=' "${BASHRC}"; then
    sed -i "s|^OSH_THEME=.*|OSH_THEME=\"${THEME_NAME}\"|" "${BASHRC}"
  else
    sed -i "/oh-my-bash.sh/i OSH_THEME=\"${THEME_NAME}\"" "${BASHRC}"
  fi
else
  [[ -f "${BASHRC}" ]] && cp "${BASHRC}" "${BACKUP}"
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
fi

echo "Remote install complete on $(hostname)"
REMOTE_EOF

echo ""
echo "Done. Reconnect to activate prompt: ssh ${ssh_target}"
}

MODE="local"
TARGET_HOST=""
TARGET_USER="user"

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
