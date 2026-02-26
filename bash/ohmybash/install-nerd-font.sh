#!/usr/bin/env bash
# install-nerd-font.sh
# Downloads and installs MesloLGS Nerd Font from the ryanoasis/nerd-fonts
# GitHub Releases. No git clone, no LFS, no sudo required.
#
# Usage:
#   ./install-nerd-font.sh                    # Install for current user
#   ./install-nerd-font.sh --system           # Install system-wide (requires sudo, Linux only)
#   ./install-nerd-font.sh --version 3.3.0    # Pin a specific release version
#   ./install-nerd-font.sh --list             # Show available Meslo variants
#
# After install (Linux): configure your terminal emulator to use
#   "MesloLGS Nerd Font"  or  "MesloLGM Nerd Font"
#
# After install (macOS): open Font Book or set directly in your terminal
#   (iTerm2 → Preferences → Profiles → Text → Font)
#
# Why Releases, not git clone?
#   The ryanoasis/nerd-fonts repo stores font files via Git LFS (~3 GB total).
#   Cloning or sparse-checking out the patched-fonts/ tree requires git-lfs
#   and downloads multi-GB objects. The GitHub Releases publish per-family
#   zip files (~20 MB for Meslo) — no LFS, no extra tooling, just curl + unzip.
#
# Font variants installed from Meslo.zip:
#   MesloLGS Nerd Font         (Small line gap — recommended for terminals)
#   MesloLGM Nerd Font         (Medium line gap)
#   MesloLGL Nerd Font         (Large line gap)
#   MesloLGSDZ Nerd Font       (Dotted/slashed zero variant)
#   … Regular, Bold, Italic, Bold Italic for each

set -euo pipefail

# ── Defaults ──────────────────────────────────────────────────────────────────────
FONT_FAMILY="Meslo"
FONT_VERSION=""          # empty = fetch latest from GitHub API
SYSTEM_INSTALL=false
LIST_ONLY=false

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
        exit 1
        ;;
esac

# ── Preflight ─────────────────────────────────────────────────────────────────────
if ! command -v curl &>/dev/null; then
    echo "ERROR: curl is required but not found." >&2
    echo "       Install: sudo dnf install curl  (RHEL/AlmaLinux)" >&2
    echo "                sudo apt install curl  (Ubuntu/Debian)" >&2
    exit 1
fi

if ! command -v unzip &>/dev/null; then
    echo "ERROR: unzip is required but not found." >&2
    echo "       Install: sudo dnf install unzip  (RHEL/AlmaLinux)" >&2
    echo "                sudo apt install unzip  (Ubuntu/Debian)" >&2
    exit 1
fi

# ── Resolve version ───────────────────────────────────────────────────────────────
if [[ -z "$FONT_VERSION" ]]; then
    echo "==> Fetching latest nerd-fonts release version..."
    FONT_VERSION=$(
        curl -fsSL "https://api.github.com/repos/ryanoasis/nerd-fonts/releases/latest" \
        | grep '"tag_name"' \
        | sed 's/.*"v\([^"]*\)".*/\1/'
    )
    if [[ -z "$FONT_VERSION" ]]; then
        echo "ERROR: Could not determine latest version from GitHub API." >&2
        echo "       Specify manually: --version 3.3.0" >&2
        exit 1
    fi
    echo "    Latest: v${FONT_VERSION}"
fi

DOWNLOAD_URL="https://github.com/ryanoasis/nerd-fonts/releases/download/v${FONT_VERSION}/${FONT_FAMILY}.zip"

# ── List mode ─────────────────────────────────────────────────────────────────────
if $LIST_ONLY; then
    echo "Font zip: ${DOWNLOAD_URL}"
    echo ""
    echo "Meslo Nerd Font variants (all included in Meslo.zip):"
    echo "  MesloLGS   — Small line gap  (recommended for most terminals)"
    echo "  MesloLGM   — Medium line gap"
    echo "  MesloLGL   — Large line gap"
    echo "  MesloLGSDZ — Small + dotted/slashed zero"
    echo "  MesloLGMDZ — Medium + dotted/slashed zero"
    echo "  MesloLGLDZ — Large + dotted/slashed zero"
    echo ""
    echo "Each comes in: Regular, Bold, Italic, Bold Italic"
    echo ""
    echo "For terminal use, set your terminal font to:"
    echo "  'MesloLGS Nerd Font'  (after install)"
    exit 0
fi

# ── Download ──────────────────────────────────────────────────────────────────────
TMP_DIR="$(mktemp -d)"
ZIP_PATH="${TMP_DIR}/${FONT_FAMILY}.zip"

trap 'rm -rf "$TMP_DIR"' EXIT

echo "==> Downloading ${FONT_FAMILY}.zip (v${FONT_VERSION})..."
echo "    URL: ${DOWNLOAD_URL}"
curl -fL --progress-bar -o "$ZIP_PATH" "$DOWNLOAD_URL"
echo "    Downloaded: $(du -sh "$ZIP_PATH" | cut -f1)"

# ── Install ───────────────────────────────────────────────────────────────────────
echo ""
echo "==> Installing to: ${FONT_DIR}"

if $SYSTEM_INSTALL && [[ "$OS" == "Linux" ]]; then
    sudo mkdir -p "$FONT_DIR"
    sudo unzip -o "$ZIP_PATH" "*.ttf" "*.otf" -d "$FONT_DIR" 2>/dev/null || \
    sudo unzip -o "$ZIP_PATH" -d "$FONT_DIR" 2>/dev/null
else
    mkdir -p "$FONT_DIR"
    # Extract only TTF/OTF files; ignore Windows-specific files
    unzip -o "$ZIP_PATH" "*.ttf" "*.otf" -d "$FONT_DIR" 2>/dev/null || \
    unzip -o "$ZIP_PATH" -d "$FONT_DIR" 2>/dev/null
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
echo "  Meslo Nerd Font installed successfully."
echo "  Location: ${FONT_DIR}"
echo ""
if [[ "$OS" == "Darwin" ]]; then
    echo "  macOS next steps:"
    echo "  1. In iTerm2: Preferences → Profiles → Text → Font"
    echo "     Set font to: MesloLGS Nerd Font"
    echo "  2. In Terminal.app: Preferences → Profiles → Font"
    echo "     (iTerm2 recommended for best Nerd Font support)"
else
    echo "  Linux next steps:"
    echo "  1. Set your terminal font to: MesloLGS Nerd Font"
    echo "     GNOME Terminal: Edit → Preferences → Profile → Custom font"
    echo "     Konsole:        Settings → Edit Current Profile → Appearance"
    echo "     Alacritty:      font.family = 'MesloLGS Nerd Font'"
    echo "  2. Then set NLP_NERD_FONT=1 (default) in your .bashrc to enable"
    echo "     full Nerd Font powerline arrows in the devops-powerline prompt."
fi
echo ""
echo "  To use Unicode fallback instead (no font change needed):"
echo "    Add to ~/.bashrc before 'source \"\$OSH/oh-my-bash.sh\"':"
echo "    export NLP_NERD_FONT=0"
echo "======================================================================"
