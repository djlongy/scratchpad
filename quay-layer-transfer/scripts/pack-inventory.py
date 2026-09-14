#!/usr/bin/env python3
"""Build an offline transfer pack from the approved inventory.

This is what runs after a merge. The inventory in git IS the desired state, so
nothing here scans a registry to work out what to send - which also sidesteps the
fact that a pull-through cache cannot tell you what it holds.

Each line of the inventory is `registry/repo:tag@sha256:digest`. For each one this
pulls **through the matching proxy-cache organisation, by digest**, into a single
OCI layout, then writes the same three files the transfer expects:

    oci/            the layout, blobs shared between images
    manifest.json   what this pack carries
    state.json      the complete desired tag -> digest map

Pulling by digest matters twice over: it warms the cache with exactly the bytes
that were approved, and `--preserve-digests` makes skopeo fail loudly rather than
re-encode a manifest and silently change what crosses.

**Two digests, both real, and the difference matters.** The digest an update bot
pins is the one the registry returns for the tag, which for almost every modern
image is a manifest *index* over several platforms. An OCI layout cannot hold a
Docker-format manifest *list* at all: skopeo will only write it as an OCI index,
which changes its digest, and `--preserve-digests` correctly refuses. So the index
digest cannot cross.

What crosses instead is the single platform's child manifest, and `--preserve-digests`
carries it **byte-for-byte under its own genuine upstream digest** - verified: the
layout holds exactly the digest the upstream index advertises for linux/amd64, with
its Docker media type intact. An OCI layout can hold a Docker manifest; it just
cannot hold a Docker manifest list.

So the chain is:

    index digest    what git approved, and what gets pulled
    child digest    what crosses, what the far side serves, what to pin high

Both are true upstream digests. `state.json` records the second, read back from the
layout rather than assumed, so the far side is told what it will actually serve.
Pin the child digest in far-side deployments - the index digest is the approval
record and is not valid against the mirror.

`--all-platforms` keeps the index intact instead, and only works where the source
index is already OCI-format. On an amd64-only estate it is wasted bytes.

  usage: pack-inventory.py INVENTORY OUTDIR --registry HOST --creds USER:PASS
                           [--namespace mirror] [--cache-map docker.io=hubcache,...]
"""
import argparse
import json
import re
import subprocess
import sys
import tarfile
import tempfile
from datetime import datetime, timezone
from pathlib import Path

LINE = re.compile(r"^(?P<registry>[^/\s]+)/(?P<repo>[^\s:@]+):(?P<tag>[^\s@]+)@(?P<digest>sha256:[0-9a-f]{64})$")
DEFAULT_CACHES = "docker.io=hubcache,quay.io=quaycache,registry.k8s.io=k8scache,ghcr.io=ghcrcache"


def app_version(layout, ref):
    """The version the image reports about itself.

    Some publishers ship only a `latest` tag - Bitnami's community images on
    Docker Hub are the example, where everything else in the tag list is a Cosign
    artifact rather than the application. A tag of `latest` cannot tell you whether
    a transfer moved you forward, so read the version out of the image and make it
    a real tag. Order of preference matches the convention those images follow.
    """
    out = subprocess.run(["skopeo", "inspect", f"oci:{layout}:{ref}"],
                         capture_output=True, text=True)
    if out.returncode:
        return ""
    d = json.loads(out.stdout)
    labels = d.get("Labels") or {}
    for key in ("org.opencontainers.image.version", "app.kubernetes.io/version"):
        if labels.get(key):
            return labels[key]
    for env in d.get("Env") or []:
        if env.startswith("APP_VERSION="):
            return env.split("=", 1)[1]
    return ""


