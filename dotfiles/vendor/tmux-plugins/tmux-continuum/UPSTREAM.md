# Vendored: tmux-plugins/tmux-continuum

| | |
|---|---|
| Upstream | <https://github.com/tmux-plugins/tmux-continuum> |
| Commit | `0698e8f4b17d6454c71bf5212895ec055c578da0` |
| Describes as | `v3.1.0-82-g0698e8f` |
| Commit date | 2024-01-20 |
| Vendored | 2026-08-17 |
| Licence | MIT — see `LICENSE.md`, kept unmodified |

Tree copied from upstream with `.git/` and `.github/` removed. Nothing else was
edited; this file is the only addition.

Drives tmux-resurrect on a timer: continuous saves every 15 minutes by default,
and optional restore when the server starts. It depends on tmux-resurrect being
loaded and on nothing else — no network, no compiled dependency.

`.tmux.conf` sets `@continuum-restore 'off'`, so a new tmux server starts empty
rather than resurrecting the previous layout. Automatic saving stays on, so the
state is there when you ask for it with `prefix + Ctrl-r`.
