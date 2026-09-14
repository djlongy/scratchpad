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

An update bot opened a merge request bumping a tag and its digest. Merging it
triggered a pipeline whose pack job pulled the **new** version through the cache,
dropped the pack where the link watches, and the far side ended up serving it.

Four upstream registries, four separate proxy-cache organisations, one pack:

```
docker.io/library/alpine:3.24.1 via hubcache
quay.io/prometheus/node-exporter:v1.6.0 via quaycache
registry.k8s.io/pause:3.8 via k8scache
ghcr.io/external-secrets/external-secrets:v0.9.0 via ghcrcache
docker.io/example/redis:6.2.9 via hubcache
docker.io/example/redis:8.2.1 via hubcache
wrote out/transfer-<stamp>.tar: 6 image(s), 30 blob(s), 160020 KiB
dropped out/transfer-<stamp>.tar to <host>:<watched path>
```

End state on the far side, an old version and a new one side by side, both pulled
and both executed rather than merely present:

| tag | runs as | digest against upstream |
|---|---|---|
| `6.2.9` | `Redis server v=6.2.9` | identical (that tag is a single manifest) |
| `8.2.1` | `Redis server v=8.2.1` | identical to the amd64 **child** manifest |

The second row is the two-digest behaviour described below, not a fault. Compare
against the child digest, not the index digest, or a correct mirror looks broken.

On the upgrade transfer **zero blobs crossed** — every layer was already in the
ledger from the earlier run — while the tag change still took effect. Names cross
every time; content crosses once.

## Keeping an old version pinned beside a new one

**Renovate updates every occurrence of a dependency.** Two lines for the same
image — one current, one held as a rollback target — become two lines at the
newest version the moment you merge, and the pin is silently gone. Worse, if the
two lines had different digests you end up with two entries claiming the same tag,
which is incoherent: the pack resolves whichever it saw last.

Hold the older line explicitly:

```json
{
  "matchDatasources": ["docker"],
  "matchPackageNames": ["docker.io/example/redis"],
  "matchCurrentValue": "/^6\\./",
  "enabled": false
}
```

Observed both ways: without the rule, merging the 8.x update rewrote the 6.x line
too and the reconciler refused the resulting transfer outright (its desired state
would have deleted every tag in the repository). With the rule, the same merge
moved only the 8.x line and both versions survived to the far side.

Renovate handles separate tracks well once it is told to: given `6.2.9` and
`8.2.0` it opened two independent merge requests, `v6.2.16` and `v8.2.1`, rather
than collapsing them.

## When upstream stops publishing versioned tags

Some publishers now ship only `latest` on the public registry and move the
versioned history elsewhere. One widely used set of community images does exactly
this: the repository still lists hundreds of tags, but they are Cosign signature
and attestation artifacts (`sha256-<hex>`), not the application — only `latest` is
the image. Its Helm chart still pins a versioned tag that no longer resolves, so
the chart's own default returns `manifest unknown`.

This matters more than it looks, because **`latest` cannot tell you whether a
transfer moved you forward.** Three options, in order of preference:

1. **Point the inventory at wherever the versioned history actually lives.** It is
   upstream-resolvable, so the update bot works normally and every merge request
   names the version you are approving. Check whether that location is frozen — a
   moved archive usually is, which gives you reproducible pinning but no future
   releases.
2. **A paid or authenticated feed** from the publisher, if ongoing updates matter.
3. **Track `latest` by digest**, accepting that the merge request says only "digest
   changed" and you are approving whatever the tag points at today. That is not a
   controlled upgrade.

`pack-inventory.py` softens option 3: it reads the version the image reports about
itself — `org.opencontainers.image.version`, then `app.kubernetes.io/version`, then
an `APP_VERSION` environment entry — and adds it as a **second tag inside the
pack**, so the far side gets a version-named tag even when upstream published only
`latest`. Measured: a `latest`-only image produced a pack carrying both `latest`
and `8.10.1`, sharing the same three blobs.

**That alias must never be written back into the inventory.** The inventory line
has to be something the update bot can resolve upstream; a version that exists only
in your mirror makes it query a tag that is not there, and it will quietly find
nothing. Git holds the upstream-resolvable reference; the mirror holds the
human-readable alias.

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

## Three failures a first import will hit

All three surfaced on a genuinely empty destination and none are obvious from the
error text:

- **A repository that does not exist answers 403, not 404.** A registry will not
  tell an unprivileged caller whether a private repository exists, so on a first
  import every lookup fails that way. Treat 403 and 404 alike when asking "what is
  live here" — they both mean nothing to reconcile against.
- **A push into an organisation that does not exist reports `authentication
  required`.** The credentials are fine; the namespace is missing. A repository is
  created on first push, but only inside a namespace that already exists, so the
  reconciler creates it.
- **A helper that turns 404 into `None` makes an existence check a silent no-op.**
  The namespace check looked correct and did nothing, because it tested for a
  raised exception rather than the returned value. Test the value.

## Credentials, again

The token a registry issues at first-time setup expires within hours — observed
twice in one session, the second time mid-proof. Application-specific tokens
authenticate to the *registry*, not to the management API, so they are not a
substitute. Use a robot account with API scope for anything scheduled.

Recovery is cheap if the content store is intact: rebuilding the destination
registry from empty and re-running the import restored every image, because the
store is the durable artefact and the registry is only the serving surface.
