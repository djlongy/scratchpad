# ─── Execution image for this template's CI jobs ────────────────────
# One versioned image holding every tool the pipeline needs, so no job
# downloads an installer while it runs. Owned and published by whoever
# forks this template — see runtime/README.md. Nothing here is required:
# leave RUNTIME_IMAGE unset in .gitlab-ci.yml and the pinned-installer
# path still works.
#
# Not in this image, on purpose: no credentials, no image.env, no
# template scripts. The repository is mounted at run time.
#
# Limitation: linux/amd64 only. Every pinned URL below is x86_64 and the
# runners are too. Add a TARGETARCH case when an arm64 runner appears.
# ───────────────────────────────────────────────────────────────────

# Image digests were read with `docker buildx imagetools inspect <tag>`
# on 2026-09-15; both are multi-arch index digests. Renovate bumps them
# (customManagers entry in renovate.json).

# docker CLI 27.5.1 + buildx v0.20.1, taken from the same image the build
# job runs as DOCKER_CLI_IMAGE so the client version is identical there
# and here.
FROM --platform=linux/amd64 docker.io/library/docker:27-cli@sha256:851f91d241214e7c6db86513b270d58776379aacc5eb9c4a87e5b47115e3065c AS dockercli

FROM --platform=linux/amd64 docker.io/library/alpine:3.20@sha256:d9e853e87e55526f6b2917df91a2115c36dd7c696a35be12163d44e6e2a4b6bc

# The package set .install-tools apk-adds in .gitlab-ci.yml: bash and git
# for build.sh, curl and openssl for the scan helpers, jq for image.json,
# python3 for scripts/push-backends/artifactory.sh (build-info-merge.py).
# ca-certificates so curl and crane trust public registries.
RUN apk add --no-cache bash git curl jq openssl python3 ca-certificates

COPY --from=dockercli /usr/local/bin/docker /usr/local/bin/docker
COPY --from=dockercli /usr/local/libexec/docker/cli-plugins/docker-buildx \
                      /usr/local/libexec/docker/cli-plugins/docker-buildx

# Versions are the pins the repository already enforces: CRANE_URL in
# .gitlab-ci.yml, SYFT_VERSION in scripts/scan/syft-sbom.sh, GRYPE_VERSION
# in scripts/scan/grype-vuln.sh. Keep them in step — the scan scripts
# compare `syft version` against their pin and reinstall on a mismatch,
# so drift here costs a download rather than a wrong result.
#
# Each SHA256 is the line for that file in the upstream release's
# checksums.txt.
ARG CRANE_VERSION=0.20.2
ARG CRANE_SHA256=c14340087103ba9dadf61d45acd20675490fd0ccbd56ac7901fc1b502137f44b
ARG SYFT_VERSION=1.45.1
ARG SYFT_SHA256=20c84195e24927f50a3b2269946be51f4c4abc9d2f145fee7388b4199149f716
ARG GRYPE_VERSION=0.114.0
ARG GRYPE_SHA256=edda0968d8827daab01d32b3cd7de192ae0915005e7bbfcfef9e68e79bc43343
RUN set -eux; \
    cd /tmp; \
    curl -fsSL -o crane.tgz "https://github.com/google/go-containerregistry/releases/download/v${CRANE_VERSION}/go-containerregistry_Linux_x86_64.tar.gz"; \
    echo "${CRANE_SHA256}  crane.tgz" | sha256sum -c -; \
    tar xzf crane.tgz -C /usr/local/bin crane; \
    curl -fsSL -o syft.tgz "https://github.com/anchore/syft/releases/download/v${SYFT_VERSION}/syft_${SYFT_VERSION}_linux_amd64.tar.gz"; \
    echo "${SYFT_SHA256}  syft.tgz" | sha256sum -c -; \
    tar xzf syft.tgz -C /usr/local/bin syft; \
    curl -fsSL -o grype.tgz "https://github.com/anchore/grype/releases/download/v${GRYPE_VERSION}/grype_${GRYPE_VERSION}_linux_amd64.tar.gz"; \
    echo "${GRYPE_SHA256}  grype.tgz" | sha256sum -c -; \
    tar xzf grype.tgz -C /usr/local/bin grype; \
    rm -f /tmp/*.tgz
# Nothing here runs the binaries it just installed. The checksums above
# are the integrity check, and running an amd64 Go binary under an
# emulator on an arm64 workstation fails for reasons that have nothing to
# do with the image. Check the versions after the build instead:
#   docker run --rm cbt-runtime:dev sh -c 'syft version; grype version'

# Non-root. Nothing in the image is writable by this user. A job that
# talks to a docker daemon over the socket needs the socket's group:
#   docker run --group-add "$(stat -c %g /var/run/docker.sock)" ...
# Over TCP (DOCKER_HOST=tcp://docker:2375, how the CI build job works)
# no group is needed at all. runtime/README.md has both.
RUN addgroup -g 1000 ci && adduser -u 1000 -G ci -D -h /home/ci ci
USER ci
WORKDIR /work
