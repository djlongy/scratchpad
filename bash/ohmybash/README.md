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
| make | Only needed when using `--with-blesh` |
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

# Local install + ble.sh autosuggestions (optional)
./deploy.sh --local --with-blesh

# Local install + also provision /root so sudo'ing keeps the prompt working
./deploy.sh --local --for-root

# Optional remote install
./deploy.sh --remote host01.example.com user

# Optional remote install + ble.sh autosuggestions
./deploy.sh --remote host01.example.com user --with-blesh

# Optional remote install + also provision /root on the remote box
./deploy.sh --remote host01.example.com user --for-root

# Backward-compatible remote syntax
./deploy.sh host01.example.com user
```

The script:
1. Backs up existing `~/.bashrc`
2. Clones Oh My Bash into `~/.oh-my-bash` (skips if already installed)
3. Installs/updates the theme at `~/.oh-my-bash/custom/themes/devops-powerline/`
4. Appends or updates OMB config in `~/.bashrc`

Optional: with `--with-blesh`, it also installs `ble.sh` and enables inline autosuggestions/history prediction in `~/.bashrc`.

### `--for-root` — keep the prompt alive under sudo

The theme rebuilds `PS1` every prompt via `PROMPT_COMMAND`. When you `sudo -i` / `sudo bash` / `sudo su -` to root, root's shell sources `/root/.bashrc` — and if that file doesn't load Oh My Bash, no `PROMPT_COMMAND` is registered and `PS1` freezes at whatever the parent shell inherited. The current working directory stops updating, which is confusing while troubleshooting.

`--for-root` runs the same install a second time against `/root` via `sudo -H bash`, so root gets its own `/root/.bashrc`, `/root/.oh-my-bash`, and theme copy. After deploying:

- Locally: `sudo -i` (or `sudo bash`) picks up the new prompt.
- Remotely: SSH in, then `sudo -i` — same result.

Requires sudo (locally, or on the remote box). Without `NOPASSWD`, remote runs allocate a TTY (`ssh -t`) so sudo can prompt for the password.

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
