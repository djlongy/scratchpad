# Vendored: tmux-plugins/tmux-sensible

| | |
|---|---|
| Upstream | <https://github.com/tmux-plugins/tmux-sensible> |
| Commit | `25cb91f42d020f675bb0a2ce3fbd3a5d96119efa` |
| Describes as | `v3.0.0-20-g25cb91f` |
| Commit date | 2022-08-14 |
| Vendored | 2026-08-17 |
| Licence | MIT — see `LICENSE.md`, kept unmodified |

Tree copied from upstream with `.git/` and `.github/` removed. Nothing else was
edited; this file is the only addition.

A set of defaults most people set anyway (faster escape time, larger history,
`focus-events`, sane `TERM` handling). It only ever calls `tmux set-option`, so
it has no runtime dependency and needs no network.

Options already set explicitly in `.tmux.conf` win: tmux-sensible checks whether
a value differs from the tmux default before overriding it.
