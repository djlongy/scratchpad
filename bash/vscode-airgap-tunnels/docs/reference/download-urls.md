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

| Segment | Fetched when | Contains |
|---|---|---|
| `server-linux-x64` | always (mandatory) | classic Remote-SSH server — no bundled web UI, this is what a real Remote-SSH auto-install produces. Also `server-linux-arm64`/`-armhf`/`server-linux-alpine` for other `--arch` values. |
| `linux-x64` | always (mandatory) | Linux x64 **desktop** tarball, portable/extract-and-run (`code-stable-x64-<ts>.tar.gz`) — the client installer, commit-matched to the staged server. |
| `win32-x64-user` | always (mandatory) | Windows x64 **User Setup** (`VSCodeUserSetup-x64-<ver>.exe`) — per-user install, no admin rights needed. |
| `cli-linux-<local-arch>` | `--serve-web` or `--tunnel` | `code` CLI binary. Uses the arch of the machine RUNNING this script (`LOCAL_ARCH`), not `--arch` (the remote server's arch) — see the arch-mismatch gotcha below. |
| `server-linux-<local-arch>-web` | `--serve-web` | VS Code Server + built-in web UI (what `code serve-web` runs). Same `LOCAL_ARCH` rule as the CLI. |

There is no `server-*-web` (or classic `server-linux-*`) artifact for
darwin or armhf — those are client-only architectures in Microsoft's
build matrix. `--arch` (the mandatory remote-server artifact) only
accepts `linux-x64`/`linux-arm64`/`linux-armhf`/`alpine-x64` for this
reason; darwin only ever appears as a `LOCAL_ARCH` value for the
optional CLI/serve-web fetch when this script itself runs on a Mac.

### The `ARCH` vs `LOCAL_ARCH` split (found in live testing, 2026-08-17)

v1 of this tool auto-detected one `ARCH` from `uname` and used it for
everything. That broke the moment the primary artifact set became fixed
(mandatory `linux-x64` server + client installers, regardless of what's
running the script) while `--serve-web`/`--tunnel` still need to fetch a
CLI/server-web that matches **this machine**, which is very often a
*different* architecture than the remote target (e.g. building a bundle
for an x86_64 remote on an Apple Silicon Mac, or in this case, an arm64
Colima container). Reproduced directly: with `ARCH` defaulted to
`linux-x64` and reused for `--serve-web` on an arm64 Colima host,
`code serve-web` exec'd an x86_64 binary and Rosetta failed it outright:

```
rosetta error: failed to open elf at /lib64/ld-linux-x86-64.so.2
```

Fixed by splitting the two concerns: `ARCH`/`--arch` now means *only*
"the remote Linux host's arch" (mandatory server artifact, default
`linux-x64`, never auto-detected from `uname` — pass `--arch auto` to
opt back into that for the secondary `--serve-web`-on-this-host case),
and a separate `LOCAL_ARCH` (always `uname`-detected, no override flag)
drives the CLI/server-web fetch for `--serve-web`/`--tunnel`.

### Example (verified live)

```
$ curl -sI -L https://update.code.visualstudio.com/commit:a5b500951314efd502d07465bd138dfbd714a960/server-linux-x64-web/stable
HTTP/2 302
location: https://vscode.download.prss.microsoft.com/dbazure/download/stable/a5b500951314efd502d07465bd138dfbd714a960/vscode-server-linux-x64-web.tar.gz
HTTP/1.1 200 OK
Content-Disposition: attachment; filename=vscode-server-linux-x64-web.tar.gz
```

## Checksums — corrected 2026-08-17 (was wrongly documented as a gap)

**This section previously claimed no checksum exists for the CLI/server
artifact family. That was wrong — verified live and corrected.**

```
GET https://update.code.visualstudio.com/api/update/<platform-segment>/<channel>/latest
```

Returns Microsoft-published metadata **for every platform segment this
tool uses**, not just the desktop build — confirmed live for
`server-linux-x64`, `server-linux-x64-web`, `cli-linux-x64`, `linux-x64`,
and `win32-x64-user` alike:

```json
{
  "url": "https://vscode.download.prss.microsoft.com/.../vscode-server-linux-x64.tar.gz",
  "name": "1.133.0",
  "version": "a5b500951314efd502d07465bd138dfbd714a960",
  "productVersion": "1.133.0",
  "sha256hash": "6aa31693bb05b8cb07c939f19112548fbef5752f905fb5834e26493b9619a430",
  ...
}
```

**Field-name gotcha, found the hard way:** in this JSON, `"version"` is
actually the **commit hash**, and the real semver lives under
`"productVersion"` (or `"name"`). A first implementation read `"version"`
expecting a semver and got a commit hash back — silently correct-looking
(both are strings) until it showed up as a filename:
`VSCodeUserSetup-x64-a5b500951314efd502d07465bd138dfbd714a960.exe`
instead of `VSCodeUserSetup-x64-1.133.0.exe`. Fixed by reading
`productVersion`; `resolve_artifact_meta()`'s own read of `"version"` as
the commit is correct on its own terms — the bug was specifically in the
separate semver-resolution helper, `resolve_version_for_commit()`.

This tool now uses this endpoint as the **primary** checksum source —
`server_sha256_source`/`client_linux_sha256_source`/etc in `versions.json`
record `"microsoft"` when it came from here. It only serves the
**current latest** build per platform (confirmed live: passing an older
commit where "latest" goes 204s — an update-check semantic, not a
historical lookup), so an explicit `--commit` pin falls back to
constructing the direct commit-pinned URL and self-computing sha256
instead, recorded as `"self"` in the same manifest fields. Either way,
`--mode offline` verifies every extracted artifact against
`versions.json` before touching `INSTALL_DIR` and refuses to install on a
mismatch — the manifest is honest about which kind of checksum backs
each artifact rather than presenting both as equivalent.

## Classic Remote-SSH server layout (verified live, 2026-08-17)

`server-linux-x64.tar.gz` extracts to a single top-level directory
(`vscode-server-linux-x64/`) containing:

```
vscode-server-linux-x64/
├── node                    # bundled Node.js runtime, executable
├── product.json            # {"commit": "...", "version": "1.133.0", ...}
├── package.json
├── bin/
│   ├── code-server          # executable — the actual server entrypoint
│   ├── remote-cli/code
│   └── helpers/
├── out/
│   ├── server-main.js
│   └── ...
├── extensions/              # ~38 built-in extensions, ships with the server
└── node_modules/
```

Remote-SSH expects this content **directly inside**
`~/.vscode-server/bin/<commit>/` (commit taken from `product.json`) — so
this tool extracts with `tar --strip-components=1` to drop the
`vscode-server-linux-x64/` wrapper directory, then asserts `product.json`
exists and `bin/code-server` is executable before claiming success. This
is the single most load-bearing detail in the whole tool: get this
directory wrong and Remote-SSH falls back to trying a download, which is
exactly the failure mode air-gapping is meant to prevent.

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

## Marketplace extension downloads: engine-matched, not a blind "latest"

```
POST https://marketplace.visualstudio.com/_apis/public/gallery/extensionquery
Content-Type: application/json
Accept: application/json;api-version=3.0-preview.1

{"filters":[{"criteria":[{"filterType":7,"value":"<publisher>.<name>"}]}],
 "flags":19}
```

`flags: 19` = `IncludeVersions(1) | IncludeFiles(2) |
IncludeVersionProperties(16)`. Returns every published version (newest
first per version), each with:
- `targetPlatform` — many extensions (e.g. `ms-python.python`) ship
  per-platform builds (`linux-x64`, `win32-x64`, `darwin-arm64`, `web`,
  ...). This tool prefers the `linux-x64` build when one exists (the
  remote is a Linux host), falling back to the platform-universal entry
  (no `targetPlatform` key) otherwise, and skips every other
  platform-specific variant.
- `properties` containing `Microsoft.VisualStudio.Code.Engine` — a
  semver range (almost always `^X.Y.Z` in practice) the extension
  declares compatibility with.
- `files` containing a `Microsoft.VisualStudio.Services.VSIXPackage`
  asset URL — a direct, version-specific `.vsix` download link.

The script walks versions newest-first and downloads the **first one
whose engine range accepts the bundled VS Code version** (the resolved
`productVersion` for the pinned/latest commit, e.g. `1.133.0`) — latest
*compatible*, not an old hardcoded pin, and not blindly "whatever is
newest regardless of engine". Recorded per-extension in `versions.json`:
`{"id","version","target_platform","engine","method":"engine-matched"}`.

**Fallback, and when it fires:** if the query itself fails (network
issue, extension not found, no version satisfies the engine range), the
script falls back to the simple:

```
GET https://marketplace.visualstudio.com/_apis/public/gallery/publishers/<publisher>/vsextensions/<name>/latest/vspackage
```

— genuinely "whatever is newest," no engine check — and records
`"method":"latest-fallback"` so the manifest is honest that
compatibility wasn't verified for that particular extension. A garbage
extension id (malformed, or one that doesn't exist on the Marketplace)
fails loudly through this same path rather than silently producing an
empty/invalid `.vsix` — verified live with both a malformed id
(`not-a-valid-id`, rejected before any network call) and a well-formed
but nonexistent one (`totally-bogus.does-not-exist-xyz-123`, 404s
through the fallback and the script exits non-zero).

Extension install at the remote is entirely offline — VSIX files are
staged at `INSTALL_DIR/extensions-to-install/` and installed via the
connected Web/Desktop client's own "Install from VSIX..." command (see
the install-gap note above); no marketplace reachability is needed on
the air-gapped side at any point.
