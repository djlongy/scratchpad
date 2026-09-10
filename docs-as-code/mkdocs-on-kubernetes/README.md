# MkDocs on Kubernetes (RKE2)

The third publishing option: the docs site as a container, built by CI and rolled out to a
Kubernetes cluster. No GitLab Pages, no Python on a runner, no pip on a host; the runtime
image is nginx serving static files.

Companion to the [Material for MkDocs tutorial](../mkdocs-material/) (what goes in
`docs/` and `mkdocs.yml`) and the [wiki mirror](../gitlab-wiki-mirror/) (the lint jobs and
the wiki job, which stay as they are). This folder adds the build and deploy half.

| File | Purpose |
|---|---|
| [`Dockerfile`](Dockerfile) | Two stages: `squidfunk/mkdocs-material` builds the site, `nginxinc/nginx-unprivileged` serves it |
| [`nginx.conf`](nginx.conf) | Static site config: clean URLs, the MkDocs 404 page, cache headers, `/healthz` |
| [`k8s/`](k8s/) | kustomize: Deployment (non-root, read-only fs, probes, limits), Service, Ingress |
| [`.gitlab-ci.yml`](.gitlab-ci.yml) | `image-build` with kaniko, `deploy` with kubectl through the GitLab agent |

## Why this shape

- **kaniko**, because a Kubernetes-executor runner has no Docker socket and should not get
  one. It builds the image inside the job pod and pushes to the project's registry with the
  job-scoped registry credentials GitLab already provides.
- **The Material image as the build stage**, not `python:3.12` plus pip. It carries MkDocs and
  the theme at a pinned major; `requirements.txt` adds only the plugins the site uses.
- **nginx-unprivileged** as the runtime: listens on 8080 as uid 101, so the pod can run with
  `runAsNonRoot`, `readOnlyRootFilesystem`, and all capabilities dropped. The image contains
  the built HTML and nothing else.
- **kustomize, not Helm**, for three manifests. The only thing that changes per deploy is the
  image tag, which the deploy job substitutes.
- **The GitLab agent** for cluster access: `kubectl config use-context <project>:<agent>`
  inside the job, no kubeconfig stored as a CI variable. On Rancher-managed RKE2 the agent
  is a Helm install in the cluster and a registration in the project.

## Repo layout

Same as the wiki mirror, plus this folder's files at the root:

```text
docs/                    source pages
mkdocs.yml
requirements.txt         mkdocs-awesome-pages-plugin>=2.9,<3  (and pyyaml for the wiki job)
Dockerfile
nginx.conf
k8s/
  kustomization.yaml
  deployment.yaml
  service.yaml
  ingress.yaml
.gitlab-ci.yml           lint jobs + wiki job + image-build + deploy
```

`requirements.txt` must not pin `mkdocs-material` here: the build stage already has it, and
a conflicting pin makes pip replace it. Pin only the plugins.

## Setup

1. Copy `Dockerfile`, `nginx.conf`, `k8s/` into the docs repo. Merge the two jobs from
   [`.gitlab-ci.yml`](.gitlab-ci.yml) into the pipeline and remove `pages`.
2. In `k8s/kustomization.yaml` set `newName` to the project's registry path
   (`CI_REGISTRY_IMAGE`, e.g. `registry.example.com/group/docs`) and the namespace; in
   `k8s/ingress.yaml` set the host, and TLS if the cluster has cert-manager.
3. Register a GitLab agent for the cluster (*Operate > Kubernetes clusters > Connect a
   cluster*), install it with the Helm command GitLab prints, and give it `ci_access` to
   this project in its `config.yaml`. Set the CI variable `KUBE_CONTEXT` to
   `<group>/<project>:<agent-name>`.
4. Create the namespace once (`kubectl create ns docs`) or add a Namespace manifest.
5. Merge to the default branch. `image-build` pushes `<registry>/<project>:<sha>` and
   `:latest`; `deploy` applies the manifests with the sha tag and waits for the rollout.

Without `KUBE_CONTEXT` the deploy job is skipped by its rule; the image still builds.

## Local check

```bash
cp -r ../gitlab-wiki-mirror/example/docs ../gitlab-wiki-mirror/example/mkdocs.yml .   # any docs tree
docker build -t docs:local .
docker run --rm -p 8080:8080 docs:local
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8080/operations/   # 200
kubectl kustomize k8s/ | kubeconform -strict -summary -
```

Verified here against the wiki-mirror example: image builds (`mkdocs build --strict` inside
the build stage), the page and `/healthz` return 200, the runtime container runs as uid 101,
and the rendered manifests pass kubeconform and a server-side dry run (admission policies included)
on a Kubernetes cluster with the nginx ingress class, the same class RKE2 ships.

## Alternatives, and when

- **Pages** (the tutorial's default): if the instance has it, it is the least to run.
- **Wiki mirror only**: nothing to host, readers use the Wiki tab; you lose search-as-you-type
  and the theme.
- **This folder**: an internal site with its own hostname, HTTPS, and access control from the
  ingress, on infrastructure the team already operates.
