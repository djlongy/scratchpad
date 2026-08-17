#!/usr/bin/env bash
# deploy.sh - Install Oh My Bash + devops-powerline theme
#
# Vendor-first: when ../../dotfiles/vendor/oh-my-bash exists this installs by
# copying that tree, so it works on a host with no internet. The upstream clone
# is only a fallback for a checkout without the vendored trees, and the script
# logs which path it took.
#
# The theme ships inside the dotfiles package (../../dotfiles/bash/theme) so the
# offline bundle is self-contained. This script reaches into that package for
# it; nothing in the package reaches back out here.
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
THEME_SRC="${SCRIPT_DIR}/../../dotfiles/bash/theme/${THEME_NAME}/${THEME_NAME}.theme.bash"

# Vendored payloads, two levels up in the repository. Empty when this script is
# used standalone, which is what selects the clone fallback.
VENDOR_ROOT="${SCRIPT_DIR}/../../dotfiles/vendor"
VENDOR_OMB=""
VENDOR_BLESH=""
[[ -f "${VENDOR_ROOT}/oh-my-bash/oh-my-bash.sh" ]] &&
  VENDOR_OMB="$(cd "${VENDOR_ROOT}/oh-my-bash" && pwd)"
[[ -f "${VENDOR_ROOT}/blesh/ble.sh" ]] &&
  VENDOR_BLESH="$(cd "${VENDOR_ROOT}/blesh" && pwd)"

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
    echo "The theme ships in the dotfiles package — keep this script beside a"
    echo "dotfiles/ directory containing bash/theme/${THEME_NAME}/."
    exit 1
  fi
}

# Shared install body. Runs against ${HOME} with env: THEME_NAME, THEME_STAGE,
# WITH_BLESH, OMB_VENDOR_SRC, BLESH_VENDOR_SRC. The two *_VENDOR_SRC paths are
# empty when no vendored tree is available, which selects the clone fallback.
# Captured with single-quoted heredoc so nothing expands at capture time.
OMB_INSTALL_BODY=$(cat <<'OMB_BODY'
set -euo pipefail

OMB_DIR="${HOME}/.oh-my-bash"
BASHRC="${HOME}/.bashrc"
BACKUP="${BASHRC}.pre-omb-$(date +%Y%m%d%H%M%S)"

echo "  → HOME=${HOME}  OMB_DIR=${OMB_DIR}"

if [[ ! -d "${OMB_DIR}" ]]; then
  if [[ -n "${OMB_VENDOR_SRC:-}" && -f "${OMB_VENDOR_SRC}/oh-my-bash.sh" ]]; then
    echo "  - Source: VENDORED tree at ${OMB_VENDOR_SRC} (offline, no network)"
    mkdir -p "${OMB_DIR}"
    cp -a "${OMB_VENDOR_SRC}/." "${OMB_DIR}/"
    # cp -a preserves ownership when the caller is root, which would leave the
    # tree owned by whoever owns the repository.
    chown -R "$(id -u):$(id -g)" "${OMB_DIR}" 2>/dev/null || true
  else
    echo "  - Source: NETWORK clone (no vendored tree found — this needs internet)"
    command -v git >/dev/null 2>&1 || { echo "ERROR: git not found"; exit 1; }
    git clone --depth=1 https://github.com/ohmybash/oh-my-bash.git "${OMB_DIR}"
  fi
else
  echo "  - Oh My Bash already at ${OMB_DIR}; leaving the framework as-is"
fi

# Runtime scratch dirs; Oh My Bash silently falls back to XDG paths without them.
mkdir -p "${OMB_DIR}/cache" "${OMB_DIR}/log"
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
  # A vendored framework must never poll for updates or prompt at shell start.
  if grep -q '^DISABLE_AUTO_UPDATE=' "${BASHRC}"; then
    sed -i "s|^DISABLE_AUTO_UPDATE=.*|DISABLE_AUTO_UPDATE=true|" "${BASHRC}"
  else
    sed -i "/oh-my-bash.sh/i DISABLE_AUTO_UPDATE=true" "${BASHRC}"
  fi
  echo "  - Update checking disabled (DISABLE_AUTO_UPDATE=true)"
else
  [[ -f "${BASHRC}" ]] && cp "${BASHRC}" "${BACKUP}" && echo "  - Backed up ${BASHRC} to ${BACKUP}"
  cat >>"${BASHRC}" <<'BASHRC_BLOCK'

# Oh My Bash
export OSH="${HOME}/.oh-my-bash"

OSH_THEME="devops-powerline"

# The framework is installed from a vendored copy, so never look for updates.
# DISABLE_AUTO_UPDATE gates tools/check_for_upgrade.sh entirely.
DISABLE_AUTO_UPDATE=true
# Belt and braces, in case something re-enables the check.
UPDATE_OSH_DAYS=100000
# DISABLE_UPDATE_PROMPT=true means "upgrade WITHOUT asking", not "stay quiet".
DISABLE_UPDATE_PROMPT=false

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

# ble.sh refuses to load without this exact set of POSIX tools and says so on
# stderr at every shell start, so check before wiring it into ~/.bashrc.
# Source: _ble_init_posix_command_list in ble.sh.
BLESH_REQUIRES="sed date rm mkdir mkfifo sleep stty tty sort awk chmod grep cat wc mv sh od cp ps"
if [[ "${WITH_BLESH:-0}" == "1" ]]; then
  blesh_missing=""
  for cmd in ${BLESH_REQUIRES}; do
    command -v "${cmd}" >/dev/null 2>&1 || blesh_missing="${blesh_missing} ${cmd}"
  done
  if [[ -n "${blesh_missing}" ]]; then
    echo "  ! ble.sh needs these commands and they are not on PATH:${blesh_missing}"
    echo "  ! Skipping ble.sh — enabling it would print an error at every shell start."
    WITH_BLESH=0
  fi
fi

if [[ "${WITH_BLESH:-0}" == "1" ]]; then
  BLESH_DIR="${HOME}/.local/share/blesh"
  mkdir -p "${HOME}/.local/share"
  if [[ -n "${BLESH_VENDOR_SRC:-}" && -f "${BLESH_VENDOR_SRC}/ble.sh" ]]; then
    echo "  - ble.sh source: VENDORED prebuilt release at ${BLESH_VENDOR_SRC} (offline, nothing compiled)"
    rm -rf "${BLESH_DIR}"
    mkdir -p "${BLESH_DIR}"
    cp -a "${BLESH_VENDOR_SRC}/." "${BLESH_DIR}/"
    chown -R "$(id -u):$(id -g)" "${BLESH_DIR}" 2>/dev/null || true
  else
    echo "  - ble.sh source: NETWORK clone + build (no vendored release found)"
    command -v git  >/dev/null 2>&1 || { echo "ERROR: --with-blesh: git missing";  exit 1; }
    command -v make >/dev/null 2>&1 || { echo "ERROR: --with-blesh: make missing (install via your OS package manager)"; exit 1; }
    if [[ ! -d "${BLESH_DIR}/.git" ]]; then
      git clone --recursive --depth 1 https://github.com/akinomyoga/ble.sh.git "${BLESH_DIR}"
    fi
    if [[ ! -f "${BLESH_DIR}/ble.sh" ]]; then
      (cd "${BLESH_DIR}" && make install PREFIX="${HOME}/.local")
    fi
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
  local omb_vendor="$2"
  local blesh_vendor="$3"
  printf 'THEME_NAME=%q\nTHEME_STAGE=%q\nWITH_BLESH=%q\nOMB_VENDOR_SRC=%q\nBLESH_VENDOR_SRC=%q\nexport THEME_NAME THEME_STAGE WITH_BLESH OMB_VENDOR_SRC BLESH_VENDOR_SRC\n\n%s\n' \
    "${THEME_NAME}" "${theme_stage}" "${WITH_BLESH}" "${omb_vendor}" "${blesh_vendor}" \
    "${OMB_INSTALL_BODY}"
}

