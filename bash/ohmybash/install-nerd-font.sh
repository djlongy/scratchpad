#!/usr/bin/env bash
# install-nerd-font.sh
# Installs the MesloLGS Nerd Font that the devops-powerline prompt draws its
# glyphs from.
#
# Vendor-first, the same way deploy.sh handles Oh My Bash: when
# ../../dotfiles/vendor/fonts/meslolgs-nf exists the four faces are copied out of
# it, so this works on a host with no internet. The ryanoasis/nerd-fonts release
# download is only a fallback for a checkout without the vendored tree, and the
# script logs which path it took.
#
# Usage:
#   ./install-nerd-font.sh                    # Install for current user
#   ./install-nerd-font.sh --system           # Install system-wide (requires sudo, Linux only)
#   ./install-nerd-font.sh --version 3.5.0    # Pin a release version (download fallback only)
#   ./install-nerd-font.sh --list             # Show what each path installs
#
# The font renders on the CLIENT. If you SSH into this host from elsewhere, the
# font your terminal uses is the one installed on the machine in front of you,
# and running this here changes nothing on screen. For a Windows client, use
# ../../dotfiles/windows/install-nerd-font.ps1.
#
# After install (Linux): configure your terminal emulator to use
#   "MesloLGS Nerd Font"
#
# After install (macOS): open Font Book or set directly in your terminal
#   (iTerm2 -> Preferences -> Profiles -> Text -> Font)
#
# Why Releases, not git clone, on the fallback path?
#   The ryanoasis/nerd-fonts repo stores font files via Git LFS (~3 GB total).
#   Cloning or sparse-checking out the patched-fonts/ tree requires git-lfs
#   and downloads multi-GB objects. The GitHub Releases publish per-family
#   zip files for Meslo -- no LFS, no extra tooling, just curl + unzip.

set -euo pipefail

# ── Defaults ──────────────────────────────────────────────────────────────────────
FONT_FAMILY="Meslo"
FONT_NAME="MesloLGS Nerd Font"
FONT_VERSION=""          # empty = fetch latest from GitHub API (fallback path only)
SYSTEM_INSTALL=false
LIST_ONLY=false

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Vendored payload, two levels up in the repository. Empty when this script is
# used standalone, which is what selects the download fallback.
VENDOR_FONTS=""
VENDOR_CANDIDATE="${SCRIPT_DIR}/../../dotfiles/vendor/fonts/meslolgs-nf"
[[ -f "${VENDOR_CANDIDATE}/MesloLGSNerdFont-Regular.ttf" ]] &&
  VENDOR_FONTS="$(cd "${VENDOR_CANDIDATE}" && pwd)"

# The four faces the vendored tree carries: small line gap, one family, real
# bold and italic cuts rather than synthesised ones.
VENDOR_FILES=(
    MesloLGSNerdFont-Regular.ttf
    MesloLGSNerdFont-Bold.ttf
    MesloLGSNerdFont-Italic.ttf
    MesloLGSNerdFont-BoldItalic.ttf
)

# ── Parse args ────────────────────────────────────────────────────────────────────
while [[ $# -gt 0 ]]; do
    case "$1" in
        --system)    SYSTEM_INSTALL=true ;;
        --list)      LIST_ONLY=true ;;
        --version)   FONT_VERSION="$2"; shift ;;
        --version=*) FONT_VERSION="${1#*=}" ;;
        -h|--help)
            sed -n '2,/^set -/p' "$0" | grep '^#' | sed 's/^# \?//'
            exit 0
            ;;
        *) echo "Unknown option: $1" >&2; exit 1 ;;
    esac
    shift
done

