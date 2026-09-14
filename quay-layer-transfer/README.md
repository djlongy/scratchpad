# Moving container images across a one-way link, one new layer at a time

Two Quay registries that can never talk to each other, a folder that only flows one way
between them, and the rule that a layer which has already crossed is never sent again.
This is the operating procedure and the scripts, proven end to end on a laptop with one
compose file, so the same steps can be lifted onto a real diode.

```text
 LOW SIDE                                       ONE-WAY LINK          HIGH SIDE
 ┌─────────┐  export.sh   ┌────────────┐  NiFi send flow  ┌───────┐  NiFi recv  ┌────────────┐ import.sh ┌─────────┐
 │ Quay    │ ───────────▶ │ low-export │ ───────────────▶ │ diode │ ──────────▶ │ high-store │ ────────▶ │ Quay    │
 │ (low)   │ N, N-1, N-2  │ OCI archive│                  │ .tar  │  unpack     │ OCI store  │ skopeo    │ (high)  │
 └─────────┘  per repo    └────────────┘                  └───────┘             └────────────┘           └─────────┘
                                 ▲                                                     │
                                 └──────── have/blobs.txt: digests the store holds ◀────┘
                                           (bidirectional mode — a list of hashes, no payload)
```

Two ways to decide what not to send, one command apart:

| | Who dedupes | What crosses | The flow |
|---|---|---|---|
| **bidirectional** (default) | `export.sh`, against the far side's published digest list | only blobs the far side does not have | ListFile → FetchFile → PutFile |
| **oneway** | NiFi, against a Redis ledger of what it has sent | the whole archive, minus what the ledger recognises | + UnpackContent → DetectDuplicate → MergeContent |

What is here:

| File | Purpose |
|---|---|
| [`compose.yaml`](compose.yaml) | Low Quay, high Quay (each with its own Postgres and Redis, as Quay requires), one NiFi 2.5, the dedupe Redis, the three folders |
| [`scripts/quay-config.sh`](scripts/quay-config.sh) | Minimal Quay `config.yaml` for one side (random keys, local storage) |
| [`scripts/quay-init.sh`](scripts/quay-init.sh) | Waits for a Quay, creates the superuser through the API, keeps its token, creates the organisation |
| [`scripts/seed-images.sh`](scripts/seed-images.sh) | Builds and pushes a semver image family whose versions share layers (the thing worth deduplicating) |
| [`scripts/export.sh`](scripts/export.sh) | Low side: newest N semver tags of every repository in the organisation, into one OCI layout archive plus a manifest, minus whatever the far side says it already holds |
| [`scripts/nifi-flow.py`](scripts/nifi-flow.py) | Builds the send and receive flows through NiFi's REST API, `--mode bidirectional` (default) or `--mode oneway`; [`nifi/*.json`](nifi/) are the oneway flows exported, for upload through the UI |
| [`scripts/oci-merge.py`](scripts/oci-merge.py) | High side: merge a received transfer into the persistent OCI store, verify every image is complete |
| [`scripts/import.sh`](scripts/import.sh) | High side: merge, `skopeo copy` every image of the transfer into the high Quay, and publish the store's digest list back for the next export |
| [`scripts/verify.sh`](scripts/verify.sh) | Digest on high == digest on low, `docker pull` from high, run the container |
| [`scripts/demo.sh`](scripts/demo.sh) | The three-transfer proof below, unattended, in either mode |
| [`scripts/test-lifecycle.sh`](scripts/test-lifecycle.sh) | Tears the lab down and proves cross → prune → **re-cross**: the check that a blob dropped from the far store can be sent again. `oneway` by construction |

Tools on the operator machine: docker with compose, skopeo, python3, curl. Nothing else;
the scripts are bash plus stdlib Python.

## 1. Start the lab

