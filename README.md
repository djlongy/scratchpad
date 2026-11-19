# Scratchpad

A reference kit for sysadmin / DevOps work. Quick references, runnable
snippets, and small self-contained projects — organised so things can
be found in their relevant places and dropped into real deployments
without surgery.

Each top-level folder is a category. Each project inside has its own
README explaining what it does, what it requires, and how to plug it in.

## Categories

| Folder | What's in there |
|---|---|
| [ansible-infra-roles](https://github.com/djlongy/ansible-infra-roles) | The Ansible roles, reference playbooks, sample inventories, plugins and docs moved to their own repo, packaged as the `djlongy.infra` collection. Install with `ansible-galaxy collection install git+https://github.com/djlongy/ansible-infra-roles.git`. |
| [`linux/`](linux/) | Distribution-level reference docs and configs. fapolicyd troubleshooting on EL9, hardened EL9 kickstart with GNOME + FIPS + STIG. |
| [`bash/`](bash/) | Self-contained bash tools — fzf-driven git helpers, an Oh My Bash powerline theme + deploy script, a VS Code Server installer for air-gapped networks. |
| [`python/`](python/) | Python install/setup notes and small helpers. Currently the VMware vSphere Automation SDK install guide. |
| [`quay-layer-transfer/`](quay-layer-transfer/) | Container images across a one-way link, one new layer at a time: a low-side and a high-side Quay, NiFi send/receive flows, an OCI store on the far side, and the export/import/verify scripts. Two modes — the far side publishes a digest list back and the exporter packs only what is missing, or a strict one-way link where NiFi dedupes against a Redis ledger. One compose file, proven with three transfers (22 blobs, then 6, then 0). |
| [`container-mirror/`](container-mirror/) | The CI half of that pipeline, as three GitLab CI components and the composition that wires them: resolve a reference list, copy every image into your own registry, then pack only the layers the far side does not hold into one tar in object storage. Clone, write an `images.txt`, set four masked variables, include one file. Credentials are plain CI variables, with a Vault JWT login as an option rather than a requirement. |
| [`container-build/`](container-build/) | Rebuild one image from an upstream base through a DevSecOps pipeline, then promote the exact digest that passed. A GitLab CI composition a consumer includes in one block, five components behind it, and the bash that does the work. Pluggable push backends and scanners all writing one canonical SBOM and vulnerability report, two gates that cannot be turned off from outside, and candidate and release registries kept apart so only the publish job ever names release credentials. A consumer owns two files: a `Dockerfile` and an `image.env`. |
| [`docs-as-code/`](docs-as-code/) | Documentation kept as Markdown in git, reviewed like code and published by CI. Zensical / Material for MkDocs tutorial with a runnable example site, a GitLab Pages setup guide, a CI job that mirrors `docs/` into the GitLab wiki for instances without Pages, a container build plus Kubernetes deploy for teams on RKE2, and a wheel-registry seeder so offline runners can install the toolchain. |
| [dev-env repo](https://github.com/djlongy/dev-env) | The dotfiles package, VS Code air-gap kit, and Oh My Bash tooling moved to their own repo — clone as `~/.dotfiles` and run `install.sh`. |

## Highlights

| Project | What it does | Lives in |
|---|---|---|
| [fapolicyd troubleshooting](linux/fapolicyd/) | Step-by-step debug-deny / trust.d / rules.d guide for EL9 hardening. | `linux/` |
| [EL9 hardened kickstart](linux/kickstart/) | Unattended-install template — GNOME, FIPS, fapolicyd, STIG scan, VMware USB passthrough. | `linux/` |
| git functions, Oh My Bash, VS Code air-gap, tmux dotfiles | Moved to the [dev-env repo](https://github.com/djlongy/dev-env). | — |
| [Material for MkDocs tutorial](docs-as-code/mkdocs-material/) | Zero to a published docs portal: theme and dark-mode toggle, fonts, emoji and icons, code blocks, content tabs, admonitions, Mermaid diagrams, footer, then GitHub Pages and GitLab Pages workflows. Runnable example site included, pinned for the 2026 MkDocs/Material/Zensical situation. | `docs-as-code/` |
| [GitLab wiki mirror](docs-as-code/gitlab-wiki-mirror/) | `docs/` stays the source; CI mirrors it into the project wiki: index pages become section pages, links and attachments rewritten to the forms GitLab resolves, `_sidebar.md` from the `.pages` nav, push only on change; wiki edits flow back as repo commits by their author (three-way merge, newer edit wins). Plus a one-time importer that turns an existing wiki (root pages, children, attachments, cross-links) into that `docs/` layout, the full lint-and-publish pipeline, and a worked two-section example. Tested (pytest, 98% coverage) and SonarQube-clean. | `docs-as-code/` |
| [GitLab Pages setup](docs-as-code/gitlab-pages-setup/) | Admin steps to enable Pages, then the URL options ranked by what they need: namespace-root project for `wiki.pages.example.com` (tested, no admin change), custom domain, reverse proxy, ingress; DNS placement and access control. | `docs-as-code/` |
| [GitLab wheel registry](docs-as-code/gitlab-wheel-registry/) | Bash script that uploads a folder of wheels to a GitLab PyPI registry with curl only (job token in CI, PAT elsewhere, per-filename skip, `PUBLIC_PULL` for anonymous install), plus the pipeline that downloads the mkdocs/Zensical closure per interpreter, publishes from ubi9 and python-slim, and has credential-free consumer jobs that fail if any dist came from pypi.org instead of the registry. | `docs-as-code/` |
| [MkDocs on Kubernetes](docs-as-code/mkdocs-on-kubernetes/) | Docs site as a hardened container: Material image builds, unprivileged nginx serves, kaniko builds it in a Kubernetes-executor runner, kustomize manifests (non-root, read-only fs, probes) deploy through the GitLab agent. Image and manifests verified locally and with a server-side dry run. | `docs-as-code/` |
| [Quay layer transfer](quay-layer-transfer/) | Operating procedure for moving N, N-1, N-2 of a semver image group across a diode: `export.sh` builds one OCI layout of the newest versions, the send flow gets it across, the receive flow unpacks into a persistent OCI store, `import.sh` merges and `skopeo copy`s into the high Quay, `verify.sh` pulls and compares digests. Where a digest list may come back, `import.sh` publishes what the far store holds and the exporter leaves those blobs out, so the flow is three processors with no state; where nothing may come back, NiFi dedupes against a Redis ledger instead. Lab is two Quay 3.15 instances plus NiFi 2.5 in one compose file. | `quay-layer-transfer/` |
| [Container mirror pipeline](container-mirror/) | Three GitLab CI components and one composition: resolve a reference list, `skopeo copy` every image into your own registry under a prefix, then pull the same set into a single OCI layout and upload one tar of just the blobs the receiving side has not published back. A merge request inspects every reference and writes nothing. The default branch mirrors and exports. Credentials are four masked CI variables, with a Vault JWT login as an option. A guarded dry-run flag, because a project-level variable outranks a job one and would turn every mirror into a green no-op. | `container-mirror/` |
| [Container build pipeline](container-build/) | One upstream image, rebuilt with a corporate trust store, scanned, gated and promoted by digest. The consumer's `.gitlab-ci.yml` is one `include:` and nothing else. Syft and Grype are mandatory gates and only an explicit advisory flag stands them down, so a crashed scanner fails the build instead of passing it. Xray stays advisory because a `needs:` entry naming a job that `xray-mode: off` never creates fails the whole pipeline. A generated flat single-file form ships alongside the composition, because `include:remote` gives nested local includes no project context. 129-scenario regression suite, no registry and no credentials. | `container-build/` |
| [`vsphere-automation-sdk`](python/vsphere-automation-sdk/) | Install instructions for the VMware vSphere Automation SDK on macOS + Oracle Linux. | `python/` |

## Conventions

- Every project folder has a `README.md` explaining what it does,
  requirements, and how to plug it in.
- New things go under the relevant **category** folder, never at the
  root.
- Copy-paste portability: where possible, a project is self-contained
  in its folder. Where it isn't (e.g. wrapper roles depending on a
  generic engine), the dependency is named in the README.
- This is public. Don't commit anything that shouldn't be on a public
  GitHub.
