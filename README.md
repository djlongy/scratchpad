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
| [`ansible/`](ansible/) | Portable Ansible roles, reference playbooks, sample inventories, action plugins, and a fapolicyd rule-template library. Standard top-level layout (roles/, playbooks/, inventories/, plugins/, scripts/, files/, docs/). |
| [`linux/`](linux/) | Distribution-level reference docs and configs. fapolicyd troubleshooting on EL9, hardened EL9 kickstart with GNOME + FIPS + STIG. |
| [`bash/`](bash/) | Self-contained bash tools — fzf-driven git helpers, an Oh My Bash powerline theme + deploy script. |
| [`python/`](python/) | Python install/setup notes and small helpers. Currently the VMware vSphere Automation SDK install guide. |
| [`dotfiles/`](dotfiles/) | GNU Stow packages for fast machine bootstrap (tmux, dev shell). |

## Highlights

| Project | What it does | Lives in |
|---|---|---|
| [Ansible Design Principles](ansible/docs/ansible-design-principles.md) | Opinionated, source-cited reference for designing maintainable Ansible repos at scale. Playbook structure, role naming, tag schools, variable scoping, hostvars discipline, lint hygiene, migration strategy for inheriting an unprincipled codebase. | `ansible/docs/` |
| [`vsphere_vm`](ansible/roles/vsphere_vm/) | vCenter VM lifecycle (clone-from-template create/destroy) with **two per-guest provisioning modes**: classic GOSC customization, or GOSC-free **cloud-init GuestInfo** (clone via `deploy_folder_template` + `guestinfo.metadata` — the NIC never disconnects). Multi-NIC with per-NIC static/DHCP, inventory-derived guests (one host = one VM), nested-folder placement, vCenter tag association, and bounded NIC-reconnect healing for legacy GOSC. Includes the template contract (guestId, cloud-init datasource) and field-verified gotchas. | `ansible/roles/` |
| [`vcenter_svc_accounts`](ansible/roles/vcenter_svc_accounts/) | Provision **least-privilege vCenter SSO service accounts** + scoped custom Roles (RBAC) so automation never runs as `administrator@vsphere.local`. Data-driven: per account it creates the SSO user (`dir-cli` over VCSA SSH, once, never rotated), writes the generated password to HashiCorp Vault **before** the account is usable, converges a custom Role to exactly its privilege list, and grants it at the vCenter root. Ships ready-made automation + read-only inventory privilege sets. | `ansible/roles/` |
| [`firewalld`](ansible/roles/firewalld/) | XML-template firewalld services + zones (the declarative firewalld-native way), source-CIDR and interface bindings, default-zone management, legacy `firewall_rules` back-compat, optional cleanup. Multi-distro via `ansible.builtin.package`. Behavioural defaults; env-specific bindings via inventory. | `ansible/roles/` |
| [`hashicorp_vault_container`](ansible/roles/hashicorp_vault_container/) | Auto-scaling HashiCorp Vault on Docker (standalone or Raft HA). Self-signed or FreeIPA/certmonger TLS, multi-tenant KV, LDAP/userpass/AppRole, PKI, audit, backup, rename self-heal. Full E2E playbook: [`vault_solo_e2e.yml`](ansible/playbooks/vault_solo_e2e.yml). | `ansible/roles/` |
| [`swarm_stack`](ansible/roles/swarm_stack/) | Generic engine for deploying any application stack onto Docker Swarm. Encrypted overlays, NFS volumes, content-versioned secrets/configs, redeploy + teardown via tags. | `ansible/roles/` |
| [`mattermost_swarm`](ansible/roles/mattermost_swarm/) | Worked-example wrapper over `swarm_stack` deploying Mattermost (postgres + app) with optional FreeIPA LDAP/SSO. | `ansible/roles/` |
| [`splunk_config`](ansible/roles/splunk_config/) | Capture a live Splunk-on-Swarm estate's **entire configuration** into a committable, re-appliable snapshot (readable `manifest.yml` + native app bundles), and apply it back. Reaches Splunk through the containers (`docker exec`/`docker cp`), auto-detects topology (cluster manager / SHC deployer / deployment server / search head / indexer), and **scrubs every secret** before anything touches git — re-seeding from HashiCorp Vault on apply. | `ansible/roles/` |
| [`common`](ansible/roles/common/) | Function-like task helpers callable as `tasks_from:` — passphrase generation, vault-backed secret bootstrapping, audit-log shipping (rsyslog/splunk/fluentd/elasticsearch/file/cloudwatch/syslog), fapolicyd rule deploy. | `ansible/roles/` |
| [`get_cli_args`](ansible/plugins/action/get_cli_args.README.md) | Action plugin exposing `ansible-playbook` CLI args, Semaphore extra-vars, and runtime git status to tasks. | `ansible/plugins/action/` |
| [`vsphere dynamic inventory`](ansible/inventories/vmware/) | Three vSphere inventory plugin configs — community.vmware, the official vmware.vmware (kitchen-sink folder-path template), and a lean **tag-grouped** config (groups off `Tenant`/`Environment` tags — the companion to the `vsphere_vm` + `vcenter_svc_accounts` roles). All wired up with persistent caching. | `ansible/inventories/` |
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
