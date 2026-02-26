# Oh My Bash — devops-powerline theme

Powerline-style two-line bash prompt. No packages required — just bash, git, and a Nerd Font on your SSH client.

## Prompt layout

```
                                    (blank separator line)
[ ~/path → ][ ⎇ branch ● → ] ·····fill····· [ ← ✓/✗ ][ ← ● venv ][ ← ⊙ HH:MM:SS ]
❯ _
```

- **Path segment** (blue): `~` for home, truncated if deeply nested
- **Git segment** (amber): branch name + `●` dirty indicator — hidden outside git repos
- **Fill**: dim dots spanning terminal width, keeping left/right anchored
- **Status** (green/red): `✓` last command succeeded, `✗` it failed
- **Venv segment** (teal): `● venv_name` — shown when a Python venv or conda env is active
- **Time** (light gray): current time at prompt render
- **Blank line**: always printed before the bar, so command output and prompt are cleanly separated
- **Input**: `❯` chevron on its own line

No `username@hostname`. No clutter.

## Requirements

| Requirement | Notes |
|-------------|-------|
| bash 4.2+ | Ships with RHEL 8+, AlmaLinux 8+, Ubuntu 20.04+ |
| git | For OMB installation (one-time) |
| Nerd Font on client terminal | MesloLGS NF, Hack NF, JetBrainsMono NF, etc. |
| UTF-8 locale | Standard on modern Linux |

The powerline arrow glyphs (``, ``) and the branch glyph (``) are Nerd Font codepoints. Everything else (`✓`, `✗`, `❯`, `·`) is standard Unicode — the prompt degrades gracefully if Nerd Fonts are missing.

## Deploy (local first)

```bash
cd bash/ohmybash
chmod +x deploy.sh

# Local install (default)
./deploy.sh

# Explicit local install
./deploy.sh --local

# Optional remote install
./deploy.sh --remote host01.example.com user

# Backward-compatible remote syntax
./deploy.sh host01.example.com user
```

The script:
1. Backs up existing `~/.bashrc`
2. Clones Oh My Bash into `~/.oh-my-bash` (skips if already installed)
3. Installs/updates the theme at `~/.oh-my-bash/custom/themes/devops-powerline/`
4. Appends or updates OMB config in `~/.bashrc`

Open a new shell (or run `source ~/.bashrc`) after deployment.

## Updating the theme

Edit `theme/devops-powerline/devops-powerline.theme.bash` locally, then re-run `deploy.sh`. It overwrites only the theme file and updates `OSH_THEME` as needed.

## Tested distros

| Distro | Status |
|--------|--------|
| AlmaLinux 9 | ✓ |
| RHEL 8 | ✓ |
| RHEL 9 | ✓ |
| Oracle Linux 8 | ✓ |
| Ubuntu 20.04 | ✓ |
| Ubuntu 22.04+ | ✓ |

## Customisation

Edit the colour palette at the top of the theme file:

```bash
_DP_C_PATH_BG=33      # Blue  — path segment
_DP_C_GIT_BG=214      # Amber — git segment
_DP_C_OK_BG=64        # Green — success
_DP_C_FAIL_BG=124     # Red   — failure
_DP_C_TIME_BG=252     # Light gray — time
```

Use [256-color xterm codes](https://www.ditig.com/256-colors-cheat-sheet) (0–255).

To change the fill character, edit `_DP_FILL_CHAR='·'`.

## Structure

```
bash/ohmybash/
├── README.md
├── deploy.sh                              # Local install by default; remote optional
└── theme/
    └── devops-powerline/
        └── devops-powerline.theme.bash    # The OMB theme
```
