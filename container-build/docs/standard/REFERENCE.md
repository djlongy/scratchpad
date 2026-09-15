# Container build standard: reference

The detail behind `SKILL.md`. Job names, stage vocabulary, `needs` wiring and
input naming come from the `gitlab-ci-standard` skill and are not repeated here.

Every statement below describes what the code in this repository does at
`pipelines/container.yml`, `templates/container-build/`, `scripts/` and
`runtime/`. Section 9 lists what has not been run.

## 1. Build inputs and precedence

`pipelines/container.yml` declares the whole input surface. Unknown inputs are
rejected by GitLab, and a duplicate input key across included files produces
`Duplicate input keys found`, so the surface is unique and complete by
construction.

| Group | Inputs |
|---|---|
| Identity | `instance` (required, `^[a-z][a-z0-9-]{0,47}$`), `image-name` |
| Template source | `template-project`, `template-ref` |
| What is built | `dockerfile`, `context`, `image-env-file`, `upstream-ref` |
| Destinations | `registry-kind`, `candidate-registry`, `candidate-repository`, `release-registry`, `release-repository` |
| Execution | `runtime-image`, `docker-cli-image`, `docker-dind-image`, `runner-tags` |
| Gates and evidence | `scan-fail-on-severity`, `scan-advisory`, `xray-mode`, `xray-fail-on-severity`, `jf-binary-url`, `evidence-expire-in`, `ingest-enabled`, `cosign-enabled`, `cosign-url` |
| Source eligibility | `build-changes` |

Inputs do not flow through nested includes, so the composition forwards every one
of them into the templates explicitly. A template that needs a value it was not
forwarded does not see it.

**Precedence.** The composition turns an input into a job variable, and
`scripts/lib/load-image-env.sh` gives a non-empty shell value priority over
`image.env`. An empty shell value does not, so a stray `VAR=` in the runner
environment cannot clobber a committed value. The order is therefore:

1. A non-empty input or CI variable.
2. `image.env` in the build context.
3. The script's own default.

Two inputs document their own fallthrough. Empty `image-name` takes the name from
`image.env`, then from the upstream image's leaf segment. Empty `upstream-ref`
takes `UPSTREAM_REF` from `image.env`, which is the form Renovate bumps.

Registry URLs are inputs, never `image.env`, on the composition path. The build job
maps `candidate-registry` and `candidate-repository` onto `HARBOR_REGISTRY` and
`HARBOR_PROJECT`, or onto `ARTIFACTORY_URL` and `ARTIFACTORY_TEAM`, according to
`registry-kind`. For the Artifactory kinds `candidate-registry` is therefore the
Artifactory base URL with its scheme (`https://artifactory.example.com`), not the
docker push host, which stays `ARTIFACTORY_PUSH_HOST` in `image.env`.
`release-registry` is always the docker host the release reference starts with.
Secrets are masked CI variables and appear in no input.

**Scan target resolution** (`scripts/lib/scan-common.sh`): a positional argument,
then each named override variable in order, then the image this pipeline built as
read from its identity record, then `UPSTREAM_REF`. `SCAN_SUBJECT=built` refuses
both an explicit reference and the upstream fallback, so a post-build scan either
verifies the image that was built or fails.

## 2. The Dockerfile recipe

`Dockerfile.example` has four parts and the consumer edits one of them.

1. Global `ARG`s. `build.sh` passes them from `image.env`. `UPSTREAM_REF` is the
   base as one full URL carrying the digest that `build.sh` resolved, so the
   `FROM`, the base inspection, the user lookup and the OCI labels all name the
   same bytes.
2. `FROM ${UPSTREAM_REF} AS base`.
3. A cert sidecar in a separate, shell-bearing image, so cert preparation works on
   distroless, scratch and Chainguard bases. It COPYs `install-ca-certificates.sh`,
   `inject-certs.sh` and `certs/` from the build context. **The build job stages
   both scripts from the template checkout when the context does not already carry
   them**, and never overwrites a consumer's own copy. `build.sh` creates `certs/`.
   So a consumer repository holds three files: `Dockerfile`, `image.env` and
   `.gitlab-ci.yml`. With no `.dockerignore` the context is the whole repository,
   which at that size costs nothing, so none is generated.
