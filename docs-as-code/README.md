# docs-as-code

Documentation that lives in a git repo as Markdown, is reviewed like code, and is
published by CI. Each project here is a self-contained tutorial with a runnable example.

| Project | What it covers |
|---|---|
| [`gitlab-pages-setup/`](gitlab-pages-setup/) | Enabling Pages on a self-hosted instance and the five ways to a predictable docs URL (`wiki.pages.example.com` with no admin change, custom domain, reverse proxy, ingress), with what each needs and what was tested. |
| [`gitlab-wheel-registry/`](gitlab-wheel-registry/) | Seed a GitLab project's PyPI registry with the docs toolchain wheels (curl-only bash script, job-token upload, per-file skip, anonymous pull), then prove an ephemeral runner with no pypi.org access installs mkdocs on Python 3.9 and Zensical on 3.12 from it. Tested on ubi9 and python-slim images. |
| [`gitlab-wiki-mirror/`](gitlab-wiki-mirror/) | Repo `docs/` mirrored into the project's GitLab wiki by CI (pages, attachments, sidebar from the MkDocs nav) and wiki edits pulled back into the repo as commits by their author, an importer for moving an existing wiki into the repo, and the full pipeline. The no-Pages option. |
| [`mkdocs-on-kubernetes/`](mkdocs-on-kubernetes/) | The docs site as a container on Kubernetes (RKE2): multi-stage Dockerfile onto unprivileged nginx, kaniko build in CI, kustomize manifests, deploy through the GitLab agent. For teams that run clusters rather than Pages. |
| [`mkdocs-material/`](mkdocs-material/) | Material for MkDocs from zero to a published site: theme, fonts, emoji, code blocks, tabs, admonitions, Mermaid diagrams, footer, and deployment to GitHub Pages or GitLab Pages. |
