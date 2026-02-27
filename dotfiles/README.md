# Dotfiles (GNU Stow)

Stow-managed dotfiles for repeatable workstation/server setup.

Goal: copy this repo to a new machine and recreate the same shell/tmux workflow
without manually editing config files.

## Why this layout

- Each folder is a Stow package (`tmux`, more packages can be added later).
- Files inside each package mirror their destination under `$HOME`.
- Symlinks make updates simple and reversible.

## Current package

- `tmux`
  - `.tmux.conf`
  - `.local/bin/tmx-dev` (session bootstrap with shell + cheatsheet pane)
  - `.local/bin/tmx-cheatsheet` (shortcut reference shown in pane)
  - `.local/bin/tmx-install-tpm` (optional TPM installer)

## Requirements

- `tmux`
- `stow`

Optional but recommended:

- `fzf`
- `git`

## Install

```bash
cd dotfiles
./install.sh
```

This runs `stow --target "$HOME" tmux`.

## Detailed setup on a new machine

1) Install minimum dependencies:

```bash
# RHEL/Alma/Rocky
sudo dnf install -y git tmux stow

# Debian/Ubuntu
sudo apt-get update && sudo apt-get install -y git tmux stow
```

2) Clone your repo and apply dotfiles:

```bash
git clone <your-repo-url> ~/scratchpad
cd ~/scratchpad/dotfiles
chmod +x install.sh uninstall.sh save-environment.sh tmux/.local/bin/tmx-dev tmux/.local/bin/tmx-cheatsheet tmux/.local/bin/tmx-install-tpm
./install.sh
```

3) Start tmux with the default layout:

```bash
~/.local/bin/tmx-dev
```

This starts one session with one window split into two panes:

- Left pane: terminal in your workspace
- Right pane: tmux shortcut cheatsheet

Kill the cheatsheet pane any time with `Ctrl-b x`.
Inside cheatsheet pane: use `/` to search, `n`/`N` for next/previous match, mouse wheel to scroll, `q` to quit.

4) Optional plugin path (only if GitHub access is allowed):

```bash
~/.local/bin/tmx-install-tpm
tmux
# inside tmux: prefix + I
```

5) Verify:

```bash
tmux -V
tmux source-file ~/.tmux.conf
tmux list-keys | grep sessionx || true
```

If `sessionx` bindings are not listed, TPM/plugins are not installed yet; base tmux workflow still works.

## Save environment on current machine and push to git

1) Ensure stow links are active:

```bash
cd ~/scratchpad/dotfiles
./install.sh
```

2) Save a tool-version snapshot:

```bash
./save-environment.sh
```

3) Review changes and sanitize machine-specific values if needed.

4) Commit and push:

```bash
cd ~/scratchpad
git status
git add dotfiles
git commit -m "Add tmux dotfiles workflow with stow"
git push
```

## Re-apply after changes

```bash
cd dotfiles
./install.sh
```

## Remove symlinks

```bash
cd dotfiles
./uninstall.sh
```

## Restricted environment notes

- This setup works without downloading tmux plugins.
- The tmux config is fully usable with stock tmux.
- If internet policy allows GitHub and you later want plugin support, add TPM/plugins as a separate optional step.
- Keep host/user/domain values generic in docs/scripts before committing.