4. `FROM base AS final`, which re-bases so the image keeps the upstream's user,
   then copies the prepared trust files, then the FORK EDITS region.

The trust-store replacement is unconditional. An empty `certs/` still replaces the
upstream's OpenSSL bundle with the cert builder's. That is equivalent for most
images and is not equivalent for one whose trust store was curated or
FIPS-restricted. Only the OpenSSL-style bundle is touched: a Java `cacerts`
keystore, Node's compiled-in roots and Python's `certifi` are left alone.

`build.sh` creates `certs/` and writes `CA_CERT` into `certs/ci-injected.crt` when
that variable is set, so the directory does not have to exist in git.

### RESTORE_USER

The template emits no `USER` line anywhere. A `RUN` in the edit region that needs
write permission enters root and has to come back out, and `RESTORE_USER=true` in
`image.env` is the single knob that makes `build.sh` resolve `ORIGINAL_USER` for
the `USER ${ORIGINAL_USER}` line at the end of the region.

| Situation | Result |
|---|---|
| `ORIGINAL_USER` set in `image.env` or the environment | used verbatim, no lookup |
| `crane config` succeeds and the config names a user | that user |
| `crane config` succeeds and the config names none | `root`, and the build prints which case it took |
| `crane config` fails, or crane is missing | the build stops |

The last row is the point of the knob. An image that entered root must never stay
there because a lookup quietly failed. A `--dry-run` warns instead of stopping,
because it produces no image. Leaving `RESTORE_USER` unset skips the lookup.
Setting `ORIGINAL_USER` without it does nothing, and says so.

## 3. Output identity

`scripts/lib/image-identity.sh` is the only writer. `image.json` is authoritative
and `build.env` is the dotenv copy GitLab imports. The two must agree, and
`verify_image_identity` fails the consuming job when they do not. Only a manifest
digest is ever written into a digest field. A local config or image ID is not one.

`image.json`, schema version 1:

```
schema_version  repository  digest  reference  tag  tag_reference
transport       subject_kind  platforms  source_commit  created_at
upstream_ref    upstream_digest
archive, archive_sha256     only when transport is oci-archive
pipeline_id, job_id         only when the producer had them
```

`build.env`:

```
IMAGE_REF  IMAGE_TAG_REF  IMAGE_TAG  IMAGE_DIGEST  IMAGE_NAME
IMAGE_REPOSITORY  IMAGE_TRANSPORT  IMAGE_ARCHIVE  IMAGE_ARCHIVE_SHA256
IMAGE_SUBJECT_KIND  IMAGE_PLATFORMS  IMAGE_JSON
UPSTREAM_TAG  UPSTREAM_REF  BASE_DIGEST  GIT_SHA  CREATED
SBOM_FILE  VULN_SCAN_FILE
```

`transport` is `registry` or `oci-archive`. On the registry path `IMAGE_REF` must
carry `IMAGE_DIGEST` or the write is refused. On the archive path the archive must
exist and be non-empty. A build whose `IMAGE_REF` equals `UPSTREAM_REF` is refused
outright, because that would hand downstream jobs the image it rebuilt from.

## 4. Evidence files

`scripts/lib/artifact-names.sh` roots every artifact per image, under the build
context:

```
artifacts/<image-name>/            the image this pipeline built
artifacts/<image-name>-upstream/   a prescan, which is about a different image
```

Two images in one pipeline get two roots and neither overwrites the other. A
filename containing a slash is a path the operator chose and is left alone.

| File | Writer | Contents |
|---|---|---|
| `sbom.cdx.json` | `scripts/scan/syft-sbom.sh` | CycloneDX SBOM. Canonical name, so the downstream stages do not know which producer ran |
| `vuln-scan.json` | `scripts/scan/grype-vuln.sh` | The raw vulnerability report |
| `subject.json` | `scan_write_subject` | The identity the scan actually ran against, written beside the evidence |
| `scan-result.json` | `scan_gate` | Tool and database version, the policy evaluated, counts by severity, the verdict, and the subject |
| `release.json` | `scripts/publish/promote.sh` | Source and destination digests, the evidence consulted and the gate outcomes |