```bash
scripts/quay-config.sh low  localhost:18081
scripts/quay-config.sh high localhost:18082
mkdir -p data/low-export data/diode data/high-store && chmod 777 data/*   # NiFi runs as uid 1000
docker compose up -d                                                       # first run pulls ~2.5 GB
scripts/quay-init.sh low  http://localhost:18081
scripts/quay-init.sh high http://localhost:18082
python3 scripts/nifi-flow.py                                               # bidirectional; --mode oneway for the other shape
```

Quay answers on `http://localhost:18081` (low) and `18082` (high), user `admin`, password
`quayadmin123` (override with `QUAY_USER`/`QUAY_PASS` before `quay-init.sh`). NiFi is at
`https://localhost:18090` (`admin` / `nifiadmin1234`, self-signed certificate). Ports are
chosen high to stay clear of the usual 8080/8443 tenants. To move them, set `LOW_PORT`,
`HIGH_PORT` and `NIFI_PORT`, and pass the matching `SERVER_HOSTNAME` to
`quay-config.sh`, `LOW_REGISTRY` / `HIGH_REGISTRY` to the scripts and `NIFI_URL` to
`nifi-flow.py` — which is also how you run a second copy of the lab beside the first
(`docker compose -p lab2 …`).

Quay 3.15 has an arm64 image, so this runs natively on Apple Silicon (tested in a 14 GB
Colima VM) as well as on x86_64. Budget about 3 GB per Quay with the worker caps in
`compose.yaml`; see §5 for what happens without them.

## 2. The procedure

### Low side: export N, N-1, N-2 of every repository

```bash
scripts/export.sh 3        # 3 = how many newest semver versions per repository
```

