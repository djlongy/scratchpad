# Offline Transfer Kit

Move a **curated** set of pip packages, RPM packages, Ansible Galaxy
collections, and container images from a networked machine to an
air-gapped one. One operator script, list-driven catalogs, hashed
bundles.

You do **not** need Pulp, Foreman, or a container registry to try the
fixture. Those are optional high-side publishers.

## Requirements (low side)

| Tool | Why |
|------|-----|
| **Python 3.12** | resolver, verify, indexes. Set `OTK_PYTHON` if `python3` is older. |
| **pip** | `python3 -m pip` (download + `--no-index` closure) |
| **Docker** | AlmaLinux 9 container runs `dnf download --resolve` + `createrepo_c` |
| **ansible-galaxy** | collection download |
| **skopeo** | container image → `oci-archive` |

Optional high side: a Pulp 3 server (`PULP_URL`) and/or Foreman/Katello
(`FOREMAN_URL`). If you set neither, ingest writes a **static HTTP tree**.

## Quick start (fixture smoke)

From this directory, on a machine that can reach public indexes and
registries:

```bash
# 1) Bundle — pull listed resources, resolve pip deps, hash, pack
./scripts/otk-airgap.sh --mode bundle \
  --catalog fixtures/airgap-dev/catalog \
  --require pypi,rpm,galaxy \
  --drop ./drop \
  --bundle ./outbox/airgap-dev.tar.gz

# 2) Ingest — verify the tarball, write a static serve tree (no Pulp)
unset PULP_URL FOREMAN_URL
./scripts/otk-airgap.sh --mode ingest \
  --bundle ./outbox/airgap-dev.tar.gz \
  --drop ./drop \
  --serve ./serve

# 3) Serve and install offline
python3 -m http.server 8080 --directory serve
# other terminal:
python3 -m venv /tmp/otk-venv
/tmp/otk-venv/bin/pip install --no-index --find-links serve/pypi/packages \
  -r serve/pypi/requirements.lock
```

A successful bundle prints `bundle ready sha256=…` and writes
`drop/pypi/requirements.lock` plus `drop/meta/provenance/pypi-tree.json`.
A successful static ingest prints `static serve ready:` and leaves
`serve/CLIENTS.txt`.

## What the fixture contains

`fixtures/airgap-dev/catalog/` is the only catalog shipped here:

- pip: `requests==2.32.3` and `packaging==24.2` (full transitive tree)
- RPM: `which` plus resolved Requires, built for linux/amd64
- Galaxy: `ansible.posix==1.6.2`
- optional OCI list: `docker.io/library/busybox` (needs skopeo)

Edit those lists; never hard-code package names in the scripts.

## Operator entrypoint

`./scripts/otk-airgap.sh` is the only command you need:

| Mode | Side | What it does |
|------|------|----------------|
| `pull` | networked | download + resolve + `MANIFEST.json` + `SHA256SUMS` |
| `bundle` | networked | `pull`, then tar.gz (or `.tar.zst`) |
| `ingest` | air-gap | verify hashes; publish |

**Ingest destinations**

- `PULP_URL` set → Pulp 3 (python / rpm / ansible plugins). Optional
  `FOREMAN_URL` also uploads RPMs and pip wheels, and Galaxy tarballs as
  a Katello **file** repository (collection upload is sync-only).
- `PULP_URL` unset → static tree under `--serve` / `OTK_SERVE`:
  - `pypi/simple/` — PEP 503
  - `rpm/<repo-id>/` — yum
  - `galaxy/collections/*.tar.gz`
  - `oci/*.tar` — load with skopeo

## Tests

```bash
python3 -m pytest tests/ -q
```

They drive the shipped helpers (pip closure, catalog parse, hash verify,
Katello file upload). A missing transitive pip dependency fails the
closure check.

## Layout

```text
scripts/otk-airgap.sh     # the entrypoint
scripts/lib/              # helpers
fixtures/airgap-dev/      # demo catalog
tests/                    # pytest, in this kit
FOLDER_CONTRACT.md        # drop/ on-disk contract
```
