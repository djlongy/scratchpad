# Dotfiles (self-contained, offline-first)

Repeatable shell/tmux setup for air-gapped machines: no internet access, and
nothing needed beyond a `tmux` and `fzf` binary plus the OS base repos.
Everything else the installers need ships inside this repo — tmux plugins, the
Oh My Bash framework, ble.sh. `install.sh` never touches a network, a package
manager, or git.

## Layout

- `install.sh` — links the config packages (GNU Stow when available, plain
  symlinks otherwise), then runs each hook in `install.d/` in filename order.
- `install.d/10-tmux.sh` — copies the vendored tmux plugins into
  `~/.tmux/plugins`. If `tmux` is not on PATH it says so once, with the fix,
  and still lays the plugins down.
- `install.d/20-bash.sh` — copies the vendored Oh My Bash to `~/.oh-my-bash`,
  installs the `devops-powerline` theme, and writes a marker-delimited managed
  block to `~/.bashrc`.
- `vendor/` — the offline payload. Each vendored tree carries an `UPSTREAM.md`
  recording its upstream URL and pinned commit/version, alongside the upstream
  LICENSE.
- `tmux/` — the stow package with `.tmux.conf` and the `tmx-*` helper scripts.

## Requirements

- `tmux` — from your OS repos or any binary on PATH
- `fzf` — only for the sessionx picker; without it the binding simply does not
  appear (no error, no message)

Everything else is vendored. `stow` and `git` remain optional conveniences and
are never required.

## Install

```bash
cd dotfiles
./install.sh
```

Re-running is safe and idempotent: links are refreshed, plugin copies are
re-synced, and the `.bashrc` managed block is replaced between its markers
rather than duplicated.

### Air-gapped transfer

The canonical install location is `~/.dotfiles` — hidden, because the tree is
installer machinery, not something to browse day-to-day:

```bash
tar --no-xattrs -C dotfiles -czf dotfiles-bundle.tar.gz .
scp dotfiles-bundle.tar.gz user@host:
ssh user@host 'mkdir -p ~/.dotfiles && tar xzf dotfiles-bundle.tar.gz -C ~/.dotfiles && ~/.dotfiles/install.sh'
```

(`--no-xattrs` matters when bundling from macOS — without it the extract on
Linux prints hundreds of harmless but noisy xattr warnings. The package is
self-contained: the prompt theme, the git shortcut functions and both vendored
trees all ship inside it, so `~/.dotfiles` needs nothing beside it.)

The config files in `$HOME` are symlinks into this tree, so it stays for the
life of the install; deleting it breaks the links. If you unpacked somewhere
else first, just move the tree to `~/.dotfiles` and re-run
`~/.dotfiles/install.sh` — it re-points every link in one pass.

## tmux

### Plugins — no install step, no nags

TPM, tmux-sensible, tmux-resurrect, tmux-continuum and tmux-sessionx are
vendored under `vendor/tmux-plugins/` and copied into place by the installer.
There is no `prefix + I` plugin-install step — everything is present after
`install.sh`, and the config stays silent on a machine that never ran it
(missing plugins are skipped without a message). TPM's network keys
(`prefix + U`, `prefix + M-u`) are unbound since the vendored trees are not git
clones; `prefix + I` just tells you the plugins ship with the repo.

- **sessionx** — `prefix + O` appears only when `fzf` is on PATH. Without fzf:
  no binding, no error, no message.
- **resurrect/continuum** — `prefix + Ctrl-s` save, `prefix + Ctrl-r` restore;
  continuum auto-save hooks into the status line (its one deviation from stock
  `status-right`, invisible on screen).

### Appearance

The status bar keeps tmux's stock colours (green) so it is obvious at a glance
that you are inside tmux. A blue theme ships commented out in the status-line
section of `.tmux.conf` — uncomment those five lines to switch.

### Helpers

- `~/.local/bin/tmx-dev` — session bootstrap with shell + cheatsheet pane
- `~/.local/bin/tmx-cheatsheet` — shortcut reference (`Ctrl-b ?`)

**Copy `.tmux.conf` alone and the `Ctrl-b ?` cheatsheet (and the `tmx-dev`
cheatsheet pane) cannot work** — they run `~/.local/bin/tmx-cheatsheet`, which
only exists after `install.sh` has linked the scripts. The keybind says so
instead of flashing an empty pane.

### Copy behaviour

Mouse drag in copy-mode copies the selection and **stays at the scrolled
position** instead of snapping back to the live bottom
(`copy-selection-no-clear`). The clipboard is reached via tmux's OSC 52
passthrough (`set-clipboard on`), so it works over SSH with no
`xclip`/`wl-copy` installed. Press `q` (or scroll to the bottom) to leave
copy-mode.

## bash — Oh My Bash + devops-powerline prompt

Installs the Oh My Bash framework and the two-line powerline prompt from
`bash/theme/` inside this package. Entirely offline: the framework is vendored at
`vendor/oh-my-bash/` and copied into `~/.oh-my-bash`, so no network, package
manager, `git` or `make` is involved.