# One line at the top of every run so it is obvious whether this was an offline
# install or a network one.
report_source() {
  if [[ -n "${VENDOR_OMB}" ]]; then
    echo "  Oh My Bash source: vendored tree (offline) — ${VENDOR_OMB}"
  else
    echo "  Oh My Bash source: upstream clone (needs internet) — no vendored tree found"
  fi
  if [[ "${WITH_BLESH}" == "1" ]]; then
    if [[ -n "${VENDOR_BLESH}" ]]; then
      echo "  ble.sh source:     vendored prebuilt release (offline) — ${VENDOR_BLESH}"
    else
      echo "  ble.sh source:     upstream clone + build (needs internet and make)"
    fi
  fi
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
  report_source
  echo "======================================================================"
  build_install_script "${stage}" "${VENDOR_OMB}" "${VENDOR_BLESH}" | bash

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
      build_install_script "${stage}" "${VENDOR_OMB}" "${VENDOR_BLESH}" | sudo -H bash
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
  report_source
  echo "======================================================================"

  # Stage theme + install script on the remote host with world-readable perms
  # so both ${target_user} and root can read them.
  ssh "${ssh_target}" "mkdir -p ${remote_dir} && chmod 755 ${remote_dir}"
  scp -q "${THEME_SRC}" "${ssh_target}:${remote_theme}"

  # Ship the vendored payloads as tarballs so the remote host never needs a
  # network of its own. tar/gzip are on every target this supports.
  local remote_omb="" remote_blesh=""
  if [[ -n "${VENDOR_OMB}" ]]; then
    remote_omb="${remote_dir}/oh-my-bash"
    tar -C "$(dirname "${VENDOR_OMB}")" -czf - "$(basename "${VENDOR_OMB}")" |
      ssh "${ssh_target}" "tar -xzf - -C ${remote_dir}"
    echo "  - Staged vendored Oh My Bash at ${ssh_target}:${remote_omb}"
  fi
  if [[ "${WITH_BLESH}" == "1" && -n "${VENDOR_BLESH}" ]]; then
    remote_blesh="${remote_dir}/blesh"
    tar -C "$(dirname "${VENDOR_BLESH}")" -czf - "$(basename "${VENDOR_BLESH}")" |
      ssh "${ssh_target}" "tar -xzf - -C ${remote_dir}"
    echo "  - Staged vendored ble.sh at ${ssh_target}:${remote_blesh}"
  fi

  local local_tmp
  local_tmp="$(mktemp)"
  build_install_script "${remote_theme}" "${remote_omb}" "${remote_blesh}" >"${local_tmp}"
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
