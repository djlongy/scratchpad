# Design: VS Code Server for air-gapped networks

## Problem

"Set up VS Code Server for Remote Tunnels so a client can connect in an
air-gapped network" is, taken literally, close to a contradiction.
Microsoft's Remote Tunnels feature (`code tunnel`) is built to punch through
NAT using Microsoft's own hosted relay — it authenticates against
GitHub/Microsoft and keeps a persistent outbound connection to
`*.rel.tunnels.api.visualstudio.com`. There is no self-hosted relay mode and
no offline mode. An air-gapped host, by definition, cannot reach that relay.

## Decision

Split the problem into what tunnels actually are versus what the operator
actually needs (a browser-reachable VS Code Server on an isolated host):

1. **online** — when the target host DOES have egress (even through a
   proxy), support real Remote Tunnels via `code tunnel`, clearly labelled
   as requiring internet.
2. **bundle** — build a portable, checksum-pinned tarball on a host that has
   internet, containing the CLI, the server-web bits, and (optionally)
   extension `.vsix` files.
3. **offline** — install from that bundle on the air-gapped host with zero
   outbound calls, and run `code serve-web` (Microsoft's own local web-UI
   server, no relay involved) bound to a LAN or loopback address.

`serve-web` is the actual answer to "air-gapped VS Code Server a client can
connect to" — it is a normal HTTP(S) listener with no external dependency
once installed. Tunnels are supported for completeness and honesty (some
operators asking for "tunnels" really do have outbound internet and just
want the convenience), but the script refuses `--tunnel` in `--mode
offline` rather than silently degrading or lying about what it did.

## Alternatives considered

- **Remote-SSH classic (`server-linux-x64`, no `-web`)**: works air-gapped
  too, driven from the VS Code Desktop app on the client over SSH — no
  browser involved. Documented as a variant in the runbook, since some
  operators prefer the desktop app; the script's default install still
  fetches `server-linux-x64-web` because a browser client needs no local
  VS Code Desktop install at all, which is the lower-friction default for
  "a client can connect."
- **code-server (coder.com fork)**: a different project with its own
  release channel; explicitly out of scope — the ask was Microsoft's own
  VS Code Server / Remote Tunnels stack.
- **Building our own relay**: rejected. Reimplementing tunnel relay
  semantics is a large, security-sensitive undertaking for a homelab
  script; `serve-web` + SSH port-forward already solves the actual
  requirement (client reaches an isolated VS Code instance) without it.

## Chain of custody for integrity

Microsoft does not publish a checksum file for the commit-pinned CLI/
server-web tarballs (see `docs/reference/download-urls.md`). The bundle
step computes sha256 itself at download time and writes it into
`versions.json`; the offline install step verifies against that manifest
before extracting anything. This is a self-consistent bundle-to-install
chain, not third-party attestation, and the docs say so explicitly rather
than implying a Microsoft-issued signature was checked.

## Non-goals

- TLS termination for `serve-web` — the script binds plain HTTP by design
  and documents SSH port-forwarding (or a reverse proxy the operator
  already runs) as the way to get an encrypted hop; adding a TLS
  implementation to this script would duplicate infrastructure most
  networks already have and get the crypto wrong in a way that's hard to
  review from a single script.
- Multi-user auth beyond the connection token — `serve-web`'s token model
  is what upstream ships; this tool does not layer its own auth.
