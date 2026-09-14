---
title: Air-gap the images into a basic Docker registry
type: runbook
status: active
tags: [airgap, redis, nifi, docker-registry]
updated: 2026-09-03
---

# Air-gap images into a basic Docker registry

Derived from the air-gap scripts published with Rancher releases (Apache-2.0),
extended so a publisher that no longer ships version tags can still be pinned and
compared across transfers.

**Use this when the destination is a basic Docker Registry**, which rewrites
content digests on push — so the upstream `@sha256:` cannot be the pull reference
on the far side. If your destination preserves digests (most full registries do),
prefer the OCI-layout flow in this repository's root, which keeps digest equality
end to end.

Pin is the **Hub index digest stored as a tag name** (`sha256-<hex>`). The app
version from the image label is an alias. A basic registry rewrites content
digests on push, so Hub's `@sha256:…` cannot be the pull ref on the destination.

Hub's `example/redis:sha256-<digest>` tags are Cosign/provenance, not Redis.
Pulling them does not give you the application image.

## 1. Save (machine with Hub access)

```bash
./rancher-save-images.sh \
  -l hack/airgap-images.txt \
  -i images.tar.gz \
  -p linux/amd64
```

Writes:

| file | what |
|---|---|
| `images.tar.gz` | both images, all pin tags |
| `rancher-images.resolved.txt` | tags inside the tar (load uses this) |
| `rancher-images.lock` | Hub digest + app version + track ref |

Redis tags in the tar look like:

- `example/redis:8.10.1` — label, human
- `example/redis:sha256-d75bda00…` — Hub index digest of `latest` when saved
- `example/redis:latest` — only because the list asked for `latest`

## 2. Load into the basic registry

```bash
./rancher-load-images.sh \
  --resolved-list rancher-images.resolved.txt \
  -i images.tar.gz \
  -r registry.example.com:5000
```

Daemon must list that registry under `insecure-registries` if it is HTTP.

Deploy with the digest tag:

```text
registry.example.com:5000/example/redis:sha256-<hex>
registry.example.com:5000/apache/nifi:2.11.0
```

## 3. Has Hub moved?

```bash
./rancher-save-images.sh --check-upstream --lock rancher-images.lock
```

Compares each lock `source_digest` to the live digest of `track_ref`
(`example/redis:latest` for a digest pin). Exit 1 means re-run step 1.

Copy `rancher-image-lib.sh` with the two scripts; they source it from the same directory.

Requires Docker 25+ (`docker save --platform`, `docker pull --platform`) and
`docker buildx imagetools inspect` for `--check-upstream`. No crane/skopeo.
