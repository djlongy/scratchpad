#!/usr/bin/env python3
"""High side: merge a received (deduplicated) transfer into the persistent OCI store.

usage: oci-merge.py RECEIVED_DIR STORE_DIR

RECEIVED_DIR holds what the receive flow unpacked: oci/{index.json,oci-layout,blobs/...} plus
manifest.json. Only blobs that had not crossed before are present. STORE_DIR is a full OCI
image layout that accumulates every transfer, so every image's blobs are complete there and
skopeo can copy from it. Blobs are content-addressed: copy if absent, verify if present.
Manifests in the received index.json replace any store entry with the same ref name (a
re-tagged version) and are appended otherwise. Exits 1 if an image in manifest.json still
has a blob missing from the store after the merge (the transfer or an earlier one was lost).
"""
import hashlib
import json
import shutil
import sys
from pathlib import Path


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def main(received, store):
    received, store = Path(received), Path(store)
    (store / "blobs" / "sha256").mkdir(parents=True, exist_ok=True)
    if not (store / "oci-layout").exists():
        (store / "oci-layout").write_text('{"imageLayoutVersion": "1.0.0"}\n')
    index_path = store / "index.json"
    index = json.loads(index_path.read_text()) if index_path.exists() else {"schemaVersion": 2, "manifests": []}

    added = verified = 0
    for blob in sorted((received / "oci" / "blobs" / "sha256").glob("*")):
        if sha256(blob) != blob.name:
            raise SystemExit(f"corrupt blob {blob.name}")
        target = store / "blobs" / "sha256" / blob.name
        if target.exists():
            verified += 1
        else:
            shutil.copy2(blob, target)
            added += 1

    incoming = json.loads((received / "oci" / "index.json").read_text())["manifests"]
    ref = "org.opencontainers.image.ref.name"
    names = {m["annotations"][ref] for m in incoming}
    index["manifests"] = [m for m in index["manifests"] if m["annotations"].get(ref) not in names] + incoming
    index_path.write_text(json.dumps(index, indent=1) + "\n")

    manifest = json.loads((received / "manifest.json").read_text())
    missing = []
    for image in manifest["images"]:
        for digest in [image["digest"], image["config"], *image["layers"]]:
            if not (store / "blobs" / "sha256" / digest.split(":")[1]).exists():
                missing.append(f"{image['ref']} {digest}")
    print(f"transfer {manifest['transfer']}: {added} blob(s) added, {verified} already present, "
          f"{len(incoming)} manifest(s) indexed, store now {len(index['manifests'])} image(s)")
    if missing:
        print("missing blobs:\n  " + "\n  ".join(missing))
        return 1
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 3:
        raise SystemExit(__doc__)
    sys.exit(main(sys.argv[1], sys.argv[2]))
