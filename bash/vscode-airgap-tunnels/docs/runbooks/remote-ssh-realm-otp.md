# Runbook: Remote-SSH with realm auth + OTP, port 22 only

This is the primary connection path — Remote-SSH over the port that's
already open, not a new listener and not Microsoft's tunnel relay. It's
SSH client / VS Code Remote-SSH extension configuration, not a new
protocol. This script's job is getting the right server bits onto the
remote host *before* the first connection so nothing tries to download
anything mid-session.

`--emit-ssh-config` writes three **templates** (also under `contrib/`).
They are not live until you copy them:

| Template | Who | Put it here |
|---|---|---|
| `ssh-config.example` | operator laptop | Unix/macOS: `~/.ssh/config` · Windows: `C:\Users\youruser\.ssh\config` |
| `settings.json.example` | operator laptop | Windows: `%APPDATA%\Code\User\settings.json` · macOS: `~/Library/Application Support/Code/User/settings.json` · Linux: `~/.config/Code/User/settings.json` |
| `remote-host.example` | air-gapped host (root) | sshd block → `/etc/ssh/sshd_config.d/50-remote-ssh-airgap.conf` (firewall/egress are notes, not a drop-in) |

`settings.json.example` is **JSONC**: comments are `//` lines. Do **not**
comment with fake keys such as `"// useLocalServer": "…"`. Those are
real JSON properties, not comments.

## 1. Pre-stage the server (this script's job)

```bash
./bin/vscode-airgap.sh --mode offline --bundle-path ./vscode-bundle.tar.gz
```

Run this **as the same user account** Remote-SSH will connect as, with
`--install-dir` left at its default (`~/.vscode-server`). The bootstrap
script the client runs is chosen by a staged rollout, not by extension
version, so the install puts **both** layouts on disk:

```
~/.vscode-server/bin/<commit>/                 # classic
~/.vscode-server/code-<commit>                 # exec-server CLI binary
~/.vscode-server/cli/servers/Stable-<commit>/server/
~/.vscode-server/vscode-cli-<commit>.tar.gz    # handshake archive
~/.vscode-server/vscode-cli-<commit>.tar.gz.done   # written LAST
```

Classic contents: `node`, `bin/code-server`,
`bin/helpers/check-requirements.sh` (executable), `out/server-main.js`,
a `product.json` whose `commit` field matches the client Help → About
commit exactly. Either presence test passing means the download branch
is never entered.

**There is no VS Code setting that means "never attempt a server
download, ever."** `remote.SSH.allowLocalServerDownload` sounds like it
but isn't — see §4. Pre-staging the exact commit *is* the fail-closed
mechanism. Still set `remote.SSH.localServerDownload` to `"off"` so a
staging miss fails fast instead of `wget -O` truncating the CLI tarball
to zero bytes and polling forever.

## 2. Match the client commit (this script's job, operator's install)

`--mode online`/`bundle` also fetch a Linux x64 desktop tarball and a
Windows x64 User Setup — both pinned to the exact same commit as the
staged server. Install **one of these** on the operator's laptop:

```bash
# Linux laptop
tar -xzf vscode-linux-x64.tar.gz
./VSCode-linux-x64/bin/code

# Windows laptop
VSCodeUserSetup-x64-<version>.exe   # per-user, no admin rights needed
```

Remote-SSH's client and server negotiate a commit. If the client's own
commit doesn't match what's staged on the remote, it tries to reconcile
by downloading a server matching *its own* commit. On an air-gapped
host that download fails. Matching commits on both ends is what makes
pre-staging pay off.

## 3. `~/.ssh/config` — realm + OTP, port 22 only

See [`contrib/ssh-config.example`](../../contrib/ssh-config.example).
Unix client (ControlMaster) and Windows client (no ControlMaster) are
separate `Host` blocks in that file.

Unix / macOS:

```
PreferredAuthentications gssapi-with-mic,keyboard-interactive,password
GSSAPIAuthentication yes
GSSAPIDelegateCredentials yes
PubkeyAuthentication no
ControlMaster auto
ControlPath ~/.ssh/sockets/%r@%h-%p
ControlPersist 600
```

Windows (native OpenSSH optional feature):

```
PreferredAuthentications keyboard-interactive
PubkeyAuthentication no
GSSAPIAuthentication no
NumberOfPasswordPrompts 3
```

- **Realm first, OTP as the interactive fallback.** `gssapi-with-mic`
  lets an existing Kerberos ticket (`kinit`) authenticate without a
  prompt; `keyboard-interactive` carries the OTP challenge when the
  realm alone isn't enough. Pubkey can be turned off (`PubkeyAuthentication
  no`) if policy forbids keys. Auth method selection lives in
  `ssh_config`/`sshd_config` — Remote-SSH just shells out to a normal
  SSH client.
- **Session reuse is required.** Remote-SSH opens more than one SSH
  channel. Realm TOTP is anti-replay inside the current 30s window, so
  a second unauthenticated channel is denied.
  - Unix / macOS: `ControlMaster auto` (`mkdir -p ~/.ssh/sockets`).
  - Windows: Win32-OpenSSH **ignores** ControlMaster. Reuse is
    `remote.SSH.useLocalServer: true` in `settings.json`, not this file.
- **No `ProxyJump`, no extra ports.** Only port 22 is open on the
  target. The example comments out where a `ProxyJump` would go.

An `ssh_config` file is optional if you only ever connect by
`user@host`. Use one when you need the Host alias, auth-method order,
or ControlMaster. If you set `remote.SSH.configFile`, that file must
contain the same Host alias as `remote.SSH.remotePlatform`.

## 4. VS Code `settings.json` (client laptop only)

See [`contrib/settings.json.example`](../../contrib/settings.json.example).
These keys do nothing on the remote host. After connect, the Remote-SSH
log must dump the same values.

The file is JSONC. Comments are `//` lines above each key.