`install.d/20-bash.sh` writes a marker-delimited block to `~/.bashrc` holding
`OSH`, `OSH_THEME`, a guarded `~/.local/bin` PATH entry, and the
plugin/completion/alias lists. Re-runs replace that block rather than duplicate
it, and never touch `~/.oh-my-bash/custom`, so anything you add there survives.
A one-time backup is kept at `~/.bashrc.pre-dotfiles`.

Update checking is off: the block sets `DISABLE_AUTO_UPDATE=true`, the switch
Oh My Bash tests before loading its upgrade check. `DISABLE_UPDATE_PROMPT`
stays false on purpose — it means "upgrade without asking", not "stay quiet".

ble.sh (inline autosuggestions) is optional and off by default. Enable it with
`WITH_BLESH=1` or `--with-blesh`; it installs from the prebuilt release at
`vendor/blesh/`, so nothing is compiled. If the POSIX tools ble.sh requires
(including `ps`) are missing, it is skipped with a warning instead of being
wired into a shell where it would error on every start.

The installer also copies `bash/git-functions/git-functions.bash` to
`~/.oh-my-bash/custom/git-functions.sh`, giving every interactive shell the
`gci`, `gstash` (including `gstash delete`) and `gbp` shortcuts. Oh My Bash
sources everything in `custom/` automatically, so nothing is added to
`~/.bashrc` for it. The copy carries a marker line so the uninstaller removes
that one file and leaves the rest of `custom/` alone; a file you wrote yourself
at the same path is never overwritten. `gstash` and `gci` need `fzf` on PATH
and say so politely when it is absent.

For a single host, or a remote one over SSH, the repo's `bash/ohmybash/deploy.sh`
does the same framework+theme install interactively and is vendor-first in both
local and remote mode — it installs the theme from this package, so it needs a
full repo checkout.

## Nerd Font (the glyphs render on the CLIENT)

The powerline prompt and the tmux status line draw from a Nerd Font. That font
is read by your **terminal emulator**, which runs on the machine you are
sitting at — the remote host only sends codepoints down the wire and never
consults a font of its own. Installing the font on a server you reach over SSH
changes nothing on screen. This is the usual reason a prompt still shows boxes
after "installing the font".

MesloLGS Nerd Font is vendored at `vendor/fonts/meslolgs-nf`: the
small-line-gap cut with the **slashed** zero (the `DZ` families are the
dotted-zero alternative), four faces, Apache-2.0, checksums and provenance in
`UPSTREAM.md`. Nothing below needs a network.

### Windows client (Windows Terminal, PuTTY)

Per-user, no administrator rights:

```powershell
powershell -ExecutionPolicy Bypass -File .\windows\install-nerd-font.ps1
```

It copies the four faces to `%LOCALAPPDATA%\Microsoft\Windows\Fonts`,
registers them under `HKCU:\Software\Microsoft\Windows NT\CurrentVersion\Fonts`,
and loads them into the running session so no sign-out is needed. Re-running is
safe: unchanged files are left alone and registry values are rewritten only
when they differ, so nothing is ever duplicated.

It then **prints** the terminal configuration rather than applying it — your
`settings.json` is yours. Merge this into the top-level `profiles` object:

```json
"profiles": {
    "defaults": {
        "font": { "face": "MesloLGS Nerd Font" }
    }
}
```

The same setting without JSON: Settings → Defaults → Appearance → Font face.
PuTTY: Window → Appearance → Font → Change…, pick `MesloLGS Nerd Font`, and
save the session.

Undo with `.\windows\uninstall-nerd-font.ps1`. Both scripts accept `-WhatIf`.

### Linux / macOS

`install.sh` runs `install.d/30-fonts.sh`, which installs to
`~/.local/share/fonts/NerdFonts` (macOS: `~/Library/Fonts`) and refreshes the
fontconfig cache. On a host with no fontconfig — the normal state of a headless
server — it prints one line and skips, because nothing there would render with
the font anyway. That skip is expected, not a failure.

## Verify

```bash
tmux new-session -d -s t && tmux list-keys | grep -E 'resurrect|sessionx'
bash -ic 'echo $OSH_THEME'    # devops-powerline
```

## Uninstall

```bash
cd dotfiles
./uninstall.sh
```

Drops the config symlinks (stow when available, direct unlinking otherwise),
then runs `uninstall.d/*.sh`: plugin directories are removed only when they
carry the installer's marker, and saved tmux-resurrect sessions in
`~/.local/share/tmux/resurrect` are never touched. The bash hook removes the `.bashrc` managed block and the
framework files while keeping `~/.oh-my-bash/custom`.

## Save environment snapshot

```bash
./save-environment.sh   # writes environment-snapshot.txt; review before commit
```

## Refreshing the vendored payload

Each `vendor/**/UPSTREAM.md` records the pinned upstream URL and commit or
release. Refreshing is deliberate and manual: re-fetch upstream on a connected
machine, strip `.git`/`.github`, update `UPSTREAM.md`, and re-run the install
on a disconnected test box before committing.
