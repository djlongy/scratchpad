# Ansible

Standard Ansible repository layout. Each role under `roles/` is
self-contained and copy-pasteable into another project — bring its
folder, satisfy its required collections, set its variables, done.

## Layout

```
ansible/
├── ansible.cfg              # roles_path, inventory, action_plugins
├── roles/                   # portable roles (each has its own README.md)
│   ├── common/                  # function-like task helpers (tasks_from: …)
│   ├── firewalld/               # XML-template firewalld service + zone management
│   ├── hashicorp_vault_container/  # auto-scaling Vault (standalone or Raft HA) on Docker
│   ├── hashicorp_vault/            # alias of hashicorp_vault_container
│   ├── freeipa_client/          # FreeIPA enrol + certmonger service certs
│   ├── docker/                  # Docker CE install
│   ├── storage/                 # disk → LVM/plain → mount
│   ├── baseline/                # OS hardening baseline
│   ├── vsphere_vm/              # vCenter VM lifecycle
│   ├── swarm_stack/             # generic Docker Swarm deploy engine
│   └── mattermost_swarm/        # wrapper: Mattermost via swarm_stack
├── playbooks/               # entry-point playbooks
│   ├── vault_solo_e2e.yml       # full daisy-chain: VM → baseline → FreeIPA/TLS → Vault
│   ├── vault_cluster.yml
│   ├── mattermost_swarm.yml
│   ├── mattermost_freeipa_prep.yml
│   ├── audit_single_logger.yml
│   ├── audit_multi_logger.yml
│   ├── audit_multi_play.yml
│   └── audit_semaphore.yml
├── inventories/             # one subdir per environment / source
│   ├── vaultsolo/               # single-node Vault E2E lab inventory
│   ├── vault/                   # 3-node vault cluster lab inventory
│   ├── swarm/                   # docker swarm bootstrap inventory
│   └── vmware/                  # vSphere dynamic inventory configs
├── plugins/
│   └── action/                  # action plugins (e.g. get_cli_args)
├── files/                   # reference assets (not Jinja-templated)
│   └── fapolicyd-rule-templates/  # drop-in rules.d/ examples consumed by roles/common/tasks/fapolicyd.yml
├── scripts/                 # shell helpers
│   ├── vault-env-client.sh      # ansible-vault password from env var
│   └── load_vmware_env.sh       # source VMWARE_* from HCV
└── docs/                    # ansible-specific long-form guides
    ├── ansible-design-principles.md  # community-standard playbook/role/tag/var conventions
    └── vault-env-unlock.md           # unlock-ansible / unlock-vmware shell helpers
```

OS-level reference docs (fapolicyd troubleshooting, EL9 kickstart) live
at the scratchpad's top level under [`../linux/`](../linux/) — they're
not Ansible-specific.

## Roles

| Role | What it does |
|---|---|
| [`hashicorp_vault_container`](roles/hashicorp_vault_container/) | Auto-scaling containerized HashiCorp Vault (1 node = standalone Raft; odd N ≥ 3 = HA). Self-signed or FreeIPA/certmonger TLS, Shamir unseal, multi-tenant KV, ACL policies, LDAP/userpass/AppRole, PKI mount, audit, backup timer, rename self-heal (`peers.json`). |
| [`hashicorp_vault`](roles/hashicorp_vault/) | Alias of `hashicorp_vault_container` (same code; kept for older docs/inventories). |
| [`freeipa_client`](roles/freeipa_client/) | FreeIPA client enrol + certmonger service certificates (with DNS/principal drift self-heal). |
| [`docker`](roles/docker/), [`storage`](roles/storage/), [`baseline`](roles/baseline/), [`firewalld`](roles/firewalld/), [`yum_repos`](roles/yum_repos/), [`vsphere_vm`](roles/vsphere_vm/), [`ssh_agent_key`](roles/ssh_agent_key/) | Supporting daisy-chain roles for a full Vault host build. |
| [`audit_logging`](roles/audit_logging/) | Portable run-audit logging for `ansible-playbook`. Buffers play metadata (`accumulate`) and ships one JSON record to file / syslog / rsyslog / Fluentd / Elasticsearch / Splunk HEC / CloudWatch. List under `roles:` with `audit_logging_mode` — no `post_tasks` required. |
| [`common`](roles/common/) | Function-like task helpers callable as `tasks_from:` — passphrase generation, vault-backed secret bootstrapping, fapolicyd rule deploy. (Legacy audit helpers still present; prefer the standalone `audit_logging` role.) |
| [`swarm_stack`](roles/swarm_stack/) | Generic engine for deploying any application stack onto an existing Docker Swarm. Encrypted overlays, NFS volumes, content-versioned secrets/configs, redeploy + teardown via tags. |
| [`mattermost_swarm`](roles/mattermost_swarm/) | Wrapper over `swarm_stack` for Mattermost (postgres + app), with optional FreeIPA-backed LDAP/SSO. Worked example of the wrapper pattern. LDAP off = a minimal Mattermost deploy. |

## Conventions

- Role names are lowercase `snake_case`, descriptive of what the role
  does. No `app_` or other team-specific prefixes.
- Per-role README at `roles/<name>/README.md`.
- Defaults in `roles/<name>/defaults/main.yml` are the source of truth
  for variable shapes; READMEs explain the contract and gotchas.
- Inventories are split by environment under `inventories/<env>/` with
  `hosts.yml` + `group_vars/<group>/{main.yml,vault.yml}`.
  `vault.yml.example` is the unencrypted template; the real `vault.yml`
  is `ansible-vault encrypt`'d.
- Playbooks are thin — they declare hosts and apply roles. All logic
  lives in roles.

## Required collections

Roles in this repo touch the following collections:

```yaml
# requirements.yml (suggested)
collections:
  - name: community.general          # passphrase generation lookups
  - name: community.hashi_vault      # ensure_secrets reads/writes Vault
  - name: community.docker           # swarm_stack
  - name: community.vmware           # vmware_vm_inventory plugin
  - name: vmware.vmware              # alternative inventory plugin
  - name: freeipa.ansible_freeipa    # mattermost_freeipa_prep, ldap_auth
```

Install with `ansible-galaxy collection install -r requirements.yml`.
