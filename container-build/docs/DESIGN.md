# Design notes

Rationale for the non-obvious decisions in `scripts/build.sh` and the
scan/ingest flow. The scripts carry one-line pointers here instead of
inline essays, so the code stays readable.

## Two roots: TEMPLATE_ROOT vs PROJECT_ROOT

The template is cloned and invoked by per-image repos:

```
git clone --depth 1 ${TEMPLATE_REPO} .template
cd <per-image-repo>            # has image.env + Dockerfile + certs/
bash .template/scripts/build.sh
```

- **TEMPLATE_ROOT** — where `build.sh` and its sibling libs / push
  backends / scan scripts live. Computed from `BASH_SOURCE`. Read-only;
  artifacts never land here.
- **PROJECT_ROOT** — where `image.env`, `Dockerfile`, `certs/` live and
  where `build.env` / `sbom.cdx.json` / `vuln-scan.json` are written.
  Defaults to the operator's CWD; override with `--project-root <path>`
  or the `PROJECT_ROOT` env var for callers that can't `cd` first.

When the per-image repo *is* the template repo (e.g. the template's own
tests), the two coincide and the contract still holds — no special case.

## Reproducible build digest (SOURCE_DATE_EPOCH)

Two consecutive `--push` of the **same commit** must produce the **same**
image digest. The only thing that used to differ between runs was
`CREATED=$(date now)`, which fed `org.opencontainers.image.created` and
(via BuildKit) the image config's own timestamps — so each build got a
fresh manifest digest, the re-push **orphaned** the previous digest in
the registry, and any scan still holding that digest failed immediately
(`manifest not found`).

Fix: anchor the build clock to the git **commit** time. BuildKit honours
`SOURCE_DATE_EPOCH` (clamps layer + config timestamps), so identical
source → identical digest, and a re-push is a no-op overwrite to the same
digest (nothing orphaned). Precedence:

1. `SOURCE_DATE_EPOCH` already in env → respected (CI override)
2. git committer date of HEAD → reproducible per commit
3. wall clock (no git) → last resort, non-reproducible

It is **not** exported globally — `build.sh` passes it inline on the
buildx command so it can't leak into the caller's shell and silently pin
a later same-shell run's timestamp.

## buildx attestation flags / flat manifest

When buildx is present we pass `--provenance=false --sbom=false` to force
a **flat single-arch v2** distribution manifest (config + layers in the
tag dir) instead of an OCI image index wrapping the manifest + an
attestation manifest. The index lands in JFrog as
`<tag>/list.manifest.json` with layer blobs outside the tag dir, which
makes the Free-tier build-info merger (`lib/build-info-merge.py`) report
"1 artifact, 0 dependencies (fallback)" instead of the proper
"manifest + config + N layers" count.

When buildx is **not** installed (some hosted CI runners ship plain
Docker Engine), `docker build` rejects those flags, so we fall back to a
vanilla `docker build` — non-buildx Docker never produces OCI indices
anyway, so the flags wouldn't have served a purpose there.

We don't consume buildx's provenance/SBOM attestations — Xray covers
provenance, and Syft/Trivy/Xray + `sbom-post.sh` cover SBOMs as their own
stages — so disabling them is lossless.

## Identity hand-off on the no-push path

A feature-branch pipeline that only builds still has to tell postscan
jobs which image to scan, and the honest answer is never the upstream
reference: the built image is the upstream plus this repo's changes, and
a scan of the upstream would pass for an image nobody runs.

So a no-push build exports an OCI image layout archive with
`docker buildx build --output type=oci,dest=image.oci.tar`, and
`scripts/lib/image-identity.sh` writes:

- `IMAGE_TRANSPORT=oci-archive` and `IMAGE_REF=oci-archive:image.oci.tar`
  — the form a file-based scanner consumes, with no daemon and no
  registry. Nothing invents a pullable registry reference for an image
  that was never published.
- `IMAGE_DIGEST` — the manifest digest read from the archive's own
  `index.json`, cross-checked against BuildKit's `--metadata-file`. A
  local config/image ID is never written into a digest field.
- `IMAGE_ARCHIVE_SHA256` — so a consumer can tell a truncated artifact
  fetch from a whole one.

The OCI exporter is unavailable on buildx's default driver, so this path
creates a `docker-container` builder when none exists.

With `--push` the backend supplies its registry values to the same
writer: `IMAGE_TRANSPORT=registry`, `IMAGE_REF` the digest reference of
what was pushed, `IMAGE_TAG_REF` the tag it also carries. A push whose
manifest digest cannot be resolved fails the build rather than handing
on a blank field.

Both paths also write `image.json`, the authoritative identity record
(GitLab CI standard §9.1). `build.env` is the dotenv convenience GitLab
imports, and must agree with it — `verify_image_identity` checks that
before any scan runs.

## Scan-target resolution & tool deletability

See `scripts/lib/scan-common.sh`. Every scan producer bootstraps the same
way (`scan_bootstrap`) and resolves its target through one shared
`resolve_scan_ref` whose override-var precedence is passed in as
arguments — so the lib has zero tool-specific logic. Deleting any one
scan script (e.g. all `trivy-*.sh` if Trivy is banned) never touches the
lib and never affects the other scanners.
