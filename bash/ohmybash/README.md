# Oh My Bash — devops-powerline theme

Powerline-style two-line bash prompt. No packages required — just bash and a Nerd Font on your SSH client. Oh My Bash itself is vendored into this repository, so the whole thing installs on an air-gapped host.

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
| Nerd Font on client terminal | MesloLGS NF, Hack NF, JetBrainsMono NF, etc. |
| UTF-8 locale | Standard on modern Linux |
| git | **Fallback only** — not needed when the vendored tree is present |
| make | **Never** — the vendored ble.sh is a prebuilt release |

Nothing here needs a package manager or a network. See *Offline by default* below.

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
2. Installs Oh My Bash into `~/.oh-my-bash` (skips if already installed)
3. Installs/updates the theme at `~/.oh-my-bash/custom/themes/devops-powerline/`
4. Appends or updates OMB config in `~/.bashrc`, with update checking switched off

Optional: with `--with-blesh`, it also installs `ble.sh` and enables inline autosuggestions/history prediction in `~/.bashrc`.

## Offline by default

Step 2 is **vendor-first**. When `../../dotfiles/vendor/oh-my-bash` exists — it does in
this repository — the framework is installed by copying that tree, so the whole deploy
runs on a host with no internet, no package manager and no `git`. The upstream clone is
only a fallback for a checkout that has no vendored trees. Every run says which path it
took:

```
  Oh My Bash source: vendored tree (offline) — /path/to/dotfiles/vendor/oh-my-bash
  - Source: VENDORED tree at /path/to/dotfiles/vendor/oh-my-bash (offline, no network)
```

versus

```
  Oh My Bash source: upstream clone (needs internet) — no vendored tree found
  - Source: NETWORK clone (no vendored tree found — this needs internet)
```

`--with-blesh` is vendor-first too, using the **prebuilt** ble.sh release under
`../../dotfiles/vendor/blesh`. Nothing is compiled, so `make` is never required.

Remote mode carries the same guarantee: the vendored trees are streamed over the SSH
connection as tarballs and unpacked into the staging directory, so the target host never
reaches for a network of its own.

Provenance for both vendored trees — upstream URL, commit or release, checksum, and how
to refresh them — is recorded in `UPSTREAM.md` beside each one.

### No update prompts, ever

A vendored framework must not try to update itself, and an air-gapped host has nothing to
update from. The config written to `~/.bashrc` sets `DISABLE_AUTO_UPDATE=true`, which is
the switch Oh My Bash tests before sourcing `tools/check_for_upgrade.sh` at all, plus a
very large `UPDATE_OSH_DAYS` as a second line of defence.

`DISABLE_UPDATE_PROMPT` is deliberately left **false**. Despite the name it does not mean
"stay quiet" — setting it true makes Oh My Bash run the upgrade *without asking*, which is
the opposite of what an offline host wants.

### For a fully unattended install

`deploy.sh` is the interactive, one-host tool. To provision from the repository as a
whole, use `dotfiles/install.sh`, which runs `dotfiles/install.d/20-bash.sh`. That hook
does the same job with no network commands in any executed path, writes its config into a
marker-delimited block so re-runs replace rather than duplicate it, and leaves anything
you have put in `~/.oh-my-bash/custom` untouched.

### `--for-root` — keep the prompt alive under sudo

The theme rebuilds `PS1` every prompt via `PROMPT_COMMAND`. When you `sudo -i` / `sudo bash` / `sudo su -` to root, root's shell sources `/root/.bashrc` — and if that file doesn't load Oh My Bash, no `PROMPT_COMMAND` is registered and `PS1` freezes at whatever the parent shell inherited. The current working directory stops updating, which is confusing while troubleshooting.

`--for-root` runs the same install a second time against `/root` via `sudo -H bash`, so root gets its own `/root/.bashrc`, `/root/.oh-my-bash`, and theme copy. After deploying:

- Locally: `sudo -i` (or `sudo bash`) picks up the new prompt.
- Remotely: SSH in, then `sudo -i` — same result.

Requires sudo (locally, or on the remote box). Without `NOPASSWD`, remote runs allocate a TTY (`ssh -t`) so sudo can prompt for the password.

Open a new shell (or run `source ~/.bashrc`) after deployment.

## Updating the theme

Edit `../../dotfiles/bash/theme/devops-powerline/devops-powerline.theme.bash` locally, then re-run `deploy.sh`. It overwrites only the theme file and updates `OSH_THEME` as needed.

## Tested distros

| Distro | Status |
|--------|--------|
| AlmaLinux 9 | ✓ |
| RHEL 8 | ✓ |
| RHEL 9 | ✓ |
| Oracle Linux 8 | ✓ |
| Ubuntu 20.04 | ✓ |
| Ubuntu 22.04+ | ✓ |

The offline path is verified on AlmaLinux 9 x86_64 in a container with no network
interface and with neither `git` nor `make` installed.

### One caveat with `--with-blesh`

ble.sh needs a set of POSIX tools on `PATH`, including `ps`. Every real RHEL-family
install has them, but a stripped container may not, and ble.sh reports the problem on
stderr at *every* shell start. Both installers check the list first and skip ble.sh with
a warning rather than wire a permanently noisy line into `~/.bashrc`.

The first ble.sh-enabled shell prints cache-building progress (`updating tput cache…`,
`updating binders…`). That is one-time work, not an error; later shells are silent.

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
└── install-nerd-font.sh                   # Terminal font for the powerline glyphs
```

Everything `deploy.sh` installs — the theme and both vendored trees — lives in the
dotfiles package, which is what gets bundled for an air-gapped host:

```
dotfiles/
├── bash/theme/
│   └── devops-powerline/
│       └── devops-powerline.theme.bash    # The OMB theme
├── install.d/20-bash.sh                   # Unattended, fully offline install hook
├── uninstall.d/20-bash.sh                 # Reverses it, keeping ~/.oh-my-bash/custom
└── vendor/
    ├── oh-my-bash/                        # Upstream working tree + UPSTREAM.md
    └── blesh/                             # Prebuilt ble.sh release + UPSTREAM.md
```

The dependency runs one way: `deploy.sh` reaches into the package, and nothing in
the package reaches back out here. That is what lets the package be extracted on
its own as `~/.dotfiles` and still work.
