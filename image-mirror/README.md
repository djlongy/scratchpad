# image-mirror — a pinned image set, vetted, signed, and packed for an air gap

One manifest pins an RKE2 release. A bot opens the merge request that bumps it.
This pipeline resolves that release's image lists, fetches every image into one
OCI layout, builds an SBOM and a vulnerability report per image, decides a gate,
and — on the default branch — publishes the set to a registry, signs it, and
writes one transfer archive for a one-way link.

Nothing in here is tied to one estate or one vendor: every host, credential and
key is a variable. It pairs with [`../quay-layer-transfer/`](../quay-layer-transfer/),
which is what happens to the archive on the far side, but it is useful on its own
as "mirror an upstream image set into our registry, with evidence".

| File | Purpose |
|---|---|
| [`rke2.yaml`](rke2.yaml) | The manifest: `version`, `arch`, and which image lists to pull |
| [`mirror.sh`](mirror.sh) | resolve → fetch → vet → gate → publish → sign → export |
| [`ci.yml`](ci.yml) | The GitLab job. Include it from the repo root `.gitlab-ci.yml` |
| [`renovate.json`](renovate.json) | The regex manager that bumps `rke2.yaml`, with a 7-day cooldown |

Tools: `skopeo`, `syft`, `grype`, `curl`, `python3`. Publishing adds `cosign`,
and `aws` if you use the object store.

## Run it

```bash
MIRROR_PUBLISH=false ./image-mirror/mirror.sh      # vet only; needs no credentials at all
```

That resolves the pinned release, fetches every image, writes
`out/sbom/*.cdx.json` and `out/scan/*.json`, prints the per-image summary and the
gate decision, and pushes nothing. It is the whole merge-request pipeline, and it
is the fastest way to see whether a proposed bump makes the estate worse.

To publish:

```bash
export REGISTRY=registry.example.com REGISTRY_USER=robot REGISTRY_TOKEN=...
export COSIGN_KEY=hashivault://cosign          # or awskms://…, or ./cosign.key
export S3_ENDPOINT=http://objects.example.com:9000 S3_BUCKET=mirror
MIRROR_PUBLISH=true ./image-mirror/mirror.sh
```

| Variable | Default | What it does |
|---|---|---|
| `MIRROR_PUBLISH` | `false` | `true` also pushes, signs, attests and exports |
| `MIRROR_ORG` | `rke2` | Namespace every mirrored image lands under |
| `MIRROR_OUT` | `$PWD/out` | Where the store, SBOMs, reports and archive go |
| `GATE` | `ratchet` | `ratchet` \| `strict` \| `off` |
| `GATE_SEVERITY` | `critical` | Lowest severity the gate considers |
| `BASELINE_OBJECT` | `image-mirror/baseline/vulns.json` | Baseline key in the bucket |
| `REGISTRY`, `REGISTRY_USER`, `REGISTRY_TOKEN` | — | Publish target. Required to publish |
| `COSIGN_KEY` | — | Key file or cosign KMS URI. Required to publish |
| `S3_ENDPOINT`, `S3_BUCKET`, `S3_PREFIX` | — / — / `image-diode/low-export` | Object store. Unset = no baseline, no export |

## What each phase does

1. **resolve** — reads the manifest, downloads
   `rke2-images-<list>.<arch>.txt` for every list, dedupes into `out/images.txt`.
   The release tag carries a `+`, which is legal in a URL path but rewritten to a
   space by several proxies, so it is percent-encoded.
2. **fetch** — `skopeo copy` every image into **one** OCI layout at `out/store`,
   pinned to `--override-os` / `--override-arch` from `arch:`. One layout means a
   blob shared between images is stored once — which is the whole point when the
   set later has to cross a link. A public registry answers a blob fetch with 502
   now and then, so each image gets three whole-image retries on top of skopeo's
   per-request retry; a fourth failure is a real outage and stops the run.
3. **vet** — per image: `oci-archive` → `syft -o cyclonedx-json` →
   `grype --only-fixed -o json`. One SBOM and one report per image, kept as
   artifacts. `--only-fixed` because a finding with no fix is not an action.
4. **gate** — see below. Decided after the loop from the reports, so one run
   shows the whole blast radius instead of stopping at the first offender.
5. **publish** — push to `$REGISTRY/$MIRROR_ORG/...`, then `cosign sign` and
   `cosign attest --type cyclonedx` against the *pushed digest*.
6. **export** — one `transfer-<stamp>.tar` (`oci/` + `manifest.json`) uploaded to
   the object store, with blobs the far side already holds left out.

## The gate: a ratchet, not a threshold

| `GATE` | Fails when |
|---|---|
| `ratchet` | a fixable finding at or above `GATE_SEVERITY` appears whose CVE id the **baseline** does not carry. The baseline is the previous successful publish's `vulns.json` in the bucket. No baseline (first publish, or no object store) reports and passes. |
| `strict` | any fixable finding at or above `GATE_SEVERITY` exists. |
| `off` | never. The summary still prints and the reports are still artifacts. |

These are vendor images. "Fixable" means a newer Go module exists somewhere in
the tree, not that a newer upstream build exists — so a fixed-severity gate
blocks every release forever, and the only way past it is to silence the gate,
which is worse than not having one. The ratchet asks the question an operator can
act on: *does this release make things worse than what is already running?*

A scheduled weekly run re-scans the unchanged current release against the same
baseline, so a critical CVE published against a component that did not change
still shows up as `NEW` within the week. Never wrap the gate in `|| true`.

