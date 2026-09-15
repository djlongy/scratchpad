# Container build standard

One image, one consumer repository, one include. The template owns every script,
job and gate. The consumer owns what is specific to its image and nothing else.

## Two-tier loading

This file is the summary: enough to set a consumer up and to know what the
pipeline will and will not do. `REFERENCE.md` beside it is the detail:
build inputs and precedence, the identity and evidence contracts, local execution,
rollback, and the list of untested behaviour. Read a section on demand.

Job names, stage vocabulary, `needs` wiring and input naming come from the
`gitlab-ci-standard` skill. They are not repeated here.

## What the consumer decides and supplies

Three files, committed:

| File | Source | Notes |
|---|---|---|
| `Dockerfile` | `Dockerfile.example` | Edit only the FORK EDITS region |
| `image.env` | `image.env.example` | Non-secret config. Commit it |
| `.gitlab-ci.yml` | `examples/consumer/.gitlab-ci.yml` | One include, values edited |

`install-ca-certificates.sh` and `inject-certs.sh`, which the Dockerfile COPYs,
are staged into the build context by the build job; commit your own copies only
to override them. `.dockerignore` is optional, and so is a `.gitignore` that
excludes the build outputs. `examples/consumer/README.md` lists the files and the
variables together.

Three decisions the consumer makes and expresses as inputs:

1. **The upstream base.** `UPSTREAM_REF` in `image.env`, one full image URL that
   Renovate can bump, or the `upstream-ref` input when it must come from CI.
2. **The destinations.** `candidate-registry` and `candidate-repository` are where
   the build pushes. `release-registry` and `release-repository` are where the
   publish job promotes. Leave the candidate empty and the build exports an OCI
   archive instead, which is a valid end state for a repository that only gates.
3. **The gate.** `scan-fail-on-severity` names every severity that fails, and the
   list is cumulative: `critical,high` does not cover a medium. The default is
   `critical`; high findings are reported and remediated on their own schedule.

Secrets are masked CI variables and never enter `image.env`. Release credentials
(`CBT_RELEASE_REGISTRY_USER` and `CBT_RELEASE_REGISTRY_PASSWORD`) are also
protected, because only the publish job may read them.

## What the pipeline verifies

Two gates are mandatory and neither can be turned off by an input:

- **`<instance>:container-sbom-syft:built`** produces the CycloneDX SBOM for the
  image this pipeline built, and writes `subject.json` naming the digest it read.
- **`<instance>:container-vuln-grype`** evaluates the policy and writes
  `scan-result.json` with its own subject and verdict.

A scan that cannot verify what it scanned fails. It never steps back to the
upstream image, because that is a different image. `scan-advisory: "true"` records
a verdict without failing, and is the only way not to gate.

Promotion is a copy of bytes, by digest. `scripts/publish/promote.sh` re-reads the
gate evidence against the candidate digest, copies with crane, and reads the
destination back. There is no rebuild on the release path, and none on the
rollback path either.

## Before adopting

- The template project must list the consumer in its **CI/CD job token allowlist**,
  or every job's clone gets a 403.
- `template-ref` must name the same revision as the include's `ref`.
- Minimum consumer is GitLab 17.0. Components are not used: they cannot be
  referenced across instances.
- Read `REFERENCE.md` §9 before promising anything. Signing, Bamboo runtime,
  multi-architecture builds and air-gapped operation are not verified.
