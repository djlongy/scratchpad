# Moving container images across a one-way link, one new layer at a time

Two Quay registries that can never talk to each other, a folder that only flows one way
between them, and the rule that a layer which has already crossed is never sent again.
This is the operating procedure and the scripts, proven end to end on a laptop with one
compose file, so the same steps can be lifted onto a real diode.

```text
 LOW SIDE                                       ONE-WAY LINK          HIGH SIDE
 ┌─────────┐  export.sh   ┌────────────┐  NiFi send flow  ┌───────┐  NiFi recv  ┌────────────┐ import.sh ┌─────────┐
 │ Quay    │ ───────────▶ │ low-export │ ───────────────▶ │ diode │ ──────────▶ │ high-store │ ────────▶ │ Quay    │
 │ (low)   │ N, N-1, N-2  │ OCI archive│  unpack, dedupe  │ .tar  │  unpack     │ OCI store  │ skopeo    │ (high)  │
 └─────────┘  per repo    └────────────┘  vs Redis, repack └───────┘             └────────────┘           └─────────┘
                                                ▲
                                          Redis: digests
                                          that have crossed
```

What is here:

| File | Purpose |
|---|---|
| [`compose.yaml`](compose.yaml) | Low Quay, high Quay (each with its own Postgres and Redis, as Quay requires), one NiFi 2.5, the dedupe Redis, the three folders |
| [`scripts/quay-config.sh`](scripts/quay-config.sh) | Minimal Quay `config.yaml` for one side (random keys, local storage) |
| [`scripts/quay-init.sh`](scripts/quay-init.sh) | Waits for a Quay, creates the superuser through the API, keeps its token, creates the organisation |
| [`scripts/seed-images.sh`](scripts/seed-images.sh) | Builds and pushes a semver image family whose versions share layers (the thing worth deduplicating) |
| [`scripts/export.sh`](scripts/export.sh) | Low side: newest N semver tags of every repository in the organisation, into one OCI layout archive plus a manifest |
| [`scripts/nifi-flow.py`](scripts/nifi-flow.py) | Builds the send and receive flows through NiFi's REST API; [`nifi/*.json`](nifi/) are the same flows exported, for upload through the UI |
| [`scripts/oci-merge.py`](scripts/oci-merge.py) | High side: merge a received transfer into the persistent OCI store, verify every image is complete |
| [`scripts/import.sh`](scripts/import.sh) | High side: merge, then `skopeo copy` every image of the transfer into the high Quay |
| [`scripts/verify.sh`](scripts/verify.sh) | Digest on high == digest on low, `docker pull` from high, run the container |
| [`scripts/demo.sh`](scripts/demo.sh) | The three-transfer proof below, unattended |

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
python3 scripts/nifi-flow.py                                               # builds and starts both flows
```

Quay answers on `http://localhost:18081` (low) and `18082` (high), user `admin`, password
`quayadmin123` (override with `QUAY_USER`/`QUAY_PASS` before `quay-init.sh`). NiFi is at
`https://localhost:18090` (`admin` / `nifiadmin1234`, self-signed certificate). Ports are
chosen high to stay clear of the usual 8080/8443 tenants; change them in `compose.yaml`
and the `SERVER_HOSTNAME` passed to `quay-config.sh` together.

Quay 3.15 has an arm64 image, so this runs natively on Apple Silicon (tested in a 12 GB
Colima VM) as well as on x86_64.

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

### NiFi send flow (low side)

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

`ListFile` on `data/diode/` picks up `*-dedup.tar`, `FetchFile` moves it to `received/`,
`UnpackContent` splits it, and `PutFile` writes each entry to
`data/high-store/incoming/<transfer>/<path>`, recreating the archive's tree. That folder
is the hand-over point to the registry import.

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

| Transfer | Export selected | Blobs in the export | Blobs that crossed | Redis keys after | High store |
|---|---|---|---|---|---|
| 1 | 2.0.0, 1.2.0, 1.1.0 of both repos | 22 | 22 | 22 | 22 blobs, 6 images |
| 2 | after pushing 2.1.0: 2.1.0, 2.0.0, 1.2.0 | 22 | 6 (2 version layers, 2 configs, 2 manifests) | 28 | 28 blobs, 8 images |
| 3 | the same export again | 22 | 0 (3 metadata entries only) | 28 | unchanged, import is a no-op |

After each transfer `verify.sh` pulled the newest and the N-1 version from the high Quay
and ran them (`app 2.1.0`, `app 2.0.0`, ...), with the manifest digest identical on both
sides. `1.0.0`, `latest` and `dev` were never exported.

Run it yourself against a fresh stack:

```bash
scripts/demo.sh
```

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

**Do not automate a blob prune of the far-side OCI store.** The dedupe ledger
records a digest when it *sends* it, so a blob pruned from the store is never
resent and that image becomes unbuildable there, permanently. Reconcile registry
tags automatically; treat store size as a capacity decision.

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
  on separate instances (Process Group > Upload). The Redis pool service's connection
  string is the one value to change on the low side.
- **Redis is the ledger, and it records on sight.** `DetectDuplicate` writes the digest when
  it first sees it, not when the far side confirms receipt. On a strict one-way link there
  is no confirmation, so a transfer lost in flight leaves digests marked as crossed. The
  high side's `oci-merge.py` reports exactly which blobs are missing; to resend them,
  delete those keys (`redis-cli del sha256:...`, or `flushall` to resend everything) and
  export again. Keep Redis persistent (`appendonly yes` in `compose.yaml`) and back it up
  with the low-side registry.
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
  for the lab fixed it.
