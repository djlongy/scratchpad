# Drop folder contract (low → high)

**Assumption:** transport is seamless. Whatever lands under the low-side
`drop/` tree appears byte-identical under the high-side `drop/` tree.
No diode logic lives here — only **build** (low) and **import** (high).

## Layout

```text
drop/
  MANIFEST.json              # required — release id + component map
  SHA256SUMS                 # required — integrity
  pypi/
    packages/*.whl|*.tar.gz  # wheels/sdists (full transitive tree)
    requirements.lock        # flattened name==version from the resolver
  rpm/
    <repo-id>/*.rpm
    <repo-id>/repodata/      # optional on drop; high may rebuild
  galaxy/
    collections/*.tar.gz
  oci/
    images.json              # {schema_version, images:[{ref, archive, digest}]}
    *.tar                    # skopeo oci-archive
  meta/
    sbom/  reports/  provenance/   # pypi-tree.json = per-target resolved graph
```

## Operator flow

| Side | Command | What it does |
|------|---------|--------------|
| Low | `./scripts/otk-airgap.sh --mode pull\|bundle` | Read catalog lists → hashed drop/bundle |
| — | *(delivery)* | `drop/` on low ≡ `drop/` on high |
| High | `./scripts/otk-airgap.sh --mode ingest` | Verify SHA-256; Pulp if `PULP_URL` is set, else static serve |

## High-side serve targets

| Content | Destination | Client |
|---------|-------------|--------|
| PyPI | Pulp python distribution, or static `pypi/simple/` | `pip install -i …/simple/` |
| RPM | Pulp rpm distribution, or static `rpm/<id>/` | `dnf` baseurl |
| Galaxy | Pulp ansible distribution, or static collection tarballs | `ansible-galaxy collection install ./foo.tar.gz` |
| OCI | Optional container registry, or oci-archive files | `skopeo copy oci-archive:…` |