`subject.json` exists so a consumer compares the subject the producer recorded, not
a name in the SBOM. `evidence-expire-in` sets one retention for the identity record,
the archive, the SBOM and the report together, so the identity never expires before
the evidence naming it.

## 5. Local execution

`runtime/Dockerfile` builds the execution image: bash, git, curl, openssl, jq,
python3, ca-certificates, docker CLI 27.5.1 with buildx 0.20.1, crane 0.20.2,
syft 1.45.1, grype 0.114.0, at the versions the pipeline already pins. Both bases
are digest-pinned. No credentials, no `image.env` and no template scripts are baked
in. linux/amd64 only, uid 1000.

```bash
docker build -f runtime/Dockerfile -t cbt-runtime:dev runtime/
```

The context is `runtime/` alone, so nothing else in the repository can reach a layer.

Mount the repository at `/work`, which is the image's working directory:

```bash
docker run --rm -v "$PWD":/work cbt-runtime:dev bash scripts/test/regression.sh
```

A real build needs a docker daemon. Over TCP that is the whole story. Over the
socket, uid 1000 also needs the socket's group:

```bash
docker run --rm -v "$PWD":/work \
  -v /var/run/docker.sock:/var/run/docker.sock \
  --group-add "$(stat -c %g /var/run/docker.sock)" \
  cbt-runtime:dev bash scripts/build.sh
```

Without the runtime image the pinned-installer path still works: every download is
an overridable variable and the jobs install what they need. That path is slower and
needs outbound access.

Running the scripts directly, in order, needs no manual `source` step. Each scan and
ingest script self-sources `./build.env`, so it always targets the latest build:

```bash
bash scripts/build.sh --push
bash scripts/scan/syft-sbom.sh
bash scripts/scan/grype-vuln.sh
```

## 6. The include mechanism

```yaml
include:
  - project: 'example-group/container-build-template'
    ref: 'REPLACE_WITH_PINNED_TEMPLATE_REF'
    file: '/pipelines/container.yml'
    inputs:
      instance: nginx
      template-project: 'example-group/container-build-template'
      template-ref: 'REPLACE_WITH_PINNED_TEMPLATE_REF'
```

CI/CD inputs became generally available in GitLab 17.0, which is the minimum
supported consumer. CI/CD components reached general availability in the same
release but can only be referenced within one GitLab instance, so a component is
ruled out by portability and a pinned `include: project` is the form that travels.

### Two consumption forms

**Same instance, job token.** `include: project` at a pinned `ref`, with
`template-project` and `template-ref` naming that same project and ref. The jobs
clone the template with `CI_JOB_TOKEN`, so the template project's job token
allowlist has to name the consumer. `examples/consumer/.gitlab-ci.yml`.

**Anywhere else, clone URL.** `include: remote` pointing at
`pipelines/container.flat.yml` at a pinned tag, with `template-url` set to a git
clone URL of the same tag and `template-ref` to that tag. The jobs clone that URL
with no token, so the repository has to be readable without credentials and no
allowlist is involved. `examples/consumer-remote/.gitlab-ci.yml`.

`template-url` wins over `template-project` when both are set. A URL can carry
userinfo, so the clone block masks it before logging, the same way
`scripts/scan/grype-vuln.sh` masks the Grype database URL.

`include: inputs` is a global keyword with one history: beta as `with:` in GitLab
15.11, renamed `inputs:` in 16.0, generally available in 17.0. It is not gated per
include type, so `include: remote` has accepted `inputs` since 17.0 with no
separate floor — https://docs.gitlab.com/ci/yaml/#includeinputs and
https://docs.gitlab.com/ci/inputs/. `include: remote` itself takes no
authentication and the URL must end in `.yml` or `.yaml` —
https://docs.gitlab.com/ci/yaml/#includeremote.