Measured on the pinned release (`v1.35.7+rke2r1`, `core` + `calico`, 31 images):
36 fixable low, 676 medium, **1057 high, 34 critical across 11 distinct ids**.
`strict` at `critical` fails on 6 of the 31 images; `strict` at `high` fails on
29 of them. That is the whole argument for the ratchet — there is no severity at
which a threshold gate lets a current, supported release through. The first
publish writes those 11 critical ids as the baseline, and from then on the gate
asks only what is new.

## Traps this has already hit

**A grype older than the database schema writes empty reports.** `grype db
update` fails its migration, and every scan after it produces a report with no
matches. With a quiet scanner and a swallowed exit code that reads as "every
image clean" — or, if you gate on the inverse, as "every image gated with zero
findings". So: update the database once, loudly, *before* the scan loop and fail
the run if that update fails (exit 2), and treat an empty or unwritten report as
a scanner failure, never as a result. Pin grype no older than the schema it will
download, and bump that pin with the database rather than with the rest of the
toolchain. A workstation will not reproduce this: its cache already holds an
older database the old client can read.

**A signature made without a transparency log does not verify by default.**
`cosign sign --tlog-upload=false` is what you want in an air gap, but
`cosign verify --key ...` still expects a Rekor entry and reports the signature
as missing. Pass `--insecure-ignore-tlog=true` to `verify` *and*
`verify-attestation`. This matters beyond verification: the idempotency guard
here is "verify, and sign only if that fails", so without the flag the guard
never passes and every run appends another signature layer to the
`sha256-<digest>.sig` tag. Nothing errors; the tag just grows. Count the layers
with `skopeo inspect --raw` on that tag to see it.

**busybox `wget` cannot tunnel HTTPS through a proxy.** In a bare alpine job it
is the only fetcher you have before `apk add`, and against an `https://` URL
through a proxy it connects and then reports `error getting response` even when
the proxy is perfectly healthy. Probe proxy health over plain `http://`, then
install `curl`, which tunnels correctly — as does `apk`.

**botocore ignores CIDR entries in `NO_PROXY`.** With only `192.168.0.0/16`
listed, every `aws` call to an internal object store goes through the proxy,
which denies it with 403 — while `curl` to the same host is fine, because curl
does understand the CIDR. List the object-store host by name (or IP) as well as
by CIDR. `ci.yml` derives it from `S3_ENDPOINT` so it cannot drift.

**Ask the object store for the object, not for its head.** The checksums-back
list is read with `s3api get-object`. Some S3-compatible gateways answer
`HeadObject` with HTTP 400 for aws-cli 2.x, which is indistinguishable from "no
such key" — and that reads as "the far side has nothing", so the export silently
ships every blob again. `get-object` returns a clean `NoSuchKey` that can be told
apart from a real error.

**An alpine job trusts no internal CA.** Install the chain before the first
registry call or publish fails with `x509: unknown authority` after a clean vet.

**A release-age cooldown needs a datasource that reports release timestamps.**
Renovate enforces `minimumReleaseAge` only when the candidate carries a
`releaseTimestamp`: `github-releases` supplies one, `git-tags` does not. Changing
datasource to dodge a missing token turns the cooldown into a silent no-op while
the config still reads as if it holds. Give the runner a GitHub token (no scopes
needed) and keep `github-releases`. The regex `versioning` doubles as the
pre-release filter — a tag that does not match is never a candidate.

## Wiring it to a registry and an object store

- **Registry.** Use a robot / service account scoped to write into
  `$MIRROR_ORG` only, and nothing else. `mirror.sh` builds a mode-600 auth file
  from the environment with python so the token never reaches a command line, a
  process listing, or the job log — keep it that way if you change the push.
- **Signing key.** A KMS URI (`hashivault://`, `awskms://`, `gcpkms://`,
  `azurekms://`) means no private key exists on disk anywhere, which is why
  there is no `cosign.pub` in this directory. A key file works too; supply it as
  a CI *file* variable and set `COSIGN_PASSWORD`.
- **Object store.** Any S3-compatible endpoint. It holds two things: the
  ratchet baseline, and the export archives. Leave `S3_ENDPOINT` unset and the
  pipeline still resolves, fetches, vets and gates — it just has no memory
  between runs and nothing to hand to the link.
- **The far side.** `transfer-<stamp>.tar` is the same shape
  `quay-layer-transfer` produces and consumes: `oci/` plus a `manifest.json`
  that lists every image with its manifest, config and layer digests. The
  importer there merges it into a persistent OCI store and proves completeness
  against that manifest. If the far side can publish a digest list back (a
  `have/blobs.txt` beside the export prefix), this exporter reads it and leaves
  those blobs out of the archive while `manifest.json` still names every layer —
  the bidirectional mode described in that project's README.

## Verify a mirrored image

```bash
skopeo inspect --format '{{.Digest}}' docker://$REGISTRY/rke2/rancher/hardened-coredns:<tag>
cosign verify --key "$COSIGN_KEY" --insecure-ignore-tlog=true \
  "$REGISTRY/rke2/rancher/hardened-coredns@sha256:..."
cosign verify-attestation --key "$COSIGN_KEY" --type cyclonedx --insecure-ignore-tlog=true \
  "$REGISTRY/rke2/rancher/hardened-coredns@sha256:..."
```

The digest must equal the one recorded for that ref in `out/manifest.json`.
