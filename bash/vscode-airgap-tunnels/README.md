# vscode-airgap-tunnels

Download, bundle, and install **VS Code Server** on hosts with no internet
access — with an honest look at why Microsoft's Remote Tunnels (`code
tunnel`) can't actually do that, and what does instead (`code serve-web`).

```bash
# Online host: fetch latest stable, install, start on localhost:8000
./bin/vscode-airgap.sh --mode online

# Online host: build a portable, checksum-pinned bundle
./bin/vscode-airgap.sh --mode bundle --commit <40-char-commit> \
  --bundle-path ./vscode-bundle.tar.gz

# Air-gapped host: install from the bundle, zero outbound calls
./bin/vscode-airgap.sh --mode offline --bundle-path ./vscode-bundle.tar.gz \
  --bind 0.0.0.0 --port 8000
```

## Why this exists

"Set up VS Code Server for Remote Tunnels on an air-gapped network" is a
contradiction if taken literally — tunnels require a persistent outbound
connection to Microsoft's relay service, which an air-gapped host by
definition can't reach. This tool is honest about that split:

- **online** mode supports real `code tunnel` (needs internet + a
  Microsoft/GitHub login), for hosts that genuinely have egress.
- **bundle** + **offline** modes solve the actual air-gapped case: pack the
  CLI + server-web bits (+ optional extensions) on a connected machine,
  carry the tarball across the gap, install with zero network calls, and
  serve over plain HTTP via `code serve-web` — a client reaches it directly
  on the LAN or through an SSH port-forward.

Full reasoning: [`docs/designs/vscode-airgap-tunnels.md`](docs/designs/vscode-airgap-tunnels.md).

## Requirements

- `bash`, `curl` (online/bundle only), `tar`, `sha256sum`/`shasum`.
  `openssl` and `python3` are used if present (token generation, JSON
  parsing) with pure-shell fallbacks when they're not — the offline path
  was tested with neither installed.
- Target host: Linux x86_64/arm64/armhf, or Alpine (musl) x86_64/arm64.
  macOS (darwin-x64/arm64) is supported as a **client-only** arch (fetches
  the `code` CLI, no server-web artifact exists for it upstream).

## Docs

- [`docs/reference/download-urls.md`](docs/reference/download-urls.md) —
  the exact Microsoft CDN endpoints this uses, verified live, including two
  honest gaps (no published checksums for these artifacts; no semver→commit
  API) and two things found only by testing (the extension-install gap, the
  serve-web update-check ping).
- [`docs/runbooks/online.md`](docs/runbooks/online.md) — online-mode
  walkthroughs (proxy, pinned commit, LAN bind, real tunnels).
- [`docs/runbooks/airgap.md`](docs/runbooks/airgap.md) — bundle-then-install
  walkthrough, client connection patterns, what "offline" actually
  guarantees.
- [`docs/designs/vscode-airgap-tunnels.md`](docs/designs/vscode-airgap-tunnels.md)
  — the tunnels-vs-serve-web decision and alternatives considered.

## Full option reference

```
./bin/vscode-airgap.sh --help
```

## Tested

Built and tested end-to-end in Linux containers (`docker run
--network none` for the offline path — genuinely no route to the internet,
not just unplugged proxy vars) on linux/arm64. See
[`docs/reference/download-urls.md`](docs/reference/download-urls.md) for
two real bugs the testing caught and how they were fixed (a stale-trap
cleanup bug, and `--status` wrongly requiring `--mode`/`curl`).
