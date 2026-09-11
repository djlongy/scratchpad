# publish-wheels-to-gitlab.sh

Uploads every `*.whl` in a directory (default `./packages`) to a GitLab
project's PyPI package registry. Bash 3.2+, curl and python3 only, no twine.
Wheels whose filename is already in the registry are skipped and the script exits 0.

Use case: seed a private GitLab registry with a toolchain's wheel closure so
an ephemeral runner with no pypi.org access can `pip install` from it.

## Fill `./packages`

`requirements.txt` lists the top-level packages. Download the full closure
once per target interpreter, running pip **on** that interpreter (pip
evaluates `python_version` markers against the running python, not against
`--python-version`, so a cross-version download silently drops deps such as
`importlib-metadata; python_version < "3.10"`):

```sh
python3.9  -m pip download -r requirements.txt --only-binary=:all: --dest packages
python3.12 -m pip download -r requirements.txt --only-binary=:all: --dest packages
```

Run that on the same OS/arch as the consumers (`.gitlab-ci.yml` uses
`python:3.9-slim` and `python:3.12-slim` on the x86_64 runner). Add
`--platform`/`--abi` flags only when downloading for a foreign platform.

## Publish, local

```sh
export GITLAB_URL=https://gitlab.example.com
export GITLAB_TOKEN=glpat-...            # PAT / group or project access token, scope api
export GITLAB_PROJECT_ID=123             # or GITLAB_PROJECT_PATH=group/project
DRY_RUN=1 ./publish-wheels-to-gitlab.sh  # list only
./publish-wheels-to-gitlab.sh
PUBLIC_PULL=1 ./publish-wheels-to-gitlab.sh  # also allow anonymous pull (Maintainer token)
```

`WHEEL_DIR=dist` or a positional argument (`./publish-wheels-to-gitlab.sh dist`) changes the scan directory.

## Publish, GitLab CI

`CI_API_V4_URL`, `CI_PROJECT_ID` and `CI_JOB_TOKEN` are auto-detected; no
variables to configure. `.gitlab-ci.yml` runs the script on both
`registry.access.redhat.com/ubi9/ubi` (needs `dnf install python3-pip`; python3
and curl are in the image) and `python:3.12-slim` (needs `apt-get install curl`).

## Consume

```sh
pip install mkdocs --index-url https://gitlab.example.com/api/v4/projects/123/packages/pypi/simple
```

or set `PIP_INDEX_URL` to that URL so nothing falls back to pypi.org.
GitLab forwards unknown package names to pypi.org by default (the simple
index answers 302), which hides a missing wheel; the consumer jobs in
`.gitlab-ci.yml` therefore check every installed dist against the registry.
Anonymous install needs the project's package registry to be public. On a
private project that is the "Allow anyone to pull from Package Registry"
setting (`package_registry_access_level=public`), which `PUBLIC_PULL=1` sets.
Only add `--cert /path/ca.pem` for a private CA. Do not use `--trusted-host`
for a valid certificate.

## Permissions

| Action                                  | CI_JOB_TOKEN | PAT / access token (Maintainer, scope `api`) |
|-----------------------------------------|--------------|----------------------------------------------|
| List packages (skip check)              | yes          | yes                                          |
| Upload wheel to own project's registry  | yes          | yes                                          |
| Upload to another project's registry    | only if that project allows this one under Token Access | yes |
| `PUT /projects/:id` for `PUBLIC_PULL=1` | no (HTTP 401, script fails clearly) | yes                             |
