#!/usr/bin/env python3
"""Preserve a pinned Hugging Face snapshot and independently verify every file."""
import argparse
import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path


def safe_path(root, name):
    path = root / name
    if not name or ".." in Path(name).parts or Path(name).is_absolute():
        raise ValueError(f"Invalid repository path: {name}")
    if path.is_symlink() or not path.resolve().is_relative_to(root.resolve()):
        raise ValueError(f"Repository path escapes snapshot: {name}")
    return path


def verify_file(root, item):
    path = safe_path(root, item["path"])
    if not path.is_file() or path.stat().st_size != item["size"]:
        raise ValueError(f"Missing or wrong-size file: {item['path']}")
    sha256 = hashlib.sha256()
    git_sha = hashlib.sha1(f"blob {item['size']}\0".encode(), usedforsecurity=False)
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(4 * 1024 * 1024), b""):
            sha256.update(chunk)
            git_sha.update(chunk)
    digest = sha256.hexdigest()
    expected = item.get("lfs_sha256")
    actual = digest if expected else git_sha.hexdigest()
    if actual != (expected or item["git_blob_id"]):
        raise ValueError(f"Checksum mismatch: {item['path']}")
    return digest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--repo", required=True)
    parser.add_argument("--revision", required=True)
    parser.add_argument("--note", default="")
    args = parser.parse_args()
    if not re.fullmatch(r"[\w.-]+/[\w.-]+", args.repo) or ".." in args.repo.split("/"):
        parser.error("repo must be owner/name")
    if not re.fullmatch(r"[0-9a-f]{40}", args.revision):
        parser.error("revision must be a full immutable commit")

    from huggingface_hub import HfApi, snapshot_download

    target = args.root / "huggingface" / args.repo / args.revision
    snapshot = target / "snapshot"
    metadata = target / "archive-metadata"
    metadata.mkdir(parents=True, exist_ok=True)
    complete = metadata / "VERIFIED.json"
    if complete.exists():
        print(json.dumps({"changed": False, "archive": str(target)}))
        return
    info = HfApi().model_info(args.repo, revision=args.revision, files_metadata=True)
    if info.sha != args.revision:
        raise ValueError("Upstream did not resolve to the requested commit")
    files = [
        {"path": f.rfilename, "size": f.size, "git_blob_id": f.blob_id,
         "lfs_sha256": f.lfs.sha256 if f.lfs else None}
        for f in info.siblings
    ]
    for item in files:
        safe_path(snapshot, item["path"])
    has_readme = any(f["path"].lower().startswith("readme") for f in files)
    manifest = {"repo": args.repo, "revision": args.revision,
                "source": f"https://huggingface.co/{args.repo}/tree/{args.revision}",
                "upstream_readme_available": has_readme,
                "compatibility_note": args.note, "files": files}
    (metadata / "upstream-manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    (metadata / "ABOUT.txt").write_text(
        f"Local archive metadata, not an upstream model card.\nRepository: {args.repo}\n"
        f"Revision: {args.revision}\n{args.note}\n"
        + ("Original README preserved in snapshot/.\n" if has_readme else
           "Upstream provides no README/model card at this revision. Provenance and capability claims are unverified.\n")
    )
    snapshot_download(args.repo, revision=args.revision, local_dir=snapshot, max_workers=2)
    checksums = [f"{verify_file(snapshot, item)}  {item['path']}\n" for item in files]
    (metadata / "SHA256SUMS").write_text("".join(checksums))
    result = {"repo": args.repo, "revision": args.revision, "files": len(files),
              "bytes": sum(f["size"] for f in files),
              "verified_at": datetime.now(timezone.utc).isoformat()}
    staged = metadata / "VERIFIED.json.partial"
    staged.write_text(json.dumps(result, indent=2) + "\n")
    staged.replace(complete)
    print(json.dumps({"changed": True, "archive": str(target), **result}))


if __name__ == "__main__":
    main()
