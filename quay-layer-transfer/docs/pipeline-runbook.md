# End-to-end runbook: connected registry to air-gapped registry

The whole pipeline in order, with what each stage runs and how far it has been
proven. Read `desired-state-mirror.md` first for *why* the far side is reconciled
from a desired state rather than told to add things.

**Assumed throughout:** linux/amd64 only, a connected "low" side with a registry
and proxy caches, a one-way link, and a disconnected "high" side with its own
registry and a cluster pulling from it.

## How far each stage is proven

| | Stage | Proven |
|---|---|---|
| 1 | Decide versions (Renovate) | **yes** — live, 6 MRs across 4 registries |
| 2 | Approve (merge) | **yes** |
| 3 | Pull to the low registry | design |
| 3b | Platform payload (`oc-mirror`) | evidence-backed, not executed |
| 4 | Update connected-side deployments | design |
| 5 | Export | **yes** |
| 6 | Cross the link | **yes** — 0 blobs on 4 runs |
| 7 | Import + reconcile + prune | **yes** — 14/14, twice from scratch |
| 8 | Deploy on the far side | evidence-backed, not executed |

Stages 1, 2 and 5-7 were executed end to end on live registries, twice, with a
destroy-and-rebuild in between. Stages 3, 4, 3b and 8 are recommendations with
sources; they are marked apart deliberately so they do not ride on the credibility
of the stages that actually ran.

## 1. Decide versions — on the low side, against real upstream

Renovate in CI, maintaining one inventory file whose every line is:

```
registry/repo:semver-tag@sha256:digest
```

**Point it at the real upstream registries, never at a pull-through cache.** A
cache answers tag discovery with only what has already been pulled — measured, 2
tags against 220 published. Renovate pointed at one proposes nothing and logs
success.

Renovate runs *as the job's image* (`image: renovate/renovate:<version>`). It needs
no Docker inside the job, no privileged runner and no socket; set
`binarySource=global` so it never launches sidecars.

Three things that have to be right:

- The match pattern must capture `currentDigest`, or the tag moves and the digest
  goes stale. **Write every line with a digest from day one** — Renovate can update
  one but cannot introduce one.
- Scope the digest-pinning rule to your own manager, or it also matches the CI
  file's `image:` line and proposes pinning Renovate itself.
- Guard the job with `rules: if $CI_COMMIT_BRANCH == $CI_DEFAULT_BRANCH`, or every
  Renovate branch push starts another run.

## 2. Approve

A human merges. This is the only manual step and it is the entire gate. Everything
after it is a consequence of the merge.

**Build the exception path before you need it.** A platform release is
all-or-nothing — you need every image or you have no cluster — so one unfixable
CVE blocks the whole version, including the fixes you wanted. Without a written,
expiring, signed-off accept-with-justification route, someone disables the gate
under deadline pressure and it never comes back on.

## 3. Pull to the low registry

Merge triggers a pull of the approved digests **through** the proxy caches. Serving
pulls is what caches are for; they simply cannot tell you what to pull.

### 3b. Platform payload

For a vendor platform (OpenShift release channels, operator catalogs), `oc-mirror`
is the only thing that resolves a channel into an actual image set. Run it
**mirror-to-mirror into the low registry**, not to disk:

- its archive is a distribution filesystem tree, not an OCI layout, so it cannot
  feed stage 5 directly;
- it does not prune, and its delete path removes manifests only;
- it is not a list maintainer — every additional image is a hand-written
  `name:tag`, with no ranges.

`--dry-run` emits `mapping.txt` of `source=destination` pairs, which is the
reviewable generated list and the natural bridge into the rest of this pipeline.

## 4. Update connected-side deployments

Reference the same digests the inventory names. The inventory is the source of
truth for both sides; anything that re-resolves a tag independently reintroduces
the skew this design exists to remove.

## 5. Export

```bash
scripts/export.sh 3        # 3 = newest semver versions per repository to keep
```

Produces one tar containing `oci/` (the layout), `manifest.json` (what this
transfer carries) and `state.json` (the complete desired tag→digest map, plus the
prune list). `select-tags.py` decides content by semver and carries any floating
tag pointing at content already being sent.

The prune list is computed here, and **the ledger is cleared here, before the
transfer leaves.** That ordering is the safety property: a lost transfer then costs
only a re-send.

## 6. Cross the link

NiFi unpacks the tar, drops any blob whose digest the ledger has seen, repacks the
survivors. Everything that is not a blob — index, layout, manifest, state — always
crosses, which is why names cost a few hundred bytes and content crosses once.

No flow change was needed to add `state.json`: the route rule is
`${path:contains('blobs/sha256')}` and everything else falls through.

## 7. Import, reconcile, prune

```bash
scripts/import.sh          # idempotent; safe to run on a timer
```

In order, and the order matters:

1. `oci-merge.py` merges into the persistent store and **checks completeness**.
   This is the primary guard on everything destructive below — an incomplete
   transfer is additive-only and waits in `incoming/` to retry.
2. `reconcile.py` makes the registry match the desired state: add, retarget,
   delete. It refuses a state document that is not newer than the last applied
   (so a replayed transfer cannot roll the mirror back) and refuses an empty
   desired set for any repository.
3. `prune-store.py` drops stale index entries first, then the listed blobs that
   nothing can still reach.

## 8. Deploy on the far side

**Pin by digest, keep the tag for humans.** A tag on the far side is an assertion
by whoever pushed it; across a link with no return path a mismatch is silent
forever, whereas a digest that is not there is an immediate, local failure.

Git cannot cross a one-way link — its transport is a negotiation. Render the
manifests on the low side, pin them to digests, and ship them as an OCI artifact
down the same channel as the images: Argo CD takes an `oci://` source, and Flux's
`OCIRepository` treats digest as taking precedence over everything else.

What you give up is pull-request promotion on the far side, since that needs an
SCM API. That is a real cost and the right trade against a transport that cannot
work.

## Operating it

- **Scheduled:** stage 1, the export/transfer cycle, and the import. All
  idempotent.
- **Human:** the merge at stage 2.
- **Never on a far-side timer:** pruning the content store. It happens only as a
  consequence of the low side forgetting first.

**Credentials.** Use a robot account. The token a registry issues at first-time
setup expires within hours. And make every job fail loudly on a rejected
credential: asking a registry what repositories exist with a dead token returns an
empty list rather than an error, which a pipeline reads as "nothing to mirror".

## Before trusting any of it

Run `scripts/test-lifecycle.sh`. It destroys everything and rebuilds from nothing,
then drives a blob out of the desired set and back in — the case that fails
silently and permanently if the ledger and the far store ever disagree.

Then check these against your own estate:

1. Does a floating tag resolve on the far side? `skopeo inspect … :latest`.
2. Do digests survive a round trip? If not you are on Docker's classic image
   store; the containerd snapshotter's driver is named `overlayfs`, and `overlay2`
   silently leaves you where you were.
3. What is your real layer reuse? Blob-level dedupe is worth 1-3% on public
   images, because layer digests change on rebuild. It only pays if you build
   in-house from pinned base digests. Measure two months of your own set before
   keeping the machinery.
4. Test mirror completeness with egress blocked. Cluster containerd tries the
   upstream endpoint last, so while you are connected a missing mirror entry
   silently succeeds from the internet.
