# Merge an MR, get a transfer pack

The inventory in git is the desired state. A merge triggers a job that pulls every
approved digest **through the proxy caches** and builds the offline pack. Nothing
scans a registry, which is also why this works against caches that cannot list
what they hold.

Proven end to end on a self-hosted GitLab with its own runners.

## The repository

Four files:

```
images.txt                 the inventory: registry/repo:tag@sha256:digest, one per line
renovate.json              one custom manager that maintains it
.gitlab-ci.yml             two jobs: renovate, and pack
scripts/pack-inventory.py  builds the pack
```

## The pack job

```yaml
pack:
  image: alpine:3.21
  rules:
    - if: $CI_COMMIT_BRANCH == $CI_DEFAULT_BRANCH
  before_script:
    - apk add --no-cache skopeo python3
  script:
    - python3 scripts/pack-inventory.py images.txt out
        --registry "$LOW_REGISTRY" --creds "$LOW_CREDS"
  artifacts:
    paths: [out/]
    expire_in: 30 days
```

`LOW_REGISTRY` and `LOW_CREDS` are CI variables; mask the credential. The job needs
no Docker, no privileged runner and no socket — `skopeo` speaks the registry API.

The artifact is the transfer pack. To remove the last manual step, add one line to
`script:` copying `out/*.tar` to whatever the one-way link watches — a mounted
folder, or `aws s3 cp` to a bucket. That is the only part of this flow not
exercised below, because the target is site-specific.

## Proven

Renovate opened an MR bumping a tag and its digest. Merging it triggered a
pipeline whose pack job pulled the **new** version through the cache:

```
docker.io/library/alpine:3.24.1 via hubcache
quay.io/prometheus/node-exporter:v1.6.0 via quaycache
registry.k8s.io/pause:3.8 via k8scache
ghcr.io/external-secrets/external-secrets:v0.9.0 via ghcrcache
wrote out/transfer-20260914T161534Z.tar: 4 image(s), 23 blob(s), 66560 KiB
```

Four upstreams, four separate proxy-cache organisations, one pack.

## Two digests, and which one to pin where

The digest an update bot pins is what the registry returns for the tag — for most
images a manifest **index** over several platforms. An OCI layout cannot hold a
Docker-format manifest *list*: writing it as an OCI index changes its digest, and
`--preserve-digests` refuses. So the index digest cannot cross.

What crosses is the platform's child manifest, carried byte-for-byte under its own
genuine upstream digest. Verified: the layout held exactly the digest each upstream
index advertises for linux/amd64, media type intact.

```
index digest    approved in git; what gets pulled
child digest    what crosses, what the mirror serves, what to pin on the far side
```

Both are real upstream digests. `state.json` records the child digest, read back
from the layout rather than assumed, so the far side is told what it will serve.

## Traps this flow hit, in the order they appeared

- **An unset CI variable is an empty string**, not an error, so the job built
  `docker:///repo@digest` and skopeo failed on a malformed reference. The script now
  refuses an empty registry or credential.
- **The registry's configured hostname must be the address clients actually use.**
  Quay's auth challenge redirects to its `SERVER_HOSTNAME`; configured as
  `localhost`, an off-host runner is told to authenticate against *itself* and gets
  `dial tcp [::1]: connection refused`. The registry still answers on any Host — it
  is the generated auth realm that breaks.
- **Enabling the proxy-cache feature has to live in the config generator**, not be
  applied by hand, or the next rebuild silently turns it off and every proxy-cache
  API call returns 405.
