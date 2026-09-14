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

**The resolution is that the prune must be driven from the LOW side.**

The earlier conclusion here was that the far store could only be pruned by hand.
That was half right: it cannot be pruned *independently*. But the low side already
holds every fact the decision needs — it authored the desired state, and its export
layout contains every blob that desired state references. So:

    prunable = (everything the ledger has ever sent) - (everything still wanted)

and the low side can clear those ledger entries itself, in the same breath as it
tells the far side to drop them. `state.json` grows a `prune` list;
`scripts/prune-plan.py` computes it on the low side, `scripts/prune-store.py`
acts on it on the high side.

### The ordering is the safety property

The ledger is cleared **on the low side, before the transfer leaves**. If that
transfer is then lost, the low side has merely forgotten that it once sent those
blobs, and the next transfer that needs them sends them again. The reverse order —
far side deletes first, ledger cleared later — leaves a window where the content is
gone and the ledger still claims it crossed, which is the unrecoverable state this
whole design exists to avoid.

### Two stages on the high side, also ordered

1. **Drop unwanted entries from the store's `index.json`.** Deleting a tag from the
   registry does not remove the OCI store's own record of it, so without this the
   store can still reach every blob it ever received and stage 2 correctly concludes
   nothing may go. This was a real bug, found by the test: the first run reported
   *"pruned 0 blob(s); kept 6 still reachable"*.
2. **Delete the listed blobs nothing can reach any more**, re-checking each against
   the freshly pruned index. A digest still reachable is kept, however emphatically
   the low side asked.

That order means a half-finished prune leaves a store that is merely smaller, never
one whose index points at blobs that are gone.

### What actually guards a deletion

The percentage guard described in the first version of this document was the wrong
shape, and the test proved it: withdrawing one version upstream legitimately removes
that tag **plus every floating tag that pointed at it** — 3 of 4 tags in a small
repository — and the guard refused a completely valid transfer. A percentage cannot
tell a legitimate withdrawal from corruption.

What can:

- **The completeness check.** `import.sh` only reconciles or prunes when
  `oci-merge.py` confirms every blob the transfer's manifest references is present.
  A truncated transfer is additive-only and stays in `incoming/` to retry. This is
  the primary guard.
- **Monotonic transfer stamps.** The high side records the last desired state it
  applied and refuses anything not newer, so a replayed or out-of-order transfer
  cannot roll the mirror back to an earlier world.
- **An empty desired set is refused** for any repository. That is never a legitimate
  instruction — a repository with no wanted tags would simply not appear.
- **A 90% deletion backstop** remains for a well-formed but badly truncated
  document, overridable with `RECONCILE_FORCE=1`.

## Proving there is no catch-22

`scripts/test-lifecycle.sh` destroys the lab and rebuilds it from nothing, then uses
the semver window itself to drive a blob out of the desired set and pull it back in:

| | Change upstream | Desired set | What must happen |
|---|---|---|---|
| T1 | seed 1.0.0, 1.1.0 | {1.1.0, 1.0.0} | everything crosses |
| T2 | seed 2.0.0 | {2.0.0, 1.1.0} | 1.0.0 falls out, is pruned both sides |
| T3 | withdraw 2.0.0 | {1.1.0, 1.0.0} | **1.0.0 returns and must cross again** |

T3 is the test that matters. If the ledger and the far store ever disagree, nothing
crosses and the image is permanently unbuildable over there.

Two consecutive from-scratch runs, **14 passed / 0 failed** each:

```
== 2. 2.0.0 arrives — 1.0.0 falls out of the window and is pruned
    ledger: forgot 6 blob(s) no longer wanted; the far side may drop them
    dropped 2 stale index entries, pruned 6 blob(s), 6 KiB freed
  PASS the ledger has forgotten the doomed layer (0)
  PASS the far store no longer holds it

== 3. the catch-22 test — 2.0.0 is withdrawn, so 1.0.0 must come back
  PASS the forgotten layer was sent again (6 blob(s) crossed)
  PASS the far store holds it once more
  PASS the restored image is genuinely usable, not just present
  PASS digest on the high side matches the low side
```

Three things the from-scratch requirement caught that a re-run on a warm lab would
not have:

- The teardown recreated `data/` and destroyed a **default ACL**. NiFi runs as uid
  1000 and creates the per-transfer directory itself; renaming a directory needs
  write permission *on that directory*, so `import.sh` could not move it into
  `imported/`. Setup now sets the ACL explicitly.
- Container-created files cannot be removed by the runner user, so teardown needs
  elevation — the one place in the lab where it is warranted.
- A **vacuous pass**: when the helper that isolates a test layer returned nothing,
  three later assertions passed against an empty string. The test now aborts instead.

## Not covered

- Repository deletion upstream (deliberate: absence is also what corruption
  looks like).
- Signature and attestation artifacts, which are themselves tags and only cross
  if selected.
- Multi-architecture. Single-arch amd64 is assumed throughout.
