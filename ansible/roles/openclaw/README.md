# openclaw

openclaw bot local-admin account — a portable, distro-aware site role layered on
top of the generic [`baseline`](../baseline/) OS setup. Keeps `baseline` a pure,
portable OS baseline while the bot account lives here.

## Tags

| Tag | Runs |
|---|---|
| `openclaw` | openclaw bot local-admin account (user + SSH key + optional sudo) |

## openclaw bot account

Moved out of `baseline`. Portable — user, shell, public-key path, and sudo are
all variables:

```yaml
openclaw_enabled: true
openclaw_user: openclawbot
openclaw_pubkey_file: "~/.ssh/id_ed25519_openclawbot.pub"   # ENV-SPECIFIC
openclaw_sudo: true
```

Distro-aware: adds the bot to `wheel` (EL) or `sudo` (Debian/Ubuntu).

See `defaults/main.yml` for the full surface.
