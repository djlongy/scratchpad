# Vendored: omerxx/tmux-sessionx

| | |
|---|---|
| Upstream | <https://github.com/omerxx/tmux-sessionx> |
| Commit | `c9aaa1d309791871b5e8c1f9bfb91ecc5fa7da3a` |
| Describes as | `c9aaa1d` (no release tag at this commit) |
| Commit date | 2026-04-18 |
| Vendored | 2026-08-17 |
| Licence | GPL-3.0 — see `LICENSE`, kept unmodified |

Tree copied from upstream with `.git/` and `.github/` removed. Nothing else was
edited; this file is the only addition.

A fuzzy session switcher on `prefix + O`: jump between sessions, create one from
a directory, rename or kill without leaving the picker.

## This is the one plugin with a hard external dependency

The upstream README lists tpm, fzf, fzf-tmux, bat, and optionally zoxide. Reading
the code, only some of those actually bind:

| Dependency | Actually required? |
|---|---|
| `fzf` | **Yes** — `scripts/sessionx.sh` invokes it to draw the picker |
| `fzf-tmux` | Only when `@sessionx-fzf-builtin-tmux` is `off`; with it `on`, fzf's own `--tmux` is used instead |
| `bat` | No — the preview is `scripts/preview.sh`, which uses `tmux capture-pane` for sessions and `@sessionx-ls-command` (default `ls`) for directories |
| `zoxide` | No — only reached via `@sessionx-zoxide-mode 'on'` or the `ctrl-f` binding inside the picker |
| `git` | No — only under `@sessionx-git-branch 'on'` |

So the single thing that must be present is `fzf`, and `.tmux.conf` sets
`@sessionx-fzf-builtin-tmux 'on'` to keep it that way — one dependency instead
of two. fzf comes from the host, not from this repo.

That option needs fzf >= 0.53. On an older fzf, set
`@sessionx-fzf-builtin-tmux 'off'` and make sure the `fzf-tmux` script is on
PATH as well.

## How the guard works

Because fzf can be missing, this plugin is **not** declared with a `@plugin`
line. TPM finds plugins by grepping `~/.tmux.conf` for `@plugin`, so a declaration
there would load sessionx unconditionally and leave a `prefix + O` binding that
fails the moment it is pressed.

Instead `.tmux.conf` runs `sessionx.tmux` directly from a `run-shell` that first
checks `command -v fzf`. That test runs in the tmux server's own environment —
the same environment `scripts/sessionx.sh` will later inherit — so it answers the
question that actually matters: *will fzf resolve when the key is pressed?*

No fzf means no `prefix + O` binding, no error, and no message.
