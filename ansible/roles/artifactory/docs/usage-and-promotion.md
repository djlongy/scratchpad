# Using repos + promoting DEV → PROD

Practical recipes for the repo topology this role provisions: per-tenant
`*-dev-local` / `*-prod-local` locals, a `*-docker-dev` virtual that aggregates
the tenant's locals + shared remotes, and JFrog **Projects** as the tenant
boundary.

Set up auth once:

```bash
export AF=https://<tenant>.jfrog.io
export H="Authorization: Bearer <admin-or-ci-token>"
```

## 1. Repo topology (per tenant)

```
acme-docker-dev-local     (env DEV)   ← CI pushes here
acme-docker-prod-local    (env PROD)  ← promoted, released images only
acme-generic-dev-local    (env DEV)
acme-generic-prod-local   (env PROD)
acme-docker-dev (virtual) = acme-docker-dev-local + acme-docker-prod-local
                          + shared-dockerhub-remote + shared-ghcr-remote
shared-*-remote           ← upstream caches, shared by ALL tenants
```

Resolve through the **virtual** (one URL gets you tenant artifacts *and* cached
upstreams); deploy to the **dev local**; promote to the **prod local**.

## 2. Using the repos

**Generic** — deploy / download a file:

```bash
# deploy to DEV (the trailing ;k=v are build properties, optional)
curl -H "$H" -T app-1.0.0.jar \
  "$AF/artifactory/acme-generic-dev-local/acme-app/1.0.0/app-1.0.0.jar;build.name=acme-app;build.number=15"
# download
curl -H "$H" -O "$AF/artifactory/acme-generic-dev-local/acme-app/1.0.0/app-1.0.0.jar"
```

**Docker** — push to the dev local, pull through the virtual:

```bash
docker login <tenant>.jfrog.io                       # token as password
# push to DEV
docker tag myimage:1.0 <tenant>.jfrog.io/acme-docker-dev-local/myimage:1.0
docker push        <tenant>.jfrog.io/acme-docker-dev-local/myimage:1.0
# pull through the virtual (serves tenant repos + cached Docker Hub/GHCR)
docker pull        <tenant>.jfrog.io/acme-docker-dev/library/alpine:3.20
```

## 3. Promotion DEV → PROD — three ways

### 3a. File/artifact promotion (copy or move)

The simplest: copy the exact path from the dev local to the prod local.

```bash
curl -X POST -H "$H" \
  "$AF/artifactory/api/copy/acme-generic-dev-local/acme-app/1.0.0/app-1.0.0.jar\
?to=/acme-generic-prod-local/acme-app/1.0.0/app-1.0.0.jar"
# → "...copying ... completed successfully, 1 artifacts ... copied"
# use /api/move instead of /api/copy to relocate (remove from dev).
```

### 3b. Docker image promotion

Promote an image tag between two docker repos without re-pushing layers:

```bash
curl -X POST -H "$H" -H 'Content-Type: application/json' \
  "$AF/artifactory/api/docker/acme-docker-dev-local/v2/promote" \
  -d '{"targetRepo":"acme-docker-prod-local","dockerRepository":"myimage","tag":"1.0","copy":true}'
# copy:false moves (retags) instead of copying.
```

### 3c. Build promotion (recommended — promotes a whole build with its metadata)

This is the canonical flow: publish **build-info** for the build, then promote the
build — Artifactory moves/copies every artifact the build produced and records a
signed promotion status. **The flow:**

```bash
# (1) deploy artifacts to DEV, tagging them with the build coordinates
curl -H "$H" -T acme-app-1.0.0.jar \
  "$AF/artifactory/acme-generic-dev-local/acme-app/1.0.0/acme-app-1.0.0.jar;build.name=acme-app;build.number=15"

# (2) publish build-info (full metadata: modules, artifacts w/ checksums, agent, url)
curl -X PUT -H "$H" -H 'Content-Type: application/json' "$AF/artifactory/api/build" -d '{
  "version":"1.0.1","name":"acme-app","number":"15",
  "started":"2026-06-11T01:30:00.000+0000",
  "buildAgent":{"name":"gitlab-ci","version":"17.0"},
  "principal":"ci@acme","url":"https://ci.example.com/acme-app/15",
  "modules":[{"id":"com.acme:acme-app:1.0.0","artifacts":[
    {"type":"jar","name":"acme-app-1.0.0.jar","sha1":"<sha1>","sha256":"<sha256>","md5":"<md5>"}
  ]}]
}'

# (3) inspect the published build
curl -H "$H" "$AF/artifactory/api/build/acme-app/15"

# (4) PROMOTE the build DEV → PROD (moves all its artifacts, sets a status)
curl -X POST -H "$H" -H 'Content-Type: application/json' \
  "$AF/artifactory/api/build/promote/acme-app/15" -d '{
    "status":"Released","comment":"QA passed",
    "sourceRepo":"acme-generic-dev-local","targetRepo":"acme-generic-prod-local",
    "copy":true,"artifacts":true,"dependencies":false,"dryRun":false
  }'
# → GET /api/build/acme-app/15 then shows: statuses:[{status:Released, repository:acme-generic-prod-local}]
```

The same thing with the **JFrog CLI** (`jf`), how CI pipelines usually do it:

```bash
jf rt upload "app-1.0.0.jar" "acme-generic-dev-local/acme-app/1.0.0/" \
  --build-name=acme-app --build-number=15
jf rt build-publish acme-app 15
jf rt build-promote  acme-app 15 acme-generic-prod-local \
  --status=Released --copy=true --comment="QA passed"
```

### 3d. Signed promotion across environments (Enterprise+, optional)

For tamper-evident promotion across the project **environments** (DEV → PROD
stages) use **Release Bundles v2** + the Lifecycle API: build the bundle from the
build, then `POST /lifecycle/api/v2/promotion/records/{name}/{version}` to advance
it, each step adding DSSE-signed evidence. (Needs Release Bundles enabled.) The
`copy`/build-promote flow above is the everyday equivalent.

## 4. Multitenancy — how the tenants interact

`examples/multitenant.yml` (and the 4-tenant demo) provision one **Project** per
tenant. Each tenant's repos are prefixed with its project key and scoped to that
project; the **shared remotes are unscoped**, so every tenant's `*-docker-dev`
virtual pulls upstream images through the *same* cached `shared-dockerhub-remote`
— one download benefits all tenants, while each tenant's own artifacts stay
isolated behind its project's RBAC (per-tenant `*-developers` / `*-admins` groups
bound to `Developer` / `Project Admin` project roles).

Apply it with:

```bash
ansible-playbook playbooks/artifactory.yml -e artifactory_url=$AF \
  -e @roles/artifactory/examples/multitenant.yml
```
