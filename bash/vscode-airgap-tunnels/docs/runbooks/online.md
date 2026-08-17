# Runbook: online mode (internet-connected host)

Use this when the target host has outbound HTTPS, directly or via a proxy.

## 1. Straight install + start (latest stable, localhost only)

```bash
./bin/vscode-airgap.sh --mode online
```

- Resolves the latest `stable` commit from `update.code.visualstudio.com`.
- Downloads the CLI + `server-linux-*-web` tarball for the detected arch.
- Installs into `~/.vscode-server` (override with `--install-dir`).
- Generates a random connection token at
  `~/.vscode-server/serve-web.token` (chmod 600, never printed).
- Starts `code serve-web --host 127.0.0.1 --port 8000`.

Connect from the same host: `http://127.0.0.1:8000`, paste the token from
the token file.

## 2. Behind an egress proxy

The script does nothing special for proxies — curl reads the standard
environment variables natively:

```bash
export HTTPS_PROXY=http://proxy.example:3128
export NO_PROXY=localhost,127.0.0.1,.internal.example
./bin/vscode-airgap.sh --mode online
```

## 3. Pin an exact commit and expose on the LAN

```bash
./bin/vscode-airgap.sh --mode online \
  --commit a5b500951314efd502d07465bd138dfbd714a960 \
  --bind 0.0.0.0 --port 8000
```

`--bind 0.0.0.0` exposes serve-web to the whole LAN with only the
connection-token as auth and no TLS. Prefer the SSH-port-forward pattern in
`docs/runbooks/airgap.md` unless the LAN is already trusted.

## 4. Real Remote Tunnels (needs internet + a Microsoft/GitHub account)

```bash
./bin/vscode-airgap.sh --mode online --tunnel
```

This runs `code tunnel` after install instead of `serve-web`. It will print
a device-code URL/login prompt on first run (interactive — not suitable for
unattended automation) and then keep an outbound connection open to
Microsoft's tunnel relay for as long as it runs. This is the feature most
people mean by "VS Code Remote Tunnels"; it is **not** what makes this tool
useful for air-gapped hosts — see `docs/runbooks/airgap.md` for that case.

## 5. Download only, install later

```bash
./bin/vscode-airgap.sh --mode online --download-only
```

Fetches and verifies (self-computed sha256) into
`INSTALL_DIR/.download-cache` without starting anything.

## 6. Check what's installed

```bash
./bin/vscode-airgap.sh --status --install-dir ~/.vscode-server
```
