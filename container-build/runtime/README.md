# CI execution image

Every tool this template's pipeline uses, at the versions it already pins: bash,
git, curl, openssl, jq, python3, ca-certificates, docker CLI 27.5.1 with buildx
v0.20.1, crane 0.20.2, syft 1.45.1, grype 0.114.0. Jobs running it download
nothing. It is optional: leave `RUNTIME_IMAGE` alone in `.gitlab-ci.yml` and the
pinned-installer path still works. No credentials, no `image.env` and no template
scripts are baked in. linux/amd64 only, uid 1000.

## Build

    docker build -f runtime/Dockerfile -t cbt-runtime:dev runtime/

The context is `runtime/` alone, so nothing else in the repository can reach a
layer. Both bases are pinned by digest, bumped by Renovate.

## Run the template's scripts locally

Mount the repository at `/work`, the image's working directory:

    docker run --rm -v "$PWD":/work cbt-runtime:dev bash scripts/test/regression.sh

A real build needs a docker daemon. Over TCP (`-e DOCKER_HOST=tcp://docker:2375`)
that is the whole story. Over the socket, uid 1000 also needs the socket's group:

    docker run --rm -v "$PWD":/work -v /var/run/docker.sock:/var/run/docker.sock \
      --group-add "$(stat -c %g /var/run/docker.sock)" cbt-runtime:dev bash scripts/build.sh

## Publish

Any registry. Tag it with the template version it belongs to, push, then set
`RUNTIME_IMAGE` to that reference as a project or group CI variable:

    docker tag cbt-runtime:dev registry.example.com/ci/cbt-runtime:1.0.0
    docker push registry.example.com/ci/cbt-runtime:1.0.0

GitLab's docker executor clones as root. If a job in this image cannot write its
build directory, set `FF_DISABLE_UMASK_FOR_DOCKER_EXECUTOR: "true"` in the job.
