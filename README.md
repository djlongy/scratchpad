# Scratchpad

Second brain for sysadmin / DevOps work. Quick references, runnable
snippets, and small self-contained projects — organised so things can
be found in their relevant places and dropped into real deployments
without surgery.

Each top-level folder is a category. Each project inside has its own
README explaining what it does, what it requires, and how to plug it in.

## Categories

| Folder | What's in there |
|---|---|
| [`ansible/`](ansible/) | Portable Ansible roles, reference playbooks, sample inventories, action plugins, and a fapolicyd rule-template library. Standard top-level layout (roles/, playbooks/, inventories/, plugins/, scripts/, files/, docs/). |
| [`linux/`](linux/) | Distribution-level reference docs and configs. fapolicyd troubleshooting on EL9, hardened EL9 kickstart with GNOME + FIPS + STIG. |
| [`bash/`](bash/) | Self-contained bash tools — fzf-driven git helpers, an Oh My Bash powerline theme + deploy script. |
| [`python/`](python/) | Python install/setup notes and small helpers. Currently the VMware vSphere Automation SDK install guide. |
| [`dotfiles/`](dotfiles/) | GNU Stow packages for fast machine bootstrap (tmux, dev shell). |

## Highlights

| Project | What it does | Lives in |
|---|---|---|
| [Ansible Design Principles](ansible/docs/ansible-design-principles.md) | Opinionated, source-cited reference for designing maintainable Ansible repos at scale. Playbook structure, role naming, tag schools, variable scoping, hostvars discipline, lint hygiene, migration strategy for inheriting an unprincipled codebase. | `ansible/docs/` |
| [`firewalld`](ansible/roles/firewalld/) | XML-template firewalld services + zones (the declarative firewalld-native way), source-CIDR and interface bindings, default-zone management, legacy `firewall_rules` back-compat, optional cleanup. Multi-distro via `ansible.builtin.package`. Behavioural defaults; env-specific bindings via inventory. | `ansible/roles/` |
| [`vault_container`](ansible/roles/vault_container/) | 3-node HashiCorp Vault Raft HA cluster as a Docker container. Self-signed TLS, Shamir unseal, KV mounts, ACL policies, optional FreeIPA LDAP. | `ansible/roles/` |
| [`swarm_stack`](ansible/roles/swarm_stack/) | Generic engine for deploying any application stack onto Docker Swarm. Encrypted overlays, NFS volumes, content-versioned secrets/configs, redeploy + teardown via tags. | `ansible/roles/` |
| [`mattermost_swarm`](ansible/roles/mattermost_swarm/) | Worked-example wrapper over `swarm_stack` deploying Mattermost (postgres + app) with optional FreeIPA LDAP/SSO. | `ansible/roles/` |
| [`common`](ansible/roles/common/) | Function-like task helpers callable as `tasks_from:` — passphrase generation, vault-backed secret bootstrapping, audit-log shipping (rsyslog/splunk/fluentd/elasticsearch/file/cloudwatch/syslog), fapolicyd rule deploy. | `ansible/roles/` |
| [`get_cli_args`](ansible/plugins/action/get_cli_args.README.md) | Action plugin exposing `ansible-playbook` CLI args, Semaphore extra-vars, and runtime git status to tasks. | `ansible/plugins/action/` |
| [`vsphere dynamic inventory`](ansible/inventories/vmware/) | Two flavours of vSphere inventory plugin config (community.vmware + the official vmware.vmware), wired up with persistent caching. | `ansible/inventories/` |
| [fapolicyd troubleshooting](linux/fapolicyd/) | Step-by-step debug-deny / trust.d / rules.d guide for EL9 hardening. | `linux/` |
| [EL9 hardened kickstart](linux/kickstart/) | Unattended-install template — GNOME, FIPS, fapolicyd, STIG scan, VMware USB passthrough. | `linux/` |
| [`bash/git-functions`](bash/git-functions/) | fzf interactive branch picker, prune-gone-branches helper. | `bash/` |
| [`bash/ohmybash`](bash/ohmybash/) | Powerline prompt with git/venv/time, deploy script, Nerd Font installer. | `bash/` |
| [`dotfiles/tmux`](dotfiles/) | Stow-managed tmux + dev environment bootstrap. | `dotfiles/` |
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