# ── Detect OS and install directory ──────────────────────────────────────────────
OS="$(uname -s)"
case "$OS" in
    Linux)
        if $SYSTEM_INSTALL; then
            FONT_DIR="/usr/local/share/fonts/NerdFonts"
        else
            FONT_DIR="${HOME}/.local/share/fonts/NerdFonts"
        fi
        ;;
    Darwin)
        if $SYSTEM_INSTALL; then
            FONT_DIR="/Library/Fonts"
        else
            FONT_DIR="${HOME}/Library/Fonts"
        fi
        ;;
    *)
        echo "ERROR: Unsupported OS: ${OS}" >&2
        echo "       Supported: Linux, macOS (Darwin)" >&2
        echo "       Windows clients: ../../dotfiles/windows/install-nerd-font.ps1" >&2
        exit 1
        ;;
esac

# ── List mode ─────────────────────────────────────────────────────────────────────
if $LIST_ONLY; then
    if [[ -n "$VENDOR_FONTS" ]]; then
        echo "Source: VENDORED tree at ${VENDOR_FONTS} (offline, no network)"
        echo ""
        echo "Faces installed:"
        for file in "${VENDOR_FILES[@]}"; do
            echo "  ${file}"
        done
        echo ""
        echo "All four report family '${FONT_NAME}' -- small line gap, the cut"
        echo "recommended for terminals. Provenance and checksums: ${VENDOR_FONTS}/UPSTREAM.md"
    else
        echo "Source: NETWORK download (no vendored tree at ${VENDOR_CANDIDATE})"
        echo ""
        echo "Meslo Nerd Font variants (all included in ${FONT_FAMILY}.zip):"
        echo "  MesloLGS   — Small line gap  (recommended for most terminals)"
        echo "  MesloLGM   — Medium line gap"
        echo "  MesloLGL   — Large line gap"
        echo "  MesloLGSDZ — Small + dotted/slashed zero"
        echo "  MesloLGMDZ — Medium + dotted/slashed zero"
        echo "  MesloLGLDZ — Large + dotted/slashed zero"
        echo ""
        echo "Each comes in: Regular, Bold, Italic, Bold Italic"
    fi
    echo ""
    echo "For terminal use, set your terminal font to:"
    echo "  '${FONT_NAME}'  (after install)"
    exit 0
fi

# ── Install ───────────────────────────────────────────────────────────────────────
# One of two paths, announced before it runs so an offline failure is never a
# mystery about where the fonts were meant to come from.
install_from_vendor() {
    echo "==> Source: VENDORED tree at ${VENDOR_FONTS} (offline, no network)"
    echo "==> Installing to: ${FONT_DIR}"

    if $SYSTEM_INSTALL; then
        sudo mkdir -p "$FONT_DIR"
    else
        mkdir -p "$FONT_DIR"
    fi

    for file in "${VENDOR_FILES[@]}"; do
        src="${VENDOR_FONTS}/${file}"
        if [[ ! -f "$src" ]]; then
            echo "ERROR: vendored tree is incomplete — missing ${src}" >&2
            exit 1
        fi
        if $SYSTEM_INSTALL; then
            sudo install -m 0644 "$src" "${FONT_DIR}/${file}"
        else
            install -m 0644 "$src" "${FONT_DIR}/${file}"
        fi
        echo "    ${file}"
    done
}

