# Vendored: tmux-plugins/tpm

| | |
|---|---|
| Upstream | <https://github.com/tmux-plugins/tpm> |
| Commit | `e261deb1b47614eed3400089ce7197dc68acc4eb` |
| Describes as | `v3.0.0-78-ge261deb` |
| Commit date | 2026-05-17 |
| Vendored | 2026-08-17 |
| Licence | MIT — see `LICENSE.md`, kept unmodified |

Tree copied from upstream with `.git/` and `.github/` removed. Nothing else was
edited; this file is the only addition.

## Why it is still here in an offline setup

TPM is used purely as a **loader**, not as a package manager. On startup it
reads the `@plugin` lines out of `~/.tmux.conf` and executes the matching
`*.tmux` file under `~/.tmux/plugins/<name>/`, redirecting all output to
`/dev/null` — which is what keeps the config silent. It raises no error for a
plugin directory that is absent.

The plugins themselves are already on disk, copied by
`dotfiles/install.d/10-tmux.sh`, so nothing is ever downloaded.

## Local note: the install/update keys are unbound

TPM's `prefix + I` / `prefix + U` / `prefix + M-u` bindings shell out to
`git clone` and `git pull`. On a host with no network — and against these
`.git`-stripped trees, which TPM cannot recognise as installed — they would fail
noisily. `.tmux.conf` therefore rebinds `I` to a one-line explanation and unbinds
the other two, immediately after running TPM and inside the same shell command so
the ordering is deterministic.

To refresh a plugin, re-vendor it here and re-run `dotfiles/install.sh`.