### Why the remote form includes a different file

`include: remote` is one HTTP GET. Its nested includes run "without context as a
public user" (https://docs.gitlab.com/ci/yaml/#includeremote), so a nested
`include: local:` inside a remotely fetched file has no project to resolve
against and GitLab fails the pipeline. `pipelines/container.yml` composes itself
from five `include: local` entries, so it cannot be consumed remotely as it
stands.

`pipelines/container.flat.yml` is that composition with the five templates
inlined, generated by `scripts/ci/flatten.sh` from the same sources. It declares
the identical `spec: inputs` and the identical jobs, and it has no nested
includes at all, so the restriction cannot apply to it. `scripts/test/regression.sh`
regenerates it and diffs, so a template edited without regenerating fails the
suite, and this repository's own `.gitlab-ci.yml` includes the flat file rather
than its source, so CI proves the form a remote consumer actually gets.

The alternative was making the nested includes `include: remote` URLs built from
an input. Inputs do interpolate inside include values
(https://docs.gitlab.com/ci/inputs/examples/), so that is supported, but it needs
every nested include duplicated behind `include: rules` to keep the private
same-instance path working, and it would force the template repository to be
publicly fetchable for every consumer. A generated single file is smaller and
cannot half-resolve.

The consumer repository owns no `scripts/`. Every job therefore locates them at run
time: if `scripts/build.sh` is already in the checkout it is used, which is how this
template builds itself. Otherwise the job shallow-clones at `template-ref` into
`.cbt-template`: from `template-url` with no token when that input is set,
otherwise from `template-project` on this instance using `CI_JOB_TOKEN`.
`build.sh` derives its own root from `BASH_SOURCE` and the project root from the
working directory, so the clone drives the consumer's files with nothing copied.

Two consequences, both hard requirements on the job-token path.

- **Job token allowlist.** The template project must list the consumer under
  Settings, CI/CD, Token Access, or the clone returns 403 and every job fails.
  By API: `POST /projects/<template>/job_token_scope/allowlist` with
  `target_project_id`. The allowlist is only enforced while the template project
  also has "Limit access to this project" enabled on that same page
  (`ci_inbound_job_token_scope_enabled` on the project API). With it off the
  allowlist is not consulted and any project's job token can clone the template.
- **`template-ref` equals `ref`.** Otherwise the jobs run scripts from a different
  revision than the pipeline was written against.

`build-changes` is the build context expressed as a path list, and it is the only
such list: the workflow gate and every job rule read it, so a commit either creates
a pipeline whose jobs run or creates no pipeline at all. There is no second list to
disagree with it and no job-less pipeline for GitLab to mark failed. Every path the
Dockerfile reads belongs in it, plus `.gitlab-ci.yml` if editing the pipeline itself
should rebuild. The default is `**/*`.

## 7. Bamboo

`bamboo-specs/bamboo.yml` is an Atlassian YAML Specs v2 plan, `EXAMPLE-CBT`, running
the same build and scan scripts inline on an agent that already has docker, so it
needs no Docker-in-Docker and no template indirection. Grype runs in a stage after
its Syft producer, because Bamboo has no DAG and stage order is the only ordering
guarantee. Artifacts are declared `shared: true` with no expiry, so Bamboo's
server-side retention applies rather than anything this repository sets.

Bamboo has no QA stage. Shellcheck, the regression suite and SonarQube run on GitLab
only. It has no equivalent of the composition, the job-token clone, the gates'
`needs` wiring or the promotion job.

**This is source validation only.** No Bamboo server was queried and the plan has not
been imported or run. Runtime acceptance is outstanding.

## 8. Rollback

Rollback re-promotes a digest that was already verified. It never rebuilds.

1. Find the release record of the last good run: `artifacts/<image>/release.json`
   names the source and destination digests and the evidence consulted.
2. Re-run the publish job of that pipeline. Its artifacts still carry `image.json`,
   `subject.json` and `scan-result.json`, and `promote.sh` re-verifies them against
   the candidate digest before copying. Set `RELEASE_TAG` when the destination tag
   differs from the candidate's.
3. That pipeline ran at its own `template-ref`, so re-running it uses the previous
   template revision with no further action.

The publish job's `resource_group` serialises promotions but does not order them, so
the consumer project should also set that group's process mode to `oldest_first`:
`PUT /projects/<id>/resource_groups/<instance>:container-publish` with
`process_mode=oldest_first`. `promote.sh` refuses a branch pipeline whose commit is no
longer the branch head (read with git through `CI_REPOSITORY_URL`, which needs no
API allowlist), so a stale retry cannot move the release tag backwards either
way; the process mode is what stops the queue running them out of order in the first
place.

If the evidence artifacts have expired, there is no verified rollback through this
pipeline. Copy the digest by hand with crane and record that the gates were not
re-checked:

```bash
crane copy <candidate>@sha256:<digest> <release-registry>/<repo>:<tag>
```

A rebuild is not a rollback. A rebuild at the same commit is reproducible only when
git is present, because `SOURCE_DATE_EPOCH` comes from the commit time, and it is
still a different act from releasing the bytes that passed the gates.

## 9. Compatibility and limits

Verified on this branch: the one-include consumer path against GitLab 18.9.1-ee,
the job-token clone, the upstream Syft prescan, the OCI-archive build, and the
built-image Syft and Grype gates. Everything below is not.

- **The runtime image is unpublished.** `runtime/Dockerfile` builds and its contents
  are pinned, but no registry destination is decided, so `runtime-image` has no
  verified value. Consumers fall back to the pinned-installer path.
- **The default `runtime-image` installs its tools on every run.** `alpine:3.20`
  carries none of them, so each shell job — build, the two gates, ingest, attest and
  publish alike — runs the same `apk add bash git curl openssl jq python3` before the
  job-token clone. The clone works on the default image; the cost is one package
  install per job, which a runtime image that already ships those six skips.
- **Promotion has not been run end to end.** `promote.sh` is written and reviewed,
  and no pipeline in this branch has copied a candidate to a release registry,
  because no registry destination is provisioned.
- **Signing is dormant.** No signer identity exists. `cosign-enabled` creates the
  job but the gate is not a release precondition, and whether signing is mandatory
  is undecided.
- **Multi-architecture is untested.** `IMAGE_PLATFORMS` is carried through the
  identity record, the runtime image is linux/amd64 only, and no multi-platform
  build has been exercised.
- **The runtime tooling is linux/amd64 only.** `runtime/Dockerfile` builds for that
  platform, and the crane, cosign and JFrog CLI download URLs all name Linux x86_64
  binaries, so a job on an arm64 runner installs a binary it cannot execute.
- **The tool guard is Alpine-only.** The install step in every job is `apk add`, so a
  runtime image on a Debian or UBI base must already ship all six tools. The guard
  skips the install when they are all present, and fails with `apk: not found` when
  any one of them is missing.
- **Air-gapped operation is untested.** Every download is an overridable variable,
  and no run has been made with outbound access disabled. Do not claim offline mode.
- **Registry backends.** Only the no-candidate OCI-archive path has run. `harbor`,
  `artifactory_jcr` and `artifactory_pro` have not been exercised against a live
  registry on this branch. JCR's flat-manifest limitation stays scoped to its
  compatibility mode.
- **Xray, Trivy and the evidence sinks are off.** `xray-mode: advisory`, the Trivy
  scaffolds and `ingest-enabled: "true"` have not been run here.
- **The remote consumption path has not been run.** `template-url` and
  `pipelines/container.flat.yml` are written, CI lints the flat file and the
  regression suite proves it is in sync, but no pipeline has been created from an
  `include: remote` of a public copy, and no job has cloned a template over
  `template-url`. The GitLab version floor is a documentation claim like the rest
  of §6.
- **GitLab 17.0 is a documentation claim.** The only instance exercised is 18.9.1-ee.
- **Consumers have not been inventoried.** Nothing about the legacy inline
  `.gitlab-ci.yml` is deprecated yet, and it remains this repository's own pipeline.
