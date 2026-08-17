# Vendored: tmux-plugins/tmux-resurrect

| | |
|---|---|
| Upstream | <https://github.com/tmux-plugins/tmux-resurrect> |
| Commit | `cff343cf9e81983d3da0c8562b01616f12e8d548` |
| Describes as | `v4.0.0-18-gcff343c` |
| Commit date | 2023-03-06 |
| Vendored | 2026-08-17 |
| Licence | MIT — see `LICENSE.md`, kept unmodified |

Tree copied from upstream with `.git/` and `.github/` removed. Nothing else was
edited; this file is the only addition.

Saves and restores sessions, windows, panes and layouts across a reboot:
`prefix + Ctrl-s` to save, `prefix + Ctrl-r` to restore. Pure shell plus tmux
commands — no network, no compiled dependency.

`.tmux.conf` sets `@resurrect-capture-pane-contents 'on'`.

## Saved state is not managed by the installer

Snapshots live in `~/.tmux/resurrect/`, which is deliberately outside the plugin
directory. `install.d/10-tmux.sh` refreshes `~/.tmux/plugins/tmux-resurrect/`
on every run and `uninstall.d/10-tmux.sh` removes it, but neither touches saved
sessions.
