#!/usr/bin/env bash
# devops-powerline.theme.bash
# Oh My Bash theme — powerline-style two-line prompt
#
# Layout:
#   [blank separator line]
#   Line 1: [ ~/path →][ ⎇ branch ● →] ···fill··· [← ✓/✗][← ● venv][← ⊙ HH:MM:SS ]
#   Line 2: ❯ (input)
#
# Requires: Oh My Bash
# Works on:  AlmaLinux 9, RHEL 8/9, Oracle Linux 8, Ubuntu 20.04+
#
# NERD FONT MODE (default):
#   Requires a Nerd Font in your terminal emulator (MesloLGS NF recommended).
#   Uses Nerd Font powerline glyphs: U+E0B0 U+E0B2 U+E0A0
#
# UNICODE FALLBACK MODE:
#   Set NLP_NERD_FONT=0 in ~/.bashrc BEFORE the 'source oh-my-bash.sh' line.
#   Uses standard Unicode: ▶ ◀ ⎇  — renders on any terminal with no font install.
#
# Example .bashrc to force fallback:
#   export NLP_NERD_FONT=0
#   source "$OSH/oh-my-bash.sh"

# ── Symbol selection ─────────────────────────────────────────────────────────────
# NLP_NERD_FONT: 1 = Nerd Font glyphs (default), 0 = Unicode fallback
_DP_NERD_FONT="${NLP_NERD_FONT:-1}"

if [[ "$_DP_NERD_FONT" == "1" ]]; then
    # Nerd Font powerline glyphs (Nerd Font required)
    _DP_SEP_R=$'\xee\x82\xb0'     # U+E0B0  right-filled arrow
    _DP_SEP_L=$'\xee\x82\xb2'     # U+E0B2  left-filled arrow
    _DP_GIT_GLYPH=$'\xee\x82\xa0' # U+E0A0  git branch
else
    # Standard Unicode fallback — works on any terminal/font
    _DP_SEP_R='▶'                  # U+25B6
    _DP_SEP_L='◀'                  # U+25C0
    _DP_GIT_GLYPH='⎇'              # U+2387  (standard)
fi

# Standard Unicode — always available regardless of Nerd Font
_DP_OK='✓'
_DP_FAIL='✗'
_DP_CHEVRON='❯'
_DP_FILL_CHAR='·'
_DP_CLOCK='⊙'
_DP_VENV_DOT='●'

# ── Color palette (xterm-256) ─────────────────────────────────────────────────────
_DP_C_PATH_BG=33      # Blue        — path segment background
_DP_C_PATH_FG=255     # White       — path segment text
_DP_C_GIT_BG=214      # Amber       — git branch segment background
_DP_C_GIT_FG=235      # Near-black  — git branch segment text
_DP_C_OK_BG=64        # Green       — success status background
_DP_C_FAIL_BG=124     # Red         — failure status background
_DP_C_STATUS_FG=255   # White       — status segment text
_DP_C_VENV_BG=23      # Dark teal   — venv segment background
_DP_C_VENV_FG=255     # White       — venv segment text
_DP_C_VENV_DOT=43     # Cyan-green  — venv dot colour
_DP_C_TIME_BG=252     # Light gray  — time segment background
_DP_C_TIME_FG=235     # Near-black  — time segment text
_DP_C_FILL=240        # Dim gray    — fill character colour
_DP_C_CHEVRON=33      # Blue        — input chevron

# ── Color helpers ─────────────────────────────────────────────────────────────────
function __dp_bg() { printf '\[\e[48;5;%sm\]' "$1"; }
function __dp_fg() { printf '\[\e[38;5;%sm\]' "$1"; }
_DP_RESET='\[\e[0m\]'
_DP_BOLD='\[\e[1m\]'
_DP_DIM='\[\e[2m\]'

# ── Helpers ───────────────────────────────────────────────────────────────────────

