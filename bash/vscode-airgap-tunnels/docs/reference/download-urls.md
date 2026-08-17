# Reference: VS Code download endpoints

Everything here was verified live against `update.code.visualstudio.com` on
2026-08-17. These are Microsoft's actual CDN redirect endpoints, not
third-party mirrors — treat this page as the ground truth for what the
script does, and re-verify if Microsoft ever changes the scheme.

## Commit resolution

```
GET https://update.code.visualstudio.com/api/commits/<channel>/server-linux-x64
```

`<channel>` is `stable` or `insider`. Returns a JSON array of commit hashes,
**newest first**. The first element is "latest" for that channel. This
commit hash is shared across every platform/arch artifact within the same
channel+release — you resolve it once against `server-linux-x64` and reuse
it for the CLI, server-web, and any other arch.

Confirmed live: the first entry from this endpoint matched the `version`
Microsoft's desktop `/api/update/linux-x64/stable/latest` endpoint reported
as current, on the same day.

## Artifact download (commit-pinned)

```
GET https://update.code.visualstudio.com/commit:<COMMIT>/<platform-segment>/<channel>
```

Issues a `302` redirect to the real CDN blob
(`vscode.download.prss.microsoft.com/dbazure/download/...`). `curl -fL`
follows it transparently.

### Platform segments this tool uses

| Segment | Arch | Contains |
|---|---|---|
| `cli-linux-x64` | linux-x64 | `code` CLI binary (`vscode_cli_linux_x64_cli.tar.gz`) |
| `cli-linux-arm64` | linux-arm64 | `code` CLI binary |
| `cli-linux-armhf` | linux-armhf | `code` CLI binary |
| `cli-alpine-x64` | alpine-x64 (musl) | `code` CLI binary |
| `cli-alpine-arm64` | alpine-arm64 (musl) | `code` CLI binary |
| `cli-darwin-x64` | darwin-x64 | `code` CLI binary (client-only, no server) |
| `cli-darwin-arm64` | darwin-arm64 | `code` CLI binary (client-only, no server) |
| `server-linux-x64-web` | linux-x64 | VS Code Server + built-in web UI (what `code serve-web` runs) |
| `server-linux-arm64-web` | linux-arm64 | same, arm64 |
| `server-linux-armhf-web` | linux-armhf | same, armhf |
| `server-linux-alpine-web` | alpine-x64 | same, musl |
| `server-linux-x64` (no `-web`) | linux-x64 | classic Remote-SSH server (no bundled web UI) |

There is no `server-*-web` artifact for darwin or armhf — those are
client-only architectures in Microsoft's build matrix, matching a Mac used
to run the `code` CLI to *connect out*, never to host `serve-web`/tunnels
itself. This script errors clearly if you ask for a server-web artifact on
one of those (see `server_web_platform_segment()` in
`bin/vscode-airgap.sh`).

### Example (verified live)

```
$ curl -sI -L https://update.code.visualstudio.com/commit:a5b500951314efd502d07465bd138dfbd714a960/server-linux-x64-web/stable
HTTP/2 302
location: https://vscode.download.prss.microsoft.com/dbazure/download/stable/a5b500951314efd502d07465bd138dfbd714a960/vscode-server-linux-x64-web.tar.gz
HTTP/1.1 200 OK
Content-Disposition: attachment; filename=vscode-server-linux-x64-web.tar.gz
```

## Checksums — the honest gap

Microsoft's desktop build endpoint,
`GET /api/update/<platform>/<channel>/latest`, DOES return a `sha256hash`
field — but that's for the desktop `code-stable-*` installer, a different
artifact family from `cli-*`/`server-*-web`, which this tool uses. As of
this writing there is **no published checksum file** for the CLI or
server-web tarballs at the commit-pinned URLs above.

This tool does not pretend otherwise. It computes its own sha256 of every
artifact **at download time** (on the online/bundle side) and pins that
value into the bundle's `versions.json`. `--mode offline` verifies every
extracted artifact against that manifest before touching `INSTALL_DIR`, and
refuses to install on a mismatch. That's a self-consistent chain of
custody (bundle-builder → bundle → installer), not independent
Microsoft-issued attestation — say so if anyone asks "did you verify the
checksum against Microsoft's".

## Semver → commit: also a gap

There is no public `update.code.visualstudio.com` endpoint that maps an
arbitrary historical semver (e.g. `1.96.2`) to its commit hash for the
CLI/server artifact family. The desktop `/api/update/*/latest` endpoint
gives you `version` + `commit` together, but only for whatever is
*currently* latest — not for picking an older release on demand.

Practical options, in order of reliability:
1. **Pin by commit** (`--commit <40-hex>`). This is what the script
   actually resolves to and downloads by; a version string alone is
   informational.
2. Look up the commit for a known historical version from VS Code's own
   release notes (`https://code.visualstudio.com/updates/vX_Y`) or the
   `microsoft/vscode` git tags, and pass it via `--commit`.
3. Use `--version latest` (the default) and let the script resolve the
   channel's current commit via the commits API above.

## Extension install gap (found in live testing, 2026-08-17)

`code --install-extension <path>.vsix` targets the CLI's own
version-managed installation (the one it creates for itself under
`~/.vscode/cli/servers/` when you run `code serve-web`/`code tunnel`
normally) — it does **not** reliably install into a separately-downloaded
`server-linux-*-web.tar.gz` extracted to an arbitrary directory the way
this script lays one out. Reproduced directly:

```
$ code --install-extension /path/to/ms-python.python.vsix
No installation of Visual Studio Code stable was found.
Install it from your system's package manager or https://code.visualstudio.com...
```

The suggested remedy in that error (`code version use stable --install-dir
...`) does not accept an externally-supplied server-web tree either — it
expects a layout the CLI itself created. Rather than paper over this with a
"best effort, ignore failures" install step, the script stages the
downloaded `.vsix` files at `INSTALL_DIR/extensions-to-install/` and
documents the one path that IS ordinary, supported VS Code behaviour:
opening the Extensions view in the connected Web UI and using
**"Install from VSIX..."** for each file. See
`.agent/LESSONS_LEARNED.md` for the full investigation trail.

## `serve-web`'s own update-check ping (found in live testing, 2026-08-17)

`code serve-web` makes exactly one outbound call on startup — a
best-effort version check against
`https://update.code.visualstudio.com/api/latest/server-linux-<arch>-web/stable`
— independent of anything this script does. Verified under `docker run
--network none` (genuinely no route to the internet, no DNS): the call
fails and is logged as a single `warn`, and the server **still starts and
serves** normally:

```
Web UI available at http://0.0.0.0:8125?tkn=...
[...] warn error getting latest version: Could not check for update: error
requesting https://update.code.visualstudio.com/api/latest/server-linux-arm64-web/stable
```

This is the one outbound *attempt* (not requirement) that exists anywhere
in the offline path — this script itself never calls `curl` or resolves
DNS in `--mode offline`. If your policy requires zero attempted egress
(not just zero successful egress), that single call comes from the
Microsoft-built `code` binary's own runtime, not from this script, and
isn't something this tool can suppress from the outside.

## Marketplace extension downloads (air-gap bundling)

```
GET https://marketplace.visualstudio.com/_apis/public/gallery/publishers/<publisher>/vsextensions/<name>/latest/vspackage
```

Returns the `.vsix` package directly (also a redirect to a CDN blob in
practice). No auth required for public extensions. The script downloads
these on the online/bundle side and installs them offline via
`code --install-extension <path>.vsix`, which works entirely from the
local file — no marketplace reachability needed at install time.
