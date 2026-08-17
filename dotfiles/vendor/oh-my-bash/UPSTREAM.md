# Vendored: Oh My Bash

Third-party code, checked in verbatim so `dotfiles/install.sh` can run on a host with
no internet, no package manager, and no `git`.

| | |
|---|---|
| Upstream | https://github.com/ohmybash/oh-my-bash |
| Commit | `7d26139293dea898a98cf4adacc19af4b0955145` |
| Commit date | 2026-08-01 |
| Vendored on | 2026-08-17 |
| Licence | MIT — see `LICENSE.md` in this directory (kept as upstream ships it) |

## What was removed from the upstream working tree

| Path | Why |
|---|---|
| `.git/` | Vendored as a plain tree, not a nested repository. |
| `.github/` | Upstream CI workflows and issue templates; not runtime code. |
| `.gitignore` | Upstream repo hygiene only. Left in place it would exclude the vendored `custom/`, `cache/` and `log/` directories from *this* repository, so the theme shipped under `custom/themes/` would never be committed. |

Everything else is byte-for-byte upstream. Do not hand-edit files in this directory —
local behaviour belongs in `dotfiles/install.d/20-bash.sh` or in `~/.oh-my-bash/custom`.

## Directories the installer creates rather than ships

`cache/` and `log/` are runtime scratch directories. They match ignore patterns in this
repository's root `.gitignore`, so they are not committed even though upstream carries a
`.gitkeep` in each. `dotfiles/install.d/20-bash.sh` creates both with `mkdir -p` at
install time, so nothing depends on them being present here.

## Refreshing this vendor tree

Run on a networked machine, from a temporary directory outside this repository:

```bash
git clone --depth=1 https://github.com/ohmybash/oh-my-bash.git omb
rm -rf omb/.git omb/.github omb/.gitignore
# replace dotfiles/vendor/oh-my-bash with omb/, then update the table above
```

Re-read `oh-my-bash.sh` and `tools/check_for_upgrade.sh` after any refresh: the managed
`~/.bashrc` block written by the installer disables update checking using variable names
that only this version guarantees.
