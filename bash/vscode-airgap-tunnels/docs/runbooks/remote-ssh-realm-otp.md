# Runbook: Remote-SSH with realm auth + OTP, port 22 only

This is the primary connection path — Remote-SSH over the port that's
already open, not a new listener and not Microsoft's tunnel relay. It's
SSH client / VS Code Remote-SSH extension configuration, not a new
protocol; this script's job is limited to getting the right server
bits onto the remote host *before* the first connection so nothing tries
to download anything mid-session.

Everything below was verified live on 2026-08-17 against the Remote-SSH
extension's own current `package.json`
(`ms-vscode-remote.remote-ssh` v0.125.2026081318, fetched from the
Marketplace gallery API) — not assumed from memory, because these
settings genuinely do change between releases.

## 1. Pre-stage the server (this script's job)

```bash
./bin/vscode-airgap.sh --mode offline --bundle-path ./vscode-bundle.tar.gz
```

Run this **as the same user account** Remote-SSH will connect as, with
`--install-dir` left at its default (`~/.vscode-server`) — that is
deliberately the exact path Remote-SSH looks at on its own. It installs
the server at `~/.vscode-server/bin/<commit>/`, matching what a real
successful auto-install would produce: `node`, `bin/code-server`,
`out/server-main.js`, a `product.json` whose `commit` field matches
exactly. Remote-SSH's own "is a valid server already here" check finds
it and never attempts a download.

**There is no VS Code setting that means "never attempt a server
download, ever."** `remote.SSH.allowLocalServerDownload` sounds like it
but doesn't — see §4. Pre-staging the exact commit *is* the fail-closed
mechanism; it works because Remote-SSH checks for an existing valid
install before it ever considers downloading one.

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

Why this matters: Remote-SSH's client and server negotiate a commit. If
the client's own commit doesn't match what's staged on the remote, it
tries to reconcile the mismatch itself — normally by downloading a
server matching *its own* commit. On an air-gapped host, that download
just times out or fails. Matching commits on both ends is what makes the
whole pre-staging exercise actually pay off.

## 3. `~/.ssh/config` — realm + OTP, port 22 only

See [`contrib/ssh-config.example`](../../contrib/ssh-config.example) (also
written by `--emit-ssh-config`). Key points:

```
PreferredAuthentications gssapi-with-mic,keyboard-interactive,password
GSSAPIAuthentication yes
GSSAPIDelegateCredentials yes
PubkeyAuthentication no
ControlMaster auto
ControlPath ~/.ssh/sockets/%r@%h-%p
ControlPersist 600
```

- **Realm first, OTP as the interactive fallback.** `gssapi-with-mic`
  lets an existing Kerberos ticket (`kinit`) authenticate without a
  prompt at all; `keyboard-interactive` is what carries an OTP challenge
  when the realm alone isn't sufficient. Pubkey is explicitly **not**
  the only method and can be turned off entirely (`PubkeyAuthentication
  no`) if your policy forbids keys on this host — or left as a lower-
  priority convenience where it's allowed. Either way, the auth method
  selection lives entirely in `ssh_config`/`sshd_config` — Remote-SSH
  just shells out to (or reimplements, on Windows) a normal SSH client
  using whatever config you give it.
- **`ControlMaster auto`.** Remote-SSH opens more than one SSH channel.
  FreeIPA TOTP is anti-replay inside the current 30s window, so a second
  unauthenticated channel is denied. Multiplex after the first success.
  Windows OpenSSH does not implement ControlMaster — use
  `remote.SSH.useLocalServer: true` there. `mkdir -p ~/.ssh/sockets`.
- **No `ProxyJump`, no extra ports.** Only port 22 is open on the target
  — the example file says so explicitly and comments out where a
  `ProxyJump` would go, so it's obvious if one gets added by mistake
  later.

## 4. VS Code `settings.json`

See [`contrib/settings.json.example`](../../contrib/settings.json.example)
(also written by `--emit-ssh-config`). Setting-by-setting, with the exact
current descriptions pulled from the extension's own manifest:

| Setting | Value | Why |
|---|---|---|
| `remote.SSH.showLoginTerminal` | `true` | "Always reveal the SSH login terminal." Without this, the keyboard-interactive OTP prompt can run in a background process with no visible terminal to type into. |
| `remote.SSH.useLocalServer` | `true` | Reuses one SSH session for Remote-SSH's extra channels. Required for OTP: the second channel otherwise resubmits the same TOTP and is denied. This is the Windows-safe mux (Win32-OpenSSH has no ControlMaster). First connect still needs `showLoginTerminal`. |
| `remote.SSH.remotePlatform` | `{"<host>": "linux"}` | Skips a remote OS-detection round trip. Keep it set even with `useLocalServer` on. |
| `remote.SSH.lockfilesInTmp` | `true` | Keeps lockfiles in `/tmp` instead of inside the server's own folder — matters if home is NFS/distributed and has locking quirks. Harmless otherwise. |
| `remote.SSH.useExecServer` | `false` | **Found live, not assumed:** defaults to `true` in the current extension and switches connection bootstrapping to what the extension's own description calls only "a new bootstrapping mode... can be toggled off in the event of connection issues." Turned off here so the connection goes through the classic, well-documented bootstrap path this tool's pre-staged `~/.vscode-server/bin/<commit>/` layout is actually proven against, rather than a newer, less-documented alternative. |
| `remote.SSH.connectTimeout` | `30` | Default is 15s. A realm+OTP login (ticket check + waiting on a human to type a code) routinely takes longer than a bare key-based connect. |

**Deliberately NOT set:** `remote.SSH.allowLocalServerDownload`. This
sounds like "disable automatic download" but isn't — its real,
verified-live description is: *"If downloading the VS Code server fails
on the host, this allows the extension to fall back to downloading on
the client and transferring it to the host with scp."* It's a fallback
toggle for a **client-downloads-then-scps** path, not a switch for "never
attempt any download." Setting it to `false` would only remove that one
fallback — it would not stop a first-attempt remote download, and it
would not have been the right knob even if it did, since the actual
guarantee this tool provides comes from pre-staging (§1), not a setting.
Left at its default rather than implying a false sense of control by
flipping something that doesn't do what its name suggests.

## 5. Why port 22 is genuinely enough

Remote-SSH's server-side component listens on a **localhost** port or
Unix socket on the remote host — it is never exposed to the network
directly. Every byte between the VS Code client and that local listener
travels through the single SSH connection's own port forwarding, which
SSH sets up as part of the session. There's no secondary network-visible
listener to open a firewall hole for; `remote.SSH.remoteServerListenOnSocket`
(off by default) is purely a *local* multiplexing detail on the remote
side, not something that changes what has to be reachable from the
client.

## 6. Extensions

Bundled extensions land at `~/.vscode-server/extensions-to-install/` on
the remote (see `docs/reference/download-urls.md` for why they aren't
auto-installed). Once connected: Extensions view → **"Install from
VSIX..."** → pick each file in that directory.

## 7. Verifying the layout before a real connection

```bash
./bin/vscode-airgap.sh --status
```

Confirms `versions.json`'s commit matches a real directory at
`~/.vscode-server/bin/<commit>/` containing `product.json`,
`bin/code-server` (executable), and `node` (executable) — the concrete,
inspectable proxy for "Remote-SSH will find this and skip downloading,"
short of an actual live connection through a real realm+OTP-gated
sshd, which this repo's test harness deliberately doesn't require (see
`docs/designs/vscode-airgap-tunnels.md` and `.agent/PENDING.md`).