def parse_inventory(path):
    out = []
    for n, raw in enumerate(Path(path).read_text().splitlines(), 1):
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        m = LINE.match(line)
        if not m:
            sys.exit(f"{path}:{n}: not 'registry/repo:tag@sha256:digest' -> {line}")
        out.append(m.groupdict())
    if not out:
        sys.exit(f"{path}: no images. Refusing to build an empty pack, which the far "
                 f"side would read as 'delete everything'.")
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("inventory")
    ap.add_argument("outdir")
    ap.add_argument("--registry", required=True, help="the low-side registry host:port")
    ap.add_argument("--creds", required=True)
    ap.add_argument("--namespace", default="mirror", help="repo prefix the far side will hold")
    ap.add_argument("--cache-map", default=DEFAULT_CACHES)
    ap.add_argument("--all-platforms", action="store_true",
                    help="keep the whole index; fails unless the source index is OCI-format")
    args = ap.parse_args()

    # An unset CI variable is an empty string, not an error, and skopeo would happily
    # be handed "docker:///repo@digest". Refuse rather than build a nonsense reference.
    if not args.registry.strip():
        sys.exit("--registry is empty (an unset CI variable?). Refusing.")
    if ":" not in args.creds or not args.creds.split(":", 1)[0]:
        sys.exit("--creds is empty or not user:password (an unset CI variable?). Refusing.")

    caches = dict(p.split("=", 1) for p in args.cache_map.split(",") if p)
    images = parse_inventory(args.inventory)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")

    work = Path(tempfile.mkdtemp())
    layout = work / "oci"
    layout.mkdir()
    manifest, state = [], {}

    for img in images:
        cache = caches.get(img["registry"])
        if not cache:
            sys.exit(f"no proxy-cache organisation mapped for {img['registry']}. "
                     f"Add it to --cache-map; pulling direct would defeat the cache.")
        ref = f"{args.namespace}/{img['repo']}:{img['tag']}"
        src = f"docker://{args.registry}/{cache}/{img['repo']}@{img['digest']}"
        print(f"  {img['registry']}/{img['repo']}:{img['tag']} via {cache}")
        cmd = ["skopeo", "copy", "-q", "--preserve-digests", "--src-tls-verify=false",
               "--src-creds", args.creds]
        if args.all_platforms:
            cmd.append("--all")
        subprocess.run(cmd + [src, f"oci:{layout}:{ref}"], check=True)

        ver = app_version(layout, ref)
        if ver and ver != img["tag"]:
            alias = f"{args.namespace}/{img['repo']}:{ver}"
            subprocess.run(["skopeo", "copy", "-q", "--preserve-digests",
                            f"oci:{layout}:{ref}", f"oci:{layout}:{alias}"], check=True)
            print(f"      + version alias {ver} (the image reports it; the tag did not)")

    # describe what crossed, from the layout rather than from what we believe
    index = json.loads((layout / "index.json").read_text())
    by_ref = {m["annotations"]["org.opencontainers.image.ref.name"]: m["digest"]
              for m in index["manifests"]}
    # The desired state is read back from the layout, never assumed from the inventory:
    # what the far side will serve is the child-manifest digest, not the index digest.
    for ref, digest in by_ref.items():
        repo, tag = ref.rsplit(":", 1)
        state.setdefault(repo, {})[tag] = digest
        man = json.loads((layout / "blobs" / "sha256" / digest.split(":")[1]).read_text())
        if "manifests" in man:          # an index: describe each child it points at
            children = [c["digest"] for c in man["manifests"]]
            entry = {"ref": ref, "digest": digest, "config": digest, "layers": children}
            for child in children:
                cm = json.loads((layout / "blobs" / "sha256" / child.split(":")[1]).read_text())
                entry["layers"] += [cm["config"]["digest"]] + [l["digest"] for l in cm["layers"]]
            manifest.append(entry)
        else:
            manifest.append({"ref": ref, "digest": digest,
                             "config": man["config"]["digest"],
                             "layers": [l["digest"] for l in man["layers"]]})

    (work / "manifest.json").write_text(json.dumps({"transfer": stamp, "images": manifest}, indent=1))
    (work / "state.json").write_text(json.dumps({"transfer": stamp, "repos": state, "prune": []}, indent=1))

    outdir = Path(args.outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    tar_path = outdir / f"transfer-{stamp}.tar"
    with tarfile.open(tar_path, "w") as tf:
        for item in ("oci", "manifest.json", "state.json"):
            tf.add(work / item, arcname=item)

    blobs = len(list((layout / "blobs" / "sha256").iterdir()))
    print(f"wrote {tar_path}: {len(manifest)} image(s), {blobs} blob(s), "
          f"{tar_path.stat().st_size // 1024} KiB")
    if not args.all_platforms:
        print("  note: single-platform pack. The far side serves each image's child-manifest "
              "digest (a real upstream digest, recorded in state.json). Pin THAT high side; "
              "the index digest in the inventory is the approval record, not a mirror address.")


if __name__ == "__main__":
    main()
