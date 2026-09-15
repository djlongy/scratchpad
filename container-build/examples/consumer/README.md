# Consumer repository

Two files are yours: `Dockerfile` (from the template's `Dockerfile.example`)
and `image.env` (from `image.env.example`, committed). `.gitlab-ci.yml` beside
this file is the third and only its values change.

Everything else, `scripts/` included, comes from the template at the pinned
ref. The jobs clone it with `CI_JOB_TOKEN`, so add this project to the
template project's CI/CD job token allowlist.

## Which form to copy

**Template on this GitLab instance** — `.gitlab-ci.yml` beside this file.
`include: project` at a pinned ref, `template-project` and `template-ref`
naming that same project and ref, and the job token allowlist above.

**Template anywhere else, GitHub or another GitLab** —
`../consumer-remote/.gitlab-ci.yml`. `include: remote` at the raw URL of
`pipelines/container.flat.yml`, and `template-url` set to a git clone URL of the
same tag instead of `template-project`. The jobs clone that URL with no token, so
the repository has to be readable without credentials and no allowlist applies.
The flat file, not `pipelines/container.yml`: `include: remote` fetches one file
and the nested includes in the unflattened composition cannot resolve over it.
See `docs/standard/REFERENCE.md` §6.

The two cert helpers the Dockerfile COPYs, `install-ca-certificates.sh` and
`inject-certs.sh`, are staged into the build context by the build job from that
same checkout. Commit your own copies to override them; nothing is overwritten.

No `.dockerignore` means the build context is the whole repository, which for
three files costs nothing, so none is generated. Add one when the repo grows.

## CI variables

Registry URLs are inputs, not variables. Only secrets are variables: mark each
masked, and protected so untrusted branches cannot read it.

| Variable | When |
| --- | --- |
| `HARBOR_USER`, `HARBOR_PASSWORD` | `registry-kind: harbor` |
| `ARTIFACTORY_USER`, `ARTIFACTORY_TOKEN` | `registry-kind: artifactory_jcr` or `artifactory_pro` |
| `UPSTREAM_REGISTRY_USER`, `UPSTREAM_REGISTRY_PASSWORD` | the upstream image is private |
| `CBT_RELEASE_REGISTRY_USER`, `CBT_RELEASE_REGISTRY_PASSWORD` | promotion to the release registry; protected only |
| `XRAY_ARTIFACTORY_TOKEN` | `xray-mode: advisory` |
| `SPLUNK_HEC_TOKEN`, `DEPENDENCY_TRACK_API_KEY`, `SBOM_WEBHOOK_AUTH_HEADER` | `ingest-enabled: "true"` |
| `COSIGN_KEY` (file type) | `cosign-enabled: "true"` |
| `SONAR_HOST_URL`, `SONAR_TOKEN` | not used here; template-only |
