# Vendored: ble.sh (Bash Line Editor)

Third-party code, checked in verbatim so the optional autosuggestion/line-editor layer
installs on a host with no internet, no package manager, no `git` and no `make`.

| | |
|---|---|
| Upstream | https://github.com/akinomyoga/ble.sh |
| Release | `v0.4.0-devel3` (latest non-prerelease at time of vendoring) |
| Artifact | `ble-0.4.0-devel3-2.tar.xz` |
| Artifact URL | https://github.com/akinomyoga/ble.sh/releases/download/v0.4.0-devel3/ble-0.4.0-devel3-2.tar.xz |
| Artifact sha256 | `bdcdcfff216495403adf82a701fe41675f64b64644dc77caabd3e015871ebd61` |
| Vendored on | 2026-08-17 |
| Licence | BSD-3-Clause — see `doc/LICENSE.md` (kept as upstream ships it) |

This is the **prebuilt** release tarball, not a source checkout: `ble.sh` in this
directory is the generated, ready-to-source artifact. There is nothing to compile, so
the installer never needs `make`.

The tarball extracts to a single top-level `ble-0.4.0-devel3/` directory; its *contents*
are what live here, so `ble.sh` sits at the root of this directory.

## Verifying the vendored tree

```bash
bash -c 'source ./ble.sh --attach=none && echo "$BLE_VERSION"'
```

## Refreshing this vendor tree

Run on a networked machine, from a temporary directory outside this repository:

```bash
curl -sSLO https://github.com/akinomyoga/ble.sh/releases/download/<tag>/<artifact>.tar.xz
sha256sum <artifact>.tar.xz          # record it in the table above
tar -xJf <artifact>.tar.xz
# replace dotfiles/vendor/blesh with the extracted directory's contents
```

Do not vendor the `nightly` tag — it is a prerelease and moves under the same name, so
the recorded checksum stops meaning anything.