`export.sh` asks the Quay API for every repository in the organisation (`QUAY_ORG`,
default `demo`), keeps the tags that look like `1.2.3` or `v1.2.3`, sorts them as version
tuples (so `2.0.0` beats `10.0.0`'s string order and `latest`, `dev`, `rc1` are ignored),
takes the newest three, and `skopeo copy`s them into **one** OCI image layout. An OCI
layout stores blobs by digest, so a layer shared by six images is on disk once. The archive
is `oci/` plus a `manifest.json` that lists every image with its manifest, config and layer
digests, tarred as `data/low-export/transfer-<UTC stamp>.tar`.

Copy that file onto whatever feeds the diode. In the lab that is the same folder NiFi
watches.

In **bidirectional** mode (the default) `export.sh` first reads
`data/low-export/have/blobs.txt` — the digest list the far side published — and leaves
every blob it names out of the tar. `manifest.json` still lists **every** layer of every
image, which is what lets the far side prove a merged image is complete even though most
of its layers arrived months ago. `TRANSFER_MODE=oneway` skips that step and packs
everything, leaving the dedupe to NiFi.

### The return channel: `have/blobs.txt`

`import.sh` writes the sorted digests of its OCI store to
`data/low-export/have/blobs.txt` on **every** exit — a successful import, a failed one, or
an empty queue — because a run that merged blobs and then failed later has still changed
what the high side holds, and a stale list makes the low side re-send things that are
already there. It is written to a temporary name and moved into place, so the exporter
never reads half a list.

What crosses back is a sorted list of hashes and nothing else: no filenames, no image
names, no tags, no bytes of content. That is the whole point — it is small enough and
dull enough to argue for on a link where the default answer to "can anything come back?"
is no. If the answer really is no, run `oneway` and nothing changes on the low side
except that the archive is bigger.

The direction of trust is the reason to prefer it where it is allowed. The Redis ledger
records a digest when NiFi **sends** it, so it describes what the low side believes; a
transfer lost in flight leaves digests marked as crossed and the far side permanently
short. `have/blobs.txt` describes what the far side **has**, so a lost transfer corrects
itself on the next export with no operator action and no `redis-cli del`.

### NiFi send flow (low side) — bidirectional

```bash
python3 scripts/nifi-flow.py --mode bidirectional --reset
```

`ListFile` picks up `transfer-*.tar` from `data/low-export` (not recursing, so the
`have/` folder is never listed as something to send), `FetchFile` moves it to `sent/`, and
`PutFile` writes it to `data/diode/` under the same basename. Three processors, no state,
no Redis. The archive crosses byte for byte.

Deduplicating a second time would repeat work the exporter already did, and every
processor removed is a processor that cannot corrupt an archive — the unpack/merge shape
below is the one with the timing failure described in §5.

### NiFi send flow (low side) — oneway

```bash
python3 scripts/nifi-flow.py --mode oneway --reset
```

`ListFile` picks up `transfer-*.tar`, `FetchFile` moves it to `sent/`, `UnpackContent`
splits it into one flowfile per entry, and `RouteOnAttribute` sends every entry under
`oci/blobs/sha256/` to `DetectDuplicate`, whose cache key is the file name, which **is** the
blob digest. The cache is a `RedisDistributedMapCacheClientService` on the dedupe Redis.
A digest seen for the first time is recorded and the blob continues; a digest already in
Redis is dropped. `index.json`, `oci-layout` and `manifest.json` always continue.
`MergeContent` (bin-packing, correlated on the archive's `fragment.identifier`, tar format,
paths kept, a bin closes on age only, 30 s after its first entry) repacks what survived; `UpdateAttribute` names it
`transfer-<stamp>-dedup.tar`; `PutFile` writes it to `data/diode/`.

So the second time a base layer is exported it never leaves the low side, and the
archive that crosses the link carries only blobs the high side has not got.

### NiFi receive flow (high side)

The same in both modes. `ListFile` on `data/diode/` picks up `transfer-*.tar`, `FetchFile`
moves it to `received/`, `UnpackContent` splits it, and `PutFile` writes each entry to
`data/high-store/incoming/<transfer>/<path>`, recreating the archive's tree. The
per-transfer folder is the archive name with `.tar` and any `-dedup` stripped, so a
transfer lands under the same name whichever send flow produced it and `import.sh` needs
no mode of its own. That folder is the hand-over point to the registry import.

### High side: merge and import

```bash
scripts/import.sh                 # every transfer under incoming/ not yet imported
scripts/verify.sh demo/app 2.0.0 1.2.0
```

`oci-merge.py` copies the received blobs into `data/high-store/oci/`, a persistent OCI
layout that accumulates every transfer, verifying each blob's sha256 on the way, and merges
the received `index.json` into the store's index (same ref name replaces, new appends).
Then it walks `manifest.json` and fails if any image still has a blob missing, which is the
check that catches a lost transfer. `import.sh` then `skopeo copy`s each image from the
store into the high Quay. Because the store holds every blob that ever crossed, an image
whose base layers crossed weeks ago is complete there even though this transfer carried
only its new layer.

`verify.sh` compares the manifest digest on both sides, pulls from the high Quay and runs
the container.

## 3. The proof (`scripts/demo.sh`)

Seed: `demo/app` and `demo/api`, tags `1.0.0 1.1.0 1.2.0 2.0.0`, plus `latest` and `dev`.
Each image is four layers: base, runtime (shared by all), app (shared per repository),
version (unique).

The three transfers were run twice, once in each mode, each against a lab torn down to
nothing first. **The same blobs cross either way. What differs is how much work the low
side does to achieve it.**

`bidirectional` — the exporter leaves out what the far side published, so the file it
writes *is* the file that crosses:

| Transfer | Packed of 22 | Archive written and crossed | Store after |
|---|---|---|---|
| 1 | 22 | **8.0M** | 22 blobs, 8 images |
| 2 | 6 (16 left out) | **40K** | 22 blobs, 10 images (6 merged, 6 pruned) |
| 3 | 0 (22 left out) | **24K** | unchanged, import is a no-op |

`oneway` — the exporter packs everything and NiFi drops what the ledger recognises:

| Transfer | Packed of 22 | Archive written | Archive that crosses | Store after |
|---|---|---|---|---|
| 1 | 22 | 8.0M | 8.0M (22 blobs) | 22 blobs, 8 images |
| 2 | 22 | 8.0M | 24K (6 blobs) | 22 blobs, 10 images (6 merged, 6 pruned) |
| 3 | 22 | 8.0M | 16K (0 blobs) | unchanged, import is a no-op |

Six blobs is the real delta of a patch release here: two version layers, two configs, two
manifests. `oneway` still builds and writes an 8 MB archive for it, then unpacks 22 blobs,
looks each up in Redis, and repacks six — on the low side's disk and CPU, and across the
wire first if the exporter is not on the link's own host. `bidirectional` never builds the
8 MB: the third transfer, which carries nothing at all, is 24 KB of metadata.

After each transfer `import.sh` reported `missing: none` — the line that says the merged
image is complete in the store, not merely plausible, even though most of its layers
arrived in an earlier transfer — and `verify.sh` pulled the newest and the N-1 version
from the high Quay and ran them, with the manifest digest identical on both sides. Both
modes finished with a store of 22 blobs and 10 images.

Run it yourself against a fresh stack:

```bash
python3 scripts/nifi-flow.py --mode bidirectional --reset && scripts/demo.sh
TRANSFER_MODE=oneway scripts/demo.sh    # after rebuilding the flow with --mode oneway
```

`scripts/test-lifecycle.sh` is the other regression: it tears the lab down, drives a blob
out of the semver window, proves the far store drops it, then withdraws a version upstream
so the blob is wanted again and must re-cross. That one is `oneway` by construction — it
asserts what the Redis ledger holds at each step, and in `bidirectional` mode there is no
ledger to assert on.

## 3a. Keeping the far side in step (desired state)

A transfer that only says "add these images" can never express a deletion, so an
additive mirror drifts: a tag removed upstream lives on the far side forever, and
a floating tag that moves leaves its old manifest behind, referenced by nothing.

A tag is an independent `name -> digest` row in each registry — **not** an alias.
Copying `1.2.0` does not make `latest` resolve on the far side even when both name
the same digest.

So `export.sh` also writes `state.json`: the complete intended tag -> digest map
for every repository, resent in full on every transfer. It costs a few KB because
tags are pointers, while layers are megabytes and still cross only once.
`scripts/reconcile.py` then makes the far side match — add, retarget, delete — and
is the only writer on the high side.

```bash
scripts/select-tags.py "$API" demo 3 .secrets/low.token   # content filter + desired state
scripts/export.sh 3                                       # writes oci/, manifest.json, state.json
scripts/import.sh                                         # merge, then reconcile
```

Two safety properties, both deliberate: a run that would delete more than
`RECONCILE_MAX_DELETE_PCT` (default 50) of a repository's tags refuses unless
`RECONCILE_FORCE=1`, and repositories absent from `state.json` are left alone —
because absence is also what a truncated transfer looks like.

**Whether a blob prune of the far-side OCI store is safe depends on the mode.**
In `oneway` the dedupe ledger records a digest when it *sends* it, so a blob
pruned from the store is never resent and that image becomes unbuildable there,
permanently. `prune-plan.py` makes it safe by clearing the ledger on the low side
*before* the transfer leaves — worst case a lost transfer costs a re-send — and
`test-lifecycle.sh` exists to prove that path end to end. In `bidirectional` the
whole hazard is gone: the far side republishes `have/blobs.txt` from its store
after every import, so a blob it no longer holds is simply one the next export
packs again. `prune-plan.py` takes that file instead of the ledger and has
nothing to forget.

Full design, evidence and the runs that proved it: [`docs/desired-state-mirror.md`](docs/desired-state-mirror.md).

## 3b. The whole pipeline, end to end

[`docs/pipeline-runbook.md`](docs/pipeline-runbook.md) puts the stages in order —
deciding versions, approving, pulling, exporting, crossing, reconciling, pruning
and deploying — and marks which stages were executed against live registries and
which are recommendations. Read it if you are building this rather than reading
about it.

## 3c. Driving it from git

[`docs/inventory-driven-pipeline.md`](docs/inventory-driven-pipeline.md) — an
update bot maintains an inventory of `repo:tag@digest`, merging an MR triggers a
job that pulls those digests through the proxy caches and builds the transfer
pack. Proven on a self-hosted GitLab.

## 4. Taking it to a real link

- **Replace the folder with the diode.** The send flow's `PutFile` and the receive flow's
  `ListFile` are the only two processors that know about `data/diode/`; point them at the
  sender's outbound directory and the receiver's inbound directory (or swap them for the
  diode vendor's NiFi processors, or `PutSFTP`/`ListSFTP`).
- **Two NiFi instances.** In the lab both flows live in one NiFi; the exported
  [`nifi/low-side-flow.json`](nifi/low-side-flow.json) and
  [`nifi/high-side-flow.json`](nifi/high-side-flow.json) upload as separate process groups
  on separate instances (Process Group > Upload). Those two are the `oneway` shape, so the
  Redis pool service's connection string is the one value to change on the low side; for
  `bidirectional` there is no service to point anywhere, and `nifi-flow.py --mode
  bidirectional` builds the three processors in less time than an import takes.
- **Ask for the return channel before you accept the ledger.** In `oneway` mode Redis is
  the ledger and it records on sight: `DetectDuplicate` writes the digest when it first
  sees it, not when the far side confirms receipt. On a strict one-way link there is no
  confirmation, so a transfer lost in flight leaves digests marked as crossed. The high
  side's `oci-merge.py` reports exactly which blobs are missing; to resend them, delete
  those keys (`redis-cli del sha256:...`, or `flushall` to resend everything) and export
  again — an operator action, on a list a human has to read out of one side's logs and
  apply on the other. Keep Redis persistent (`appendonly yes` in `compose.yaml`) and back
  it up with the low-side registry. In `bidirectional` mode none of that exists: the far
  side states what it holds, so a lost transfer is corrected by the next export on its
  own. A one-way link that permits a hash list back is a materially better one, and this
  is the argument to take to whoever owns the link.
- **Tag policy.** `export.sh` keeps the newest N semver tags per repository. Pre-releases
  (`1.2.3-rc1`) and moving tags (`latest`) are excluded on purpose; widen the regex in
  `export.sh` if the group is versioned differently.
- **Credentials.** The scripts take the Quay superuser for brevity. In production use a
  robot account per side with `read` on the low organisation and `write` on the high one,
  and give `export.sh` an OAuth token from an application with `repo:read`.
- **Signatures and attestations.** Cosign signatures are OCI artifacts referring to the image
  digest; export them with the image (`skopeo copy --all` handles multi-arch indexes,
  signatures need `--sign-by`/`--preserve-digests` or a `cosign save`), and they dedupe
  like any other blob.
- **What the layer counts mean for bandwidth.** Transfer 2 above moved 6 blobs of 22, and
  the six were the small ones (a 30-byte layer, two configs, two manifests). A real
  application release with a rebuilt base image moves the base once for the whole group.

## 5. Gotchas met building this

**Never POST to `/api/v1/user/initialize` as a probe.** It is one-shot per empty
database and creates a real superuser, so a diagnostic call burns the path
`quay-init.sh` needs; the only way back is deleting the Quay database volumes. On
a fresh stack it also returns 403 for a minute or two *after* `/v2/` already
answers 401, which is why `quay-init.sh` now retries instead of trusting the
`/v2/` readiness gate.

**The token `user/initialize` returns expires — do not build a pipeline on it.**
Both sides' tokens started returning 401 within a few hours of being minted, and
the registry log says it plainly: `OAuth access with an expired token`. Nothing in
the generated config sets a lifetime, so this is the default for that token. Fine
for a lab session, useless for anything scheduled: a recurring job needs a robot
account or an application-specific token instead, and needs to fail loudly when
its credential stops working rather than reporting an empty result.

**Quay's API v1 needs the OAuth bearer token.** Basic auth with the same
credentials authenticates registry pull and push but the API rejects it with 401.

**An expired token looks like an empty organisation.** `/repository?namespace=…`
returns `{"repositories": []}` rather than 401, because private repositories are
invisible to an unauthenticated caller — so a dead token would silently produce a
desired state of nothing. `select-tags.py` probes an endpoint that does return 401
and refuses.

**Docker's classic image store breaks digest equality.** It keeps schema2
manifests, which an OCI layout cannot hold, so manifests are re-encoded in transit
and no digest matches. Enable the containerd snapshotter — and note the driver is
named `overlayfs` there; `overlay2` silently leaves you on the old store. Then set
`BUILDX_NO_DEFAULT_ATTESTATIONS=1`, because BuildKit's default provenance
attestation wraps the build in an OCI index and breaks the same comparison one
level up. Both failures produce a working image with the wrong digest.

**Verify a layout against `dir:`, not a registry.** Pushing a damaged OCI layout to
a registry that already holds the blobs succeeds, because the client skips
uploading what the destination has. "The push worked" is not evidence the layout
is intact.


- Quay's `/health/instance` pings `SERVER_HOSTNAME` from inside the container. With a host
  port mapping (`localhost:18081` outside, `8080` inside) that ping fails and the health
  endpoint stays 503 while the registry works. The compose health check uses `/v2/`
  (401 means up) instead.
- Repositories created by `docker push` are private in Quay, so every `skopeo` call against
  it needs `--creds`, including `inspect`.
- NiFi 2.x images are HTTPS-only with single-user credentials; `nifi-flow.py` logs in with
  `POST /access/token` and disables certificate verification for the lab's self-signed cert.
  `NIFI_WEB_PROXY_HOST` must name the host:port you use in the browser.
- `DetectDuplicate` keys on `${filename}`; `UnpackContent` sets `path` and `filename` per
  entry, so blobs are keyed by digest with no parsing.
- `MergeContent` in defragment mode waits for every fragment of the archive; dropped
  duplicates would starve it. Bin-packing correlated on `fragment.identifier` with a bin
  age is what makes "merge whatever survived" work. The minimum entry count must be
  unreachable: with "minimum 1", MergeContent merges a bin the moment nothing is queued
  behind it, so the three metadata entries (routed straight to the merge) left in their own
  archive while the blobs were still in `DetectDuplicate`, and the second archive with the
  same name replaced the first. Found on the second full run, not the first: timing.
- macOS `tar` adds AppleDouble `._*` entries which the flow would count as blobs;
  `export.sh` sets `COPYFILE_DISABLE=1`.
- Docker Desktop's credential helper (`credsStore: desktop` in `~/.docker/config.json`)
  hung every pull through a second Colima VM's socket; an empty `DOCKER_CONFIG` directory
  for the lab fixed it. If you point the lab at a non-default daemon, select it with
  `DOCKER_HOST` and not `DOCKER_CONTEXT`: contexts are stored *inside* `DOCKER_CONFIG`, so
  the empty directory that fixes the credential helper also makes every named context
  vanish (`context not found`).
- **Quay sizes its worker pools from the host CPU count.** On a 6-CPU VM each instance
  settled at 5.3 GB RSS, so the pair alone wanted more than 10 GB and a second copy of the
  lab would not start beside the first. `WORKER_COUNT_WEB` / `WORKER_COUNT_REGISTRY` /
  `WORKER_COUNT_SECSCAN` (set in `compose.yaml`) cap them: with those and 4 CPUs an
  instance settles at 3.1 GB, both Quays report healthy in about 45 s, and the whole lab
  fits in a 14 GB VM with room to spare. `QUAY_OVERRIDE_SERVICES` can switch whole
  services off if you need to go further.
