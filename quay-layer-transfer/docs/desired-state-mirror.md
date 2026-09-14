# Keeping the high side in step: desired state, not "add these"

Proven end to end on two independent machines — a laptop compose stack and a
Linux VM. Every number below came from a run, not a design.

## The problem

A transfer that only says *"here are some images, add them"* cannot express a
deletion. So the high side drifts:

- a tag deleted upstream lives on the mirror forever
- a floating tag that moves leaves its old manifest behind, referenced by nothing
- a repository deleted upstream is never noticed

The mirror becomes a superset of the low side that only grows, and nothing on it
tells you which parts are still real.

## The fix, and why it is cheap

A tag is an independent `name -> digest` row in each registry. It is **not** an
alias: copying `1.2.0` does not make `latest` resolve on the far side, even when
both point at the same digest. Verified — before the change, `demo/app:latest` on
the high side returned `manifest unknown` while the identical manifest sat there
addressable by digest.

That gives a clean split:

| | unit | sent | cost |
|---|---|---|---|
| **Metadata** — the tag -> digest map | complete desired state | every transfer | a few KB |
| **Content** — layers | delta only | when new | megabytes |

`export.sh` now writes `state.json` next to `manifest.json`: the full intended
tag -> digest map for every repository, rebuilt and resent every run. It crosses
with no NiFi change, because the send flow routes on
`${path:contains('blobs/sha256')}` and everything else falls through to "always
continue".

`scripts/select-tags.py` decides what goes in it. Newest-KEEP semver is the
*content* filter; any floating tag (`latest`, `dev`, a major/minor alias) that
points at content already being sent is carried along, because it costs a tag row
and nothing else.

`scripts/reconcile.py` makes the far side match: add, retarget, delete. It is the
only writer — `manifest.json` lists only what a transfer carried, which cannot
express a deletion, so it is now just the fallback for transfers made before
`state.json` existed.

## What was proven

| Run | Change on the low side | Blobs crossed | Result on the high side |
|---|---|---|---|
| A | `latest` + `dev` enter the desired state | **0** (tar had 4 entries: index, oci-layout, manifest, state) | both tags added; `latest` resolves to the same digest as `2.1.0`; stale `1.1.0` deleted |
| B | `latest` -> 2.0.0, `1.2.0` deleted upstream | **0** | `1.1.0` added, `latest` retargeted, `1.2.0` deleted |
| C | `latest` -> 2.1.0, `dev` deleted upstream | **0** | 2 retargeted, 2 deleted, by reconcile alone |
| D | a corrupt `state.json` naming 1 of 4 tags | n/a | **refused**, nothing deleted |
| E | repeated on the second machine: floating tags in, `1.2.0` deleted upstream | **0** (4 tar entries) | 4 added, 2 deleted; `latest` resolves to `2.1.0`'s digest |

Run D is the guard: a run that would delete more than
`RECONCILE_MAX_DELETE_PCT` (default 50) of a repository's tags changes nothing
unless `RECONCILE_FORCE=1`. A truncated transfer and a genuine mass deletion look
identical from the high side, so the default is to stop.

Repositories absent from `state.json` are left alone for the same reason.

## The constraint that shapes any orphan prune

**Do not prune blobs from the high-side OCI store automatically.** Proven:

1. `a650501b…` is a layer unique to `demo/app:1.1.0`. Removed it from
   `data/high-store/oci`.
2. The low-side Redis ledger still holds the key (`exists` -> 1), because
   `DetectDuplicate` records a digest when it first *sends* it, not when the far
   side confirms receipt.
3. Next transfer: **0 blobs crossed**. The layer was not resent. `oci-merge.py`'s
   completeness check reported `missing blobs`.
4. The store can no longer produce that image:
   `skopeo copy oci:…:demo/app:1.1.0 dir:/tmp/…` fails with
   `reading blob sha256:a650501b…: no such file or directory`.

Nothing repairs this from the low side, because nothing on the low side knows.

A misleading detail worth knowing: pushing that broken image *to the high Quay*
still succeeded, because Quay already held the blob and skopeo skips uploading
what the destination has. The damage only shows on a destination that lacks the
content — a rebuilt Quay, or the standalone copy above. So "the push worked" is
not evidence the store is intact.

**Therefore the prune splits in two:**

- **Quay tags — safe, automatic.** `reconcile.py` deletes tags that left the
  desired state, and Quay garbage-collects the manifests they held. This is the
  orphan prune you want running every transfer.
- **OCI store blobs — manual, and paired with the ledger.** The store is the
  durable content cache that justifies the ledger's "already crossed" answer.
  Pruning it is only safe if the matching digests are cleared from the low-side
  Redis at the same time, which is an operator action on the low side with no
  return path to automate it. Treat store size as a capacity decision, not
  housekeeping.

## Not covered

- Repository deletion upstream (deliberate: absence is also what corruption
  looks like).
- Signature and attestation artifacts, which are themselves tags and only cross
  if selected.
- Multi-architecture. Single-arch amd64 is assumed throughout.
