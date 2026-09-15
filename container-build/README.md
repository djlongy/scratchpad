# Container build pipeline

Rebuild one container image from an upstream base, scan it, gate on what the
scan found, and promote the exact digest that passed. The consumer's
`.gitlab-ci.yml` is a single `include:` block and nothing else; the two files
they own are a `Dockerfile` and an `image.env`.

The canonical consumable copy is
[github.com/djlongy/container-build-template](https://github.com/djlongy/container-build-template)
at tag `v1.1.0`. Point your `include:` at that repository. The folder you are
reading is the same tree as a readable reference, so the pipeline, the
templates and the scripts can be read without cloning anything.

## 1. Include it

`examples/consumer-remote/.gitlab-ci.yml` is the whole file. Copy it, replace
every `REPLACE_WITH_` value, commit.

```yaml
include:
  - remote: 'https://raw.githubusercontent.com/djlongy/container-build-template/v1.1.0/pipelines/container.flat.yml'
    inputs:
      instance: nginx
      image-name: nginx

      template-url: 'https://github.com/djlongy/container-build-template.git'
      template-ref: 'v1.1.0'

      upstream-ref: 'docker.io/library/nginx:1.25.3-alpine'
      registry-kind: harbor
      candidate-registry: 'REPLACE_WITH_CANDIDATE_REGISTRY_HOST'
      candidate-repository: 'REPLACE_WITH_CANDIDATE_PROJECT'
      release-registry: 'REPLACE_WITH_RELEASE_REGISTRY_HOST'
      release-repository: 'REPLACE_WITH_RELEASE_PROJECT'

      runner-tags:
        - REPLACE_WITH_PRIVILEGED_RUNNER_TAG
```

`include:remote` fetches one file over plain HTTPS with no authentication, so
the file to include is `pipelines/container.flat.yml`, the generated
single-file form. Never `pipelines/container.yml`: its nested `include:local`
entries get no project context over a remote include and GitLab rejects them.
The raw URL, `template-url` and `template-ref` must all name the same pinned
tag.

`template-url` is how the jobs find `scripts/`. A consumer repository holds a
Dockerfile and an `image.env`, so each shell job shallow-clones the template at
`template-ref` and runs the scripts from that clone against your checkout. The
job image must already carry `git`, because the clone happens before any job
installs anything.

Mirrored the template onto your own GitLab instead? Use
`examples/consumer/.gitlab-ci.yml`, which swaps the remote include for
`include:project` with `file: '/pipelines/container.yml'` and `template-url`
for `template-project`. Those jobs clone with `CI_JOB_TOKEN`, so the template
project's job token allowlist has to name your repository.

## 2. Write the two files you own

```bash
TPL=/path/to/container-build-template

cp "$TPL/Dockerfile.example"         Dockerfile   # edit the FORK EDITS region
cp "$TPL/image.env.example"          image.env    # edit, then COMMIT it
cp "$TPL/inject-certs.sh"            .            # the Dockerfile COPYs these two
cp "$TPL/install-ca-certificates.sh" .
cp "$TPL/.dockerignore"              .
```

`image.env` is the single source of truth for everything except secrets:
hostnames, project paths, layout templates and sourcetypes all live there and
all get committed. `image.env.example` is the slim starter;
`image.env.reference` is the full annotated option set, and stays in the
template for you to copy lines out of.

Do not copy the template's own `.gitignore`. It ignores `image.env`, which in
your repository is committed config.

Only secrets are CI variables. `examples/consumer/README.md` lists which ones
each `registry-kind` and feature flag needs.

## 3. Run the regression suite

From this folder, not from the repository root. Every script derives its own
location from `BASH_SOURCE` and the project root from the working directory, so
the suite runs wherever the tree is checked out.

```bash
cd container-build
bash scripts/test/regression.sh                  # 129 scenarios
bash scripts/test/regression.sh registry-kind    # filter by name substring
```

It needs `bash`, `git`, `jq` and `python3`, reaches no registry and uses no
credentials: `crane` and `docker` are stubbed onto `PATH`. It writes `image.env`
from `scripts/test/image.env.fixture` as it goes and removes it again on exit.

The suite also diffs `pipelines/container.flat.yml` against what
`scripts/ci/flatten.sh` generates, so a template edited without regenerating the
flat form fails. Regenerate with:

```bash
bash scripts/ci/flatten.sh --write
```

Coverage: cert sidecar behaviour and `CA_CERT` materialisation, `RESTORE_USER` /
`ORIGINAL_USER` resolution, the `APPEND_GIT_SHORT` toggle, required-field
validation, argv handling, `REGISTRY_KIND` backend dispatch, `HARBOR_*` and
`ARTIFACTORY_*` independence, `bamboo_*` auto-import, canonical artifact-name
propagation, the scan-target resolution chain, and the silent-fail regression.

## The job graph

```
verify   container-sbom-syft:upstream
         container-scan-xray:{vuln,sbom}-upstream   (xray-mode: advisory)
         container-vuln-grype:db-sync               (ARTIFACTORY_GRYPE_DB_REPO set)
build    container-build
test     consumer smoke tests, if any
scan     container-sbom-syft:built    <- build          MANDATORY GATE
         container-vuln-grype         <- build, sbom    MANDATORY GATE
         container-scan-xray:*-built  <- build          advisory
attest   container-attest-cosign      <- build, sbom, grype   (cosign-enabled)
publish  container-publish            <- build, sbom, grype
         container-evidence-ingest:*  <- build, evidence      (ingest-enabled)
```

Every job name is prefixed with the `instance` input, so two images can share
one pipeline.

Two gates are mandatory and cannot be turned off from the outside: the SBOM of
the built image, and the Grype scan of it. `scan-advisory` records the Grype
verdict without failing, and that is the only way to not gate. An unreadable
report or a crashed scanner never downgrades a gate on its own. Xray is
advisory by design, because a blocking gate has to appear in the publish job's
static `needs:` list and a `needs:` entry naming a job that `xray-mode: off`
never creates fails the whole pipeline.

Candidate and release destinations are separate on purpose. The build job can
reach the candidate registry only. Promotion to the release registry is the
publish job, it is the only job that names release credentials, and it copies by
digest after re-checking the gate evidence against that digest.

Branches build and scan but do not push. Only the default branch and tags push a
candidate and run the ingest stage, so feature branches never post duplicate
events to the evidence sinks.

## What is in this folder

| Path | What it is |
|---|---|
| `pipelines/container.yml` | The composition a consumer includes. Owns workflow, stages, signing and promotion. |
| `pipelines/container.flat.yml` | Generated single-file form of the same thing, for `include:remote`. |
| `templates/` | One component per capability: build, syft SBOM, grype vuln, Xray scan, evidence ingest. |
| `scripts/` | All the logic. `build.sh` orchestrates; `push-backends/`, `scan/`, `ingest/`, `publish/`, `lib/` are swappable parts. |
| `scripts/test/regression.sh` | The suite above. |
| `scripts/ci/flatten.sh` | Generates the flat composition from `container.yml` plus the templates. |
| `runtime/` | The CI execution image. Every tool pinned, so no job downloads anything while it runs. |
| `examples/consumer/`, `examples/consumer-remote/` | The two consumer shapes, complete, plus the CI-variable list. |
| `docs/standard/SKILL.md` | The consumer-facing standard: what you decide, what you supply, what is verified. |
| `docs/standard/REFERENCE.md` | Input list, identity and evidence contracts, local execution, rollback, and what is *not* yet verified. |
| `docs/DESIGN.md` | Why `build.sh` is shaped the way it is. Two roots, reproducible digests, the scan-target chain. |
| `Dockerfile.example` | Copy to `Dockerfile`. Also this repository's own self-test fixture. |
| `image.env.example`, `image.env.reference` | Slim starter, and the full annotated option set. |
| `inject-certs.sh`, `install-ca-certificates.sh` | Vendored verbatim into a consumer repository. The Dockerfile `COPY`s both. |
| `bamboo-specs/bamboo.yml` | The same scripts run inline from Bamboo. Source-validated only, never imported or run. |
| `renovate.json` | Bumps `UPSTREAM_REF` in `image.env` and the pinned `ref:` on the include. |
| `.gitlab-ci.yml` | This tree's own pipeline: shellcheck, the regression suite, a runtime-image build, then the composition pointed at the self-test fixture. |

Read `docs/standard/REFERENCE.md` §9, "Compatibility and limits", before promising
anything to anyone.

## Swappable parts

Pick a push backend with `REGISTRY_KIND` in `image.env`: `harbor` (a plain v2
registry, the default), `artifactory_jcr` (Free tier and self-hosted Free) or
`artifactory_pro` (Pro and JFrog Cloud, slimmer, no python3 dependency).

Pick a scanner per stage, each its own job: Syft, Xray or Trivy for the SBOM,
Grype, Xray or Trivy for vulnerabilities. Every producer writes the canonical
`sbom.cdx.json` and `vuln-scan.json`, so the downstream stages do not care which
one ran. The Trivy scripts are dormant scaffolds pinned to v0.69.3, the last
pre-compromise release, and refuse v0.69.4 through v0.69.6 even if a mirror
serves them.

Per-image work goes in the marked FORK EDITS region of the Dockerfile. There is
no extension surface, no distro selector and no remediate stage.

The image is tagged `<upstream-tag>-<gitShort>`, and the build clock is anchored
to the commit time through `SOURCE_DATE_EPOCH`, so a rebuild of the same commit
is intended to be an idempotent overwrite. Reproducibility across runners and
buildx versions has not been measured, so treat it as a design property rather
than a guarantee.

## Inputs

Of `pipelines/container.yml`, which `pipelines/container.flat.yml` mirrors.

| Input | Default | What it is |
|---|---|---|
| `instance` | **required** | Job-name namespace for this image. Two images in one pipeline use two instances. |
| `image-name` | (empty) | Image name. Empty takes it from `image.env`, then from the upstream image leaf. |
| `template-project` | (empty) | Path of the template project on this GitLab instance. Must match the project this file was included from. Empty is valid only when the consumer already carries `scripts/`. |
| `template-url` | (empty) | Full git clone URL of the template, for a consumer whose instance is not the one hosting it. Set, it is cloned with no token and `template-project` is ignored. |
| `template-ref` | (empty) | Pinned ref of that project. Must match the ref this file was included at. |
| `dockerfile` | `Dockerfile` | Dockerfile path, relative to the build context. |
| `context` | `.` | Build context, relative to the repository root. Holds the Dockerfile, `image.env`, certs and every artifact. |
| `image-env-file` | `image.env` | Configuration file sourced by every script, relative to the build context. |
| `upstream-ref` | (empty) | Full upstream image reference to rebuild from. Empty takes it from `image.env`. |
| `registry-kind` | `harbor` | Push backend for the candidate destination. One of `harbor`, `artifactory_jcr`, `artifactory_pro`. |
| `candidate-registry` | (empty) | Candidate registry. For `harbor` the docker host; for the Artifactory kinds the base URL with scheme. Empty means the build pushes nothing and the image travels on as an OCI archive. |
| `candidate-repository` | (empty) | Candidate repository or project inside that registry. |
| `release-registry` | (empty) | Release registry host or URL. Read by the publish job only. |
| `release-repository` | (empty) | Release repository or project inside that registry. Read by the publish job only. |
| `runtime-image` | `alpine:3.20` | Execution image for the shell jobs. Build one from `runtime/` and publish it, and no job downloads a tool while it runs. |
| `docker-cli-image` | `docker:27-cli@sha256:…` | Docker CLI image for the build and Xray jobs, digest-pinned. |
| `docker-dind-image` | `docker:27-dind@sha256:…` | Docker-in-Docker service image, digest-pinned. Needs a privileged runner. |
| `runner-tags` | (none) | Runner tags for every job in this pipeline. |
| `scan-fail-on-severity` | `critical` | Comma-separated severities that fail the Grype gate. Cumulative, so `critical,high` does not cover a medium. Empty falls back to `critical,high`. |
| `scan-advisory` | `false` | True records the Grype verdict without failing the pipeline. The only way to not gate. |
| `xray-mode` | `off` | `off`, or `advisory` to record Xray verdicts without blocking. Xray cannot be a blocking gate here. |
| `xray-fail-on-severity` | (empty) | Severity recorded as a policy breach in the Xray report. Advisory, so it never fails the pipeline. |
| `jf-binary-url` | (empty) | JFrog CLI binary URL for an air-gapped Xray install. Empty uses the script's default. |
| `evidence-expire-in` | `1 month` | Artifact retention for the identity record, the image archive, the SBOM and the vulnerability report. One value, so the identity never expires before the evidence naming it. |
| `ingest-enabled` | `false` | True ships the SBOM and vulnerability report to the sinks configured in `image.env`. |
| `cosign-enabled` | `false` | True signs the built image and attests its evidence. Needs `COSIGN_KEY` as a file-type CI variable. |
| `cosign-url` | sigstore v2.4.1 release | cosign binary URL. Override for an internal mirror. |
| `build-changes` | `**/*` | Paths that create a pipeline and run its jobs. This list *is* the build context, so every path the Dockerfile reads belongs here. Defaults to everything, so nothing ever ships silently unbuilt. |

## Requirements

- GitLab 17.0 or later, for `include:inputs`.
- A privileged runner: the build and Xray jobs use `docker:dind` over plain TCP
  with TLS off, matching runners that do not mount `/certs/client`.
- The job image must carry `git`, because every shell job clones the template
  before it installs anything. The default `alpine:3.20` does not; build
  `runtime/Dockerfile` and pass it as `runtime-image` instead.
- Outbound access from the runner to the upstream registry, your candidate and
  release registries, and the tool download URLs. For a closed network, every
  one of those is a variable: see the air-gap table in `image.env.reference`.
- Disk on the runner for the whole image uncompressed, in the scan jobs.

## Licence

The canonical repository ships this tree under MIT. The copy here is covered by
the scratchpad's [`LICENSE.md`](../LICENSE.md), which is narrower. Take the MIT
grant from the canonical repository if you want the permissive terms.