# Returns current dir with ~ for home, truncated if deeply nested.
# Uses explicit if/elif — avoids bash 5.x delimiter ambiguity in ${var/#$HOME/~}
# when HOME contains slash characters.
function __dp_short_path() {
    local raw
    if [[ "$PWD" == "$HOME" ]]; then
        raw="~"
    elif [[ "$PWD" == "$HOME/"* ]]; then
        raw="~${PWD#"$HOME"}"
    else
        raw="$PWD"
    fi

    local max=45
    if [[ ${#raw} -le $max ]]; then
        echo "$raw"
        return
    fi

    # Truncate deep paths: keep root + first dir + … + last two components
    local IFS='/'
    read -ra parts <<< "$raw"
    local total=${#parts[@]}
    if [[ $total -gt 4 ]]; then
        echo "${parts[0]}/${parts[1]}/…/${parts[$((total-2))]}/${parts[$((total-1))]}"
    else
        echo "$raw"
    fi
}

# Returns branch name if inside a git repo, empty string otherwise
function __dp_git_branch() {
    git rev-parse --abbrev-ref HEAD 2>/dev/null
}

# Returns " ●" if working tree is dirty, empty otherwise
function __dp_git_dirty() {
    if ! git diff --quiet 2>/dev/null || ! git diff --cached --quiet 2>/dev/null; then
        echo " ●"
    fi
}

# Returns the active venv or conda env name, empty otherwise
function __dp_venv_name() {
    if [[ -n "${VIRTUAL_ENV:-}" ]]; then
        basename "$VIRTUAL_ENV"
    elif [[ -n "${CONDA_DEFAULT_ENV:-}" && "${CONDA_DEFAULT_ENV}" != "base" ]]; then
        echo "${CONDA_DEFAULT_ENV}"
    fi
}

# ── Prompt builder ────────────────────────────────────────────────────────────────
function _omb_theme_devops_powerline_ps1() {
    local exit_code="${_omb_exit_code:-$?}"
    local cols
    cols=$(tput cols 2>/dev/null || echo 80)

    # ── Status ────────────────────────────────────────────────────────────────────
    local status_char status_bg
    if [[ "$exit_code" -eq 0 ]]; then
        status_char=" ${_DP_OK} "
        status_bg=$_DP_C_OK_BG
    else
        status_char=" ${_DP_FAIL} "
        status_bg=$_DP_C_FAIL_BG
    fi

    # ── Left content (plain text for length calculation) ──────────────────────────
    local path_text
    path_text=" $(__dp_short_path) "

    local git_branch
    git_branch=$(__dp_git_branch)

    local git_text="" git_len=0
    if [[ -n "$git_branch" ]]; then
        local dirty
        dirty=$(__dp_git_dirty)
        git_text=" ${_DP_GIT_GLYPH} ${git_branch}${dirty} "
        git_len=${#git_text}
    fi

    # ── Right content (plain text for length calculation) ─────────────────────────
    local venv_name
    venv_name=$(__dp_venv_name)

    local venv_text="" venv_len=0
    if [[ -n "$venv_name" ]]; then
        venv_text=" ${_DP_VENV_DOT} ${venv_name} "
        venv_len=${#venv_text}
    fi

    local time_text
    time_text=" ${_DP_CLOCK} $(date '+%H:%M:%S') "

    # ── Fill length ───────────────────────────────────────────────────────────────
    # Left:  [path][→] + optional [git][→]          = 1-2 arrow chars
    # Right: [←][status] + optional [←][venv] + [←][time]  = 2-3 arrow chars
    local left_arrows=1
    [[ -n "$git_branch" ]] && (( left_arrows++ ))

    local right_arrows=2
    [[ -n "$venv_name" ]] && (( right_arrows++ ))

    local left_len=$(( ${#path_text} + git_len + left_arrows ))
    local right_len=$(( ${#status_char} + venv_len + ${#time_text} + right_arrows ))

    local fill_len=$(( cols - left_len - right_len ))
    [[ $fill_len -lt 1 ]] && fill_len=1

    local fill="" i=0
    while [[ $i -lt $fill_len ]]; do fill+="${_DP_FILL_CHAR}"; (( i++ )); done

    # ── ANSI sequences ────────────────────────────────────────────────────────────
    local BG_PATH BG_GIT BG_STATUS BG_VENV BG_TIME
    local FG_PATH FG_GIT FG_STATUS FG_VENV FG_VENV_DOT FG_TIME FG_FILL FG_CHEV

    BG_PATH=$(__dp_bg   $_DP_C_PATH_BG)
    BG_GIT=$(__dp_bg    $_DP_C_GIT_BG)
    BG_STATUS=$(__dp_bg $status_bg)
    BG_VENV=$(__dp_bg   $_DP_C_VENV_BG)
    BG_TIME=$(__dp_bg   $_DP_C_TIME_BG)

    FG_PATH=$(__dp_fg       $_DP_C_PATH_FG)
    FG_GIT=$(__dp_fg        $_DP_C_GIT_FG)
    FG_STATUS=$(__dp_fg     $_DP_C_STATUS_FG)
    FG_VENV=$(__dp_fg       $_DP_C_VENV_FG)
    FG_VENV_DOT=$(__dp_fg   $_DP_C_VENV_DOT)
    FG_TIME=$(__dp_fg       $_DP_C_TIME_FG)
    FG_FILL=$(__dp_fg       $_DP_C_FILL)
    FG_CHEV=$(__dp_fg       $_DP_C_CHEVRON)

    # Separator arrow colours.
    # The filled-arrow technique: the arrow character's FG = colour of the
    # segment being exited, BG = colour of the segment being entered.
    # This makes the arrow blend seamlessly between adjacent segment colours.
    # The same fg/bg logic works with ▶/◀ (Unicode fallback) as with Nerd Font
    # glyphs — the visual quality is slightly different but correct.
    local FG_PATH_CLOSE FG_GIT_ON_PATH FG_GIT_CLOSE
    local FG_STATUS_OPEN FG_VENV_ON_STATUS FG_TIME_ON_STATUS FG_TIME_ON_VENV

    FG_PATH_CLOSE=$(__dp_fg   $_DP_C_PATH_BG)    # blue     — path  → default
    FG_GIT_ON_PATH=$(__dp_fg  $_DP_C_GIT_BG)     # amber on path bg → git
    FG_GIT_CLOSE=$(__dp_fg    $_DP_C_GIT_BG)     # amber    — git   → default
    FG_STATUS_OPEN=$(__dp_fg  $status_bg)          # status-colour → open status
    FG_VENV_ON_STATUS=$(__dp_fg $_DP_C_VENV_BG)  # teal on status bg → venv
    FG_TIME_ON_STATUS=$(__dp_fg $_DP_C_TIME_BG)  # light on status bg → time (no venv)
    FG_TIME_ON_VENV=$(__dp_fg $_DP_C_TIME_BG)    # light on venv bg → time

    # ── Build line 1 ──────────────────────────────────────────────────────────────
    local line1=""

    # Path segment
    line1+="${BG_PATH}${FG_PATH}${path_text}${_DP_RESET}"

    if [[ -n "$git_branch" ]]; then
        line1+="${BG_PATH}${FG_GIT_ON_PATH}${_DP_SEP_R}${_DP_RESET}"
        line1+="${BG_GIT}${_DP_BOLD}${FG_GIT}${git_text}${_DP_RESET}"
        line1+="${FG_GIT_CLOSE}${_DP_SEP_R}${_DP_RESET}"
    else
        line1+="${FG_PATH_CLOSE}${_DP_SEP_R}${_DP_RESET}"
    fi

    # Fill
    line1+="${_DP_DIM}${FG_FILL}${fill}${_DP_RESET}"

    # Status segment
    line1+="${FG_STATUS_OPEN}${_DP_SEP_L}${_DP_RESET}"
    line1+="${BG_STATUS}${_DP_BOLD}${FG_STATUS}${status_char}${_DP_RESET}"

    if [[ -n "$venv_name" ]]; then
        line1+="${BG_STATUS}${FG_VENV_ON_STATUS}${_DP_SEP_L}${_DP_RESET}"
        line1+="${BG_VENV}${FG_VENV_DOT} ${_DP_VENV_DOT}${FG_VENV} ${venv_name} ${_DP_RESET}"
        line1+="${BG_VENV}${FG_TIME_ON_VENV}${_DP_SEP_L}${_DP_RESET}"
    else
        line1+="${BG_STATUS}${FG_TIME_ON_STATUS}${_DP_SEP_L}${_DP_RESET}"
    fi

    # Time segment
    line1+="${BG_TIME}${FG_TIME}${time_text}${_DP_RESET}"

    # ── PS1: blank separator + bar + chevron ──────────────────────────────────────
    PS1="\n${line1}\n${_DP_BOLD}${FG_CHEV}${_DP_CHEVRON}${_DP_RESET} "
}

_omb_util_add_prompt_command _omb_theme_devops_powerline_ps1
