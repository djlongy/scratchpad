# Runbook: air-gap mode (bundle on an online host, install offline)

## 1. On a host WITH internet: build the bundle

```bash
./bin/vscode-airgap.sh --mode bundle \
  --commit a5b500951314efd502d07465bd138dfbd714a960 \
  --bundle-path ./vscode-bundle-linux-x64.tar.gz \
  --extensions ms-python.python,ms-vscode.cpptools
```

Pinning `--commit` (rather than relying on "latest") makes the bundle
reproducible — rerunning this later with the same commit produces
byte-identical downloads and the same `versions.json` sha256 values.

Output: a single tarball containing `cli.tar.gz`, `server-web.tar.gz`,
`extensions/*.vsix`, and `versions.json` (the manifest the offline side
verifies against). Move it across the air gap by whatever sneakernet path
your policy allows (approved USB, one-way transfer station, etc — out of
scope for this script).

## 2. On the air-gapped host: install with network blocked

The script never calls `curl` in `--mode offline` — there's nothing to
disable. To *prove* that during testing, run it with network genuinely cut:

```bash
# Option A: no network namespace access at all (e.g. inside a container
# started with --network none)
# Option B: point every proxy var at a black hole so any accidental egress
# fails fast and loud instead of hanging
export HTTPS_PROXY=http://127.0.0.1:1 HTTP_PROXY=http://127.0.0.1:1

./bin/vscode-airgap.sh --mode offline \
  --bundle-path ./vscode-bundle-linux-x64.tar.gz \
  --bind 0.0.0.0 --port 8000
```

Expected: sha256 verification against `versions.json` for every artifact,
extraction, extension installs, then `serve-web` starts and listens. If the
bundle is corrupt or was tampered with in transit, the sha256 check fails
loudly and nothing is installed.

## 3. Client connects

**Preferred — SSH port-forward** (keeps serve-web itself bound to
`127.0.0.1` on the server, no LAN exposure at all):

```bash
# on the server: install with default --bind 127.0.0.1
./bin/vscode-airgap.sh --mode offline --bundle-path ./bundle.tar.gz

# on the client:
ssh -L 8000:localhost:8000 user@airgapped-host
# then browse http://localhost:8000, paste the token from
# ~/.vscode-server/serve-web.token on the server
```

**Direct LAN** (only if the LAN is already access-controlled):

```bash
./bin/vscode-airgap.sh --mode offline --bundle-path ./bundle.tar.gz \
  --bind 0.0.0.0 --port 8000
```

Client browses `http://<airgapped-host>:8000` directly.

## 4. Remote-SSH instead of a browser (optional variant)

If the operator prefers VS Code Desktop over a browser: the bundle's
`server-web.tar.gz` includes the web UI; classic Remote-SSH instead expects
`server-linux-x64` (no `-web`) under
`~/.vscode-server/bin/<commit>/`. This script does not build that variant
today — see `.agent/PENDING.md`. Workaround: on the online side, download
`server-linux-x64` manually with the same commit
(`docs/reference/download-urls.md`), extract it to
`~/.vscode-server/bin/<commit>/` on the target (the exact path VS Code
Desktop's Remote-SSH expects), and Desktop will detect and reuse it instead
of trying to download over SSH itself.

## 5. What "offline" actually guarantees

- No DNS lookups, no HTTP(S) calls, no marketplace calls during
  `--mode offline`.
- Every artifact is checksum-verified against the bundle's own manifest
  before extraction (see the honesty note in
  `docs/reference/download-urls.md` about what that checksum does and
  doesn't prove).
- `--tunnel` is hard-refused in offline mode with an explanation, not
  silently ignored.