install_from_download() {
    echo "==> Source: NETWORK download from GitHub Releases (no vendored tree at ${VENDOR_CANDIDATE} — this needs internet)"

    if ! command -v curl &>/dev/null; then
        echo "ERROR: curl is required for the download fallback but not found." >&2
        echo "       Install curl, or run this from a checkout that carries" >&2
        echo "       dotfiles/vendor/fonts/meslolgs-nf and needs no network." >&2
        exit 1
    fi
    if ! command -v unzip &>/dev/null; then
        echo "ERROR: unzip is required for the download fallback but not found." >&2
        echo "       Install unzip, or run this from a checkout that carries" >&2
        echo "       dotfiles/vendor/fonts/meslolgs-nf and needs no network." >&2
        exit 1
    fi

    if [[ -z "$FONT_VERSION" ]]; then
        echo "    Fetching latest nerd-fonts release version..."
        FONT_VERSION=$(
            curl -fsSL "https://api.github.com/repos/ryanoasis/nerd-fonts/releases/latest" \
            | grep '"tag_name"' \
            | sed 's/.*"v\([^"]*\)".*/\1/'
        )
        if [[ -z "$FONT_VERSION" ]]; then
            echo "ERROR: Could not determine latest version from GitHub API." >&2
            echo "       Specify manually: --version 3.5.0" >&2
            exit 1
        fi
        echo "    Latest: v${FONT_VERSION}"
    fi

    local download_url="https://github.com/ryanoasis/nerd-fonts/releases/download/v${FONT_VERSION}/${FONT_FAMILY}.zip"
    local tmp_dir zip_path
    tmp_dir="$(mktemp -d)"
    zip_path="${tmp_dir}/${FONT_FAMILY}.zip"
    trap 'rm -rf "$tmp_dir"' EXIT

    echo "    URL: ${download_url}"
    curl -fL --progress-bar -o "$zip_path" "$download_url"
    echo "    Downloaded: $(du -sh "$zip_path" | cut -f1)"

    echo "==> Installing to: ${FONT_DIR}"
    # Only the MesloLGS faces: the zip carries every line-gap and width variant,
    # and the rest are dead weight the prompt never asks for.
    if $SYSTEM_INSTALL; then
        sudo mkdir -p "$FONT_DIR"
        sudo unzip -o -j "$zip_path" 'MesloLGSNerdFont-*.ttf' -d "$FONT_DIR"
    else
        mkdir -p "$FONT_DIR"
        unzip -o -j "$zip_path" 'MesloLGSNerdFont-*.ttf' -d "$FONT_DIR"
    fi
}

if [[ -n "$VENDOR_FONTS" ]]; then
    install_from_vendor
else
    install_from_download
fi

# ── Refresh font cache (Linux only) ──────────────────────────────────────────────
if [[ "$OS" == "Linux" ]]; then
    echo ""
    echo "==> Refreshing font cache..."
    if command -v fc-cache &>/dev/null; then
        if $SYSTEM_INSTALL; then
            sudo fc-cache -f "$FONT_DIR"
        else
            fc-cache -f "$FONT_DIR"
        fi
        echo "    Done."
    else
        echo "    WARNING: fc-cache not found (install fontconfig)."
        echo "    You may need to log out and back in for fonts to appear."
    fi
fi

# ── Done ──────────────────────────────────────────────────────────────────────────
echo ""
echo "======================================================================"
echo "  ${FONT_NAME} installed."
echo "  Location: ${FONT_DIR}"
echo ""
if [[ "$OS" == "Darwin" ]]; then
    echo "  macOS next steps:"
    echo "  1. In iTerm2: Preferences → Profiles → Text → Font"
    echo "     Set font to: ${FONT_NAME}"
    echo "  2. In Terminal.app: Preferences → Profiles → Font"
    echo "     (iTerm2 recommended for best Nerd Font support)"
else
    echo "  Linux next steps:"
    echo "  1. Set your terminal font to: ${FONT_NAME}"
    echo "     GNOME Terminal: Edit → Preferences → Profile → Custom font"
    echo "     Konsole:        Settings → Edit Current Profile → Appearance"
    echo "     Alacritty:      font.family = '${FONT_NAME}'"
    echo "  2. Then set NLP_NERD_FONT=1 (default) in your .bashrc to enable"
    echo "     full Nerd Font powerline arrows in the devops-powerline prompt."
fi
echo ""
echo "  Remember the font is read by the terminal you are sitting at. On a host"
echo "  you only ever reach over SSH, installing it changes nothing on screen."
echo ""
echo "  To use Unicode fallback instead (no font change needed):"
echo "    Add to ~/.bashrc before 'source \"\$OSH/oh-my-bash.sh\"':"
echo "    export NLP_NERD_FONT=0"
echo "======================================================================"
