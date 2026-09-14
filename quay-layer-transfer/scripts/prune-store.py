#!/usr/bin/env python3
"""High side: drop what the desired state no longer wants.

`state.json` carries a `prune` list: blob digests the low side has already removed
from its dedupe ledger, and will therefore send again if they are ever needed
(see prune-plan.py). Only once that has happened is deleting them here safe.

Two stages, in this order, because the order is the safety property:

1. **Drop unwanted manifests from index.json.** A tag deleted from the registry
   does not remove the entry the OCI store keeps for it, so without this the
   store can still reach every blob it ever received and stage 2 would correctly
   conclude that nothing may be deleted. Only refs belonging to repositories the
   state document actually names are touched; a repository it does not mention is
   left completely alone.
2. **Delete the listed blobs that nothing can reach any more.** Every candidate is
   re-checked against what the freshly pruned index can reach - index entries,
   their manifests, and each manifest's config and layers. A digest still
   reachable is kept and reported however emphatically the low side asked.

Doing it this way round means a half-finished prune leaves a store that is merely
smaller, never one whose index points at blobs that are gone.

  usage: prune-store.py STATE_JSON OCI_STORE
"""
import json
import sys
from pathlib import Path

REF = "org.opencontainers.image.ref.name"


def wanted(state):
    """Every ref the desired state names, as 'repo:tag'."""
    return {f"{repo}:{tag}" for repo, tags in state["repos"].items() for tag in tags}


def prune_index(store, state):
    """Remove index entries for tags their own repository no longer wants."""
    path = store / "index.json"
    index = json.loads(path.read_text())
    keep, dropped = [], []
    named_repos = set(state["repos"])
    for m in index.get("manifests", []):
        ref = m.get("annotations", {}).get(REF, "")
        repo = ref.rsplit(":", 1)[0] if ":" in ref else ref
        if repo in named_repos and ref not in wanted(state):
            dropped.append(ref)
        else:
            keep.append(m)
    if dropped:
        index["manifests"] = keep
        path.write_text(json.dumps(index, indent=2))
    return dropped


def reachable(store):
    """Every blob still reachable from the store's own index."""
    blobs = store / "blobs" / "sha256"
    index = json.loads((store / "index.json").read_text())
    seen = set()
    for m in index.get("manifests", []):
        digest = m["digest"].split(":", 1)[1]
        seen.add(digest)
        f = blobs / digest
        if not f.is_file():
            continue
        man = json.loads(f.read_text())
        if "config" in man:
            seen.add(man["config"]["digest"].split(":", 1)[1])
        for layer in man.get("layers", []):
            seen.add(layer["digest"].split(":", 1)[1])
        for child in man.get("manifests", []):     # a manifest list
            seen.add(child["digest"].split(":", 1)[1])
    return seen


def main():
    state_path, store = Path(sys.argv[1]), Path(sys.argv[2])
    state = json.loads(state_path.read_text())
    if not (store / "index.json").is_file():
        return

    dropped = prune_index(store, state)
    candidates = [d.split(":", 1)[-1] for d in state.get("prune", [])]
    if not dropped and not candidates:
        return

    live = reachable(store)
    blobs = store / "blobs" / "sha256"
    deleted = kept = freed = 0
    for digest in candidates:
        if digest in live:
            kept += 1
            continue
        f = blobs / digest
        if f.is_file():
            freed += f.stat().st_size
            f.unlink()
            deleted += 1

    parts = []
    if dropped:
        parts.append(f"dropped {len(dropped)} stale index entr{'y' if len(dropped) == 1 else 'ies'}")
    parts.append(f"pruned {deleted} blob(s), {freed // 1024} KiB freed")
    if kept:
        parts.append(f"kept {kept} still reachable")
    print("  " + ", ".join(parts))


if __name__ == "__main__":
    main()
