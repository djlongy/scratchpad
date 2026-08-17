# vscode-airgap-tunnels

Stage a **VS Code Remote-SSH** connection to an air-gapped Linux host —
pre-install the exact server commit at `~/.vscode-server` so the client
never tries to download anything, plus matching Linux/Windows client
installers so Help → About reports the same commit. Connects over plain
SSH port 22 with realm (GSSAPI/Kerberos) auth **and** OTP. `code
serve-web` and Microsoft's Remote Tunnels are supported as secondary,
opt-in paths — with an honest look at why tunnels genuinely can't be made
to work air-gapped.

```bash
# Online host: fetch latest stable, install Remote-SSH server + both
# client installers into ~/.vscode-server
./bin/vscode-airgap.sh --mode online

# Online host: pin a commit, bundle extensions from a file
./bin/vscode-airgap.sh --mode bundle --commit <40-char-commit> \
  --bundle-path ./vscode-bundle.tar.gz --extensions-file team-extensions.txt

# Air-gapped host, logged in as the SSH user: install, zero outbound calls
./bin/vscode-airgap.sh --mode offline --bundle-path ./vscode-bundle.tar.gz

# Print ssh_config / settings.json templates for realm + OTP, port 22 only
./bin/vscode-airgap.sh --emit-ssh-config

# Match an already-running remote instead of always grabbing latest:
./bin/vscode-airgap.sh --list-versions | head -20
./bin/vscode-airgap.sh --mode bundle --version 1.96.2 --bundle-path ./v1.96.2.tar.gz
```

## Why this exists

"Set up VS Code Server for Remote Tunnels on an air-gapped network" is a
contradiction if taken literally — tunnels require a persistent outbound
connection to Microsoft's relay service, which an air-gapped host by
definition can't reach. This tool is honest about that split, and (as of
2026-08-17) makes the actually-air-gap-friendly path the default:

- **Remote-SSH (default)** — the door that's already open (port 22),
  already authenticated (realm + OTP). This tool's job is limited to
  getting the exact matching server commit onto disk *before* the first
  connection, so Remote-SSH's own "is a valid server already here" check
  finds it and never tries to download one.
- **`--serve-web` (opt-in)** — Microsoft's own local web-UI server, no
  relay involved, works air-gapped too — kept as a second option for
  operators who specifically want browser access without installing a
  VS Code Desktop client.
- **`--tunnel` (opt-in, online mode only)** — real Remote Tunnels, for
  hosts that genuinely have internet. Refused outright in offline mode
  with an explanation rather than silently degraded.

Full reasoning: [`docs/designs/vscode-airgap-tunnels.md`](docs/designs/vscode-airgap-tunnels.md).

## Requirements

- `bash`, `curl` (online/bundle only), `tar`, `sha256sum`/`shasum`.
  `python3` is required on the online/bundle side when resolving
  extensions or a `--version` pin; `git` is additionally required for
  `--version`/`--list-versions` (semver→commit resolution via
  `microsoft/vscode`'s tags). `openssl` is used if present for the
  optional serve-web token, with a pure-shell fallback. The offline
  install path was tested with **none** of curl/python3/openssl/git
  present, under `docker run --network none`.
- Air-gapped target: Linux x86_64 (mandatory support) — arm64/armhf/Alpine
  also supported via `--arch`.
- Client installers bundled by default: Linux x64 (portable tarball) and
  Windows x64 (User Setup, per-user — no admin rights needed), both
  commit-matched to the staged server.

## Docs

- [`docs/runbooks/remote-ssh-realm-otp.md`](docs/runbooks/remote-ssh-realm-otp.md)
  — the primary path: `ssh_config` + VS Code `settings.json` for
  realm/GSSAPI + OTP auth through port 22 only, verified live against the
  Remote-SSH extension's own current settings (including one that
  sounds like "disable server download" but verified live it isn't).
- [`docs/runbooks/online.md`](docs/runbooks/online.md) /
  [`airgap.md`](docs/runbooks/airgap.md) — build-side and install-side
  walkthroughs.
- [`docs/reference/download-urls.md`](docs/reference/download-urls.md) —
  every Microsoft/Marketplace endpoint this uses, verified live, plus a
  field-name gotcha that silently produced a wrong filename until caught,
  the corrected record on checksum availability, and how `--version`
  resolves semver to a commit via `microsoft/vscode`'s own git tags
  (Microsoft's APIs don't expose that map — checked four candidates
  live, including one that hangs server-side, before finding the one
  that actually works).
- [`docs/designs/vscode-airgap-tunnels.md`](docs/designs/vscode-airgap-tunnels.md)
  — why Remote-SSH is primary (v2) and `serve-web` isn't (was primary in
  v1 — operator feedback corrected that), alternatives considered.
- [`contrib/`](contrib/) — the exact `ssh-config.example` and
  `settings.json.example` `--emit-ssh-config` writes, kept in the repo
  for browsing without running the script.

## Full option reference

```
./bin/vscode-airgap.sh --help
```

## Tested

Built and tested end-to-end in Linux containers (`docker run
--network none` for the offline path — genuinely no route to the
internet) on linux/arm64, since the homelab's designated throwaway VM
was down at test time. Live-verified: the classic Remote-SSH server
extracts to the exact directory layout Remote-SSH expects
(`~/.vscode-server/bin/<commit>/`, `product.json` commit matches,
`bin/code-server` executable); Microsoft-published sha256 checksums
resolve correctly for every artifact family; extension engine-matching
picks the newest compatible version and rejects garbage IDs loudly;
`--status`/`--emit-ssh-config` work standalone with zero network;
`--version` resolves a real historical semver (e.g. `1.96.2`) to its
commit and downloads it end to end, a 2-component version (`1.33`)
resolves to its newest matching patch tag then loudly fails the CDN
check rather than silently substituting latest (`1.33.x` is below the
current `1.34.0` floor), rejects an unknown version outright, and
correctly loses to `--commit` when both are set (including in the
output filename — see lessons below); `--list-versions` works
standalone (text/json, live or from a bundle) and fails fast with no
network rather than hanging.
Several real bugs were found and fixed by actually running it rather
than by lint — see `.agent/LESSONS_LEARNED.md` for the full list,
including a JSON field-name mixup, an architecture-mismatch bug that
only showed up when `--serve-web` tried to exec a binary built for the
wrong CPU, and a stale `--version` string leaking into a filename even
after `--commit` had already "won."