| Setting | Value | Why |
|---|---|---|
| `remote.SSH.showLoginTerminal` | `true` | Reveals the SSH login terminal. Without this, the keyboard-interactive OTP prompt can run in a background process with no place to type. |
| `remote.SSH.useLocalServer` | `true` | One shared SSH connection for Remote-SSH's extra channels. Required for OTP. This is the Windows mux (Win32-OpenSSH has no ControlMaster). Pair with `remoteServerListenOnSocket: false` or it is silently undone. First connect still needs `showLoginTerminal`. |
| `remote.SSH.useExecServer` | `false` | Defaults to `true`. A staged rollout (not an extension version) can still pick the exec-server bootstrap. Confirm the connect log dumps `remote.SSH.useExecServer = false`. This tool stages both layouts so either script's presence test passes. |
| `remote.SSH.localServerDownload` | `"off"` | A staging miss otherwise `wget -O` truncates `vscode-cli-<commit>.tar.gz` to zero bytes and the install script polls forever. `off` fails fast. |
| `remote.SSH.remoteServerListenOnSocket` | `false` | `true` silently forces `useLocalServer` off. Put it in `settings.json` — Windows ignores the UI toggle. OTP mux depends on `useLocalServer` staying on. |
| `remote.SSH.lockfilesInTmp` | `true` | Lockfiles in `/tmp` (or `%TEMP%`) instead of the server install folder. Matters if home is NFS/distributed. Harmless otherwise. |
| `remote.SSH.connectTimeout` | `60` | Default is 15s. A realm+OTP login (ticket check + typing a code) routinely takes longer than a key-based connect. |
| `remote.SSH.path` | `C:\Windows\System32\OpenSSH\ssh.exe` | Windows only. Force the native OpenSSH optional feature, not a third-party `ssh.exe` on `PATH`. Omit this key on Unix. |
| `remote.SSH.configFile` | optional | If set, that file must contain the Host alias below. Commented out in the example. |
| `remote.SSH.remotePlatform` | `{"<host>": "linux"}` | Skips a remote OS-detection round trip. The key must match the Host alias. |

**Do not set `remote.SSH.allowLocalServerDownload` expecting "never
download."** Its real meaning is: if the *remote* download fails, fall
back to downloading on the *client* and `scp` the bits over. It is not
a switch for "never attempt any download." The guarantee this tool
provides is pre-staging (§1) plus `localServerDownload: "off"` so a
miss fails fast.

Write the file as UTF-8 **without a BOM**. A UTF-8 BOM makes VS Code
reject the JSON.

## 5. Remote host (not VS Code settings)

See [`contrib/remote-host.example`](../../contrib/remote-host.example).
None of the `remote.SSH.*` keys go on the Linux host.

**sshd drop-in** (e.g. `/etc/ssh/sshd_config.d/50-remote-ssh-airgap.conf`):

```
PasswordAuthentication yes
KbdInteractiveAuthentication yes
UsePAM yes
AllowTcpForwarding yes
AllowStreamLocalForwarding yes
GatewayPorts no
PerSourcePenaltyExemptList 192.0.2.0/24,198.51.100.0/24
```

Then `sshd -t && systemctl reload sshd`. OpenSSH 9.9+ otherwise bans
the client IP after a hung OTP prompt. Replace the TEST-NET CIDRs with
the operator workstation ranges.

**Inbound firewall:** default zone drop. One management zone: service
`ssh` (and icmp if you want ping), sourced only from operator CIDRs.
Non-loopback listen is TCP/22 only. The VS Code server stays on
`127.0.0.1` and is forwarded through that SSH session. Do not open an
extra "VS Code" TCP port.

**Egress lock (IPv4 and IPv6):** allow loopback + RFC1918 (and
unique-local/link-local on v6). Reject everything else, especially
80/443 to the public Internet. An ESTABLISHED-only lock is not enough:
an in-flight download keeps going. Prove with
`curl https://update.code.visualstudio.com` — expect no route /
administratively prohibited.

**Identity:** realm user, OTP auth type, HBAC for this host + `sshd`.
First Factor = password. Second Factor = TOTP (not the password again).

**Do not set on the remote:** any `remote.SSH.*` key, ControlMaster, or
a vscode listener on `0.0.0.0`.

## 6. Why port 22 is enough

Remote-SSH's server-side component listens on a **localhost** port on
the remote host — it is never exposed to the network. Every byte
between the VS Code client and that local listener travels through the
single SSH connection's own port forwarding. There is no secondary
network-visible listener to punch a hole for.
`remote.SSH.remoteServerListenOnSocket` must stay `false` in
`settings.json`: when `true` it silently turns `useLocalServer` off,
which on Windows (no ControlMaster) means a second OTP and a denied
session. It is a local multiplexing detail, not a firewall change.

## 7. Extensions

Bundled extensions land at `~/.vscode-server/extensions-to-install/` on
the remote (see `docs/reference/download-urls.md` for why they aren't
auto-installed). Once connected: Extensions view → **"Install from
VSIX..."** → pick each file in that directory.

## 8. Verifying the layout before a real connection

```bash
./bin/vscode-airgap.sh --status
```

Confirms `versions.json`'s commit matches both layouts: classic
`bin/<commit>/` (`product.json`, `bin/code-server`, `node`) and
exec-server `code-<commit>` plus `cli/servers/Stable-<commit>/server/`,
plus a non-empty handshake tarball. That is the inspectable proxy for
"Remote-SSH will find this and skip downloading," short of an actual
live connection through a real realm+OTP-gated sshd, which this repo's
test harness deliberately doesn't require (see
`docs/designs/vscode-airgap-tunnels.md`).
