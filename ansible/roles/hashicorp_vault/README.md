# hashicorp_vault

## TL;DR

Installs and configures HashiCorp Vault as a native (package-installed) Raft
HA cluster: firewall, TLS via Let's Encrypt, cluster init/unseal, policies,
auth methods (userpass/LDAP/AppRole/Kubernetes), audit devices, the Transit
engine for cosign, auto-unseal, and backups. GitLab OIDC SSO for the UI and
GitLab CI JWT auth are available but off by default.

```bash
ansible-playbook -i inventories/<env>/hosts.yml playbooks/<playbook>.yml
```

## Requirements

Install collections before running (repo `requirements.yml`, or ad-hoc):

    ansible-galaxy collection install -r requirements.yml

| Collection | When | Used for |
|---|---|---|
| `ansible.posix` | always | firewalld rules |
| `community.hashi_vault` | always | Vault API reads/writes (policies, auth, secrets engines) |

## Key variables

Full list: `defaults/main.yml` and `vars/main.yml` (cluster/port/policy
constants). Contract: `meta/argument_specs.yml`.

**Required** = value must be correct for a successful run (defaults often work).
**Optional** = safe to leave default / empty; phase stays off or uses built-ins.
**When X** = required only if that feature is on.

| Req | Variable | Default | Purpose |
|---|---|---|---|
| **Required** | `vault_domain` | `""` | Cluster FQDN — drives `vault_addr`, TLS SAN, and OIDC redirect URIs |
| **Required** | `vault_ha_nodes` | `[]` | Raft HA cluster node definitions |
| **Required** | `vault_cert_domains` | `[]` | TLS cert SANs; first entry is the Let's Encrypt lineage name |
| **Required** | `vault_cert_email` | `""` | ACME registration email |
| Optional | `vault_install_method` | `package` | `package` (RPM) or `docker` (still installs a host CLI) |
| Optional | `vault_api_port` | `8200` | Vault API/UI listener (`vars/main.yml`) |
| Optional | `vault_cluster_port` | `8201` | Raft cluster communication port (`vars/main.yml`) |
| Optional | `vault_host_data_dir` | `/opt/vault` | Persistent Vault data directory (`vars/main.yml`) |
| Optional | `vault_additional_kv_mounts` | `[]` | Extra KV v2 mounts beyond the standard set |
| Optional | `vault_legacy_policies_to_remove` / `vault_legacy_approles_to_remove` / `vault_legacy_userpass_to_remove` | `[]` | Entity names to delete during `legacy_cleanup` (`vars/main.yml`) |
| When LDAP | `vault_ldap_auth_enabled` | `false` | Enables LDAP auth (FreeIPA); needs `vault_ldap_url`/`vault_ldap_binddn`/etc |
| When OIDC | `vault_gitlab_oidc_enabled` | `false` | Enables GitLab OIDC SSO for the Vault UI |
| When CI JWT | `vault_gitlab_jwt_enabled` | `false` | Enables GitLab CI JWT auth for pipeline Vault access |

## Minimum configuration

```yaml
# group_vars/hashicorp_vault_hosts.yml
---
# Required
vault_domain: "vault.{{ env }}.{{ domain }}"
vault_ha_nodes: "{{ groups['vault'] }}"
vault_cert_domains:
  - "vault.{{ env }}.{{ domain }}"
vault_cert_email: "REPLACE_ME_vault_cert_email"
```

## Usage

```yaml
- name: Deploy Vault
  hosts: vault
  roles:
    - role: hashicorp_vault
      tags: [hashicorp_vault]
```

Run it:

```bash
ansible-playbook -i inventories/<env>/hosts.yml playbooks/<playbook>.yml
```

Iterate on a single phase once the cluster exists:

```bash
ansible-playbook -i inventories/<env>/hosts.yml playbooks/<playbook>.yml --tags policies
ansible-playbook -i inventories/<env>/hosts.yml playbooks/<playbook>.yml --tags ldap
ansible-playbook -i inventories/<env>/hosts.yml playbooks/<playbook>.yml --list-tags
```

## Preconditions

- LDAP auth (`vault_ldap_auth_enabled: true`) requires the bind password to
  already exist at `vault_ldap_bindpass_vault_path` in HashiCorp Vault before
  the role runs.
- GitLab OIDC SSO (`vault_gitlab_oidc_enabled: true`) requires a GitLab admin
  API token at the path in `vault_gitlab_root_api_token_secret_path` (or the
  inventory-supplied OIDC client id/secret) to manage the OAuth application.

## Behaviour

- `cluster_init` is a one-time bootstrap: it checks for an existing
  `vault_init.json` on the leader and skips `vault operator init` if the
  cluster is already initialized.
- The `legacy_cleanup` phase permanently deletes the ACL policies, AppRoles,
  and userpass accounts named in `vault_legacy_policies_to_remove` /
  `vault_legacy_approles_to_remove` / `vault_legacy_userpass_to_remove`. Empty
  by default in inventory intent; names live in `vars/main.yml` — populate
  deliberately.
- Tagged-subset runs (`--tags ldap`, `--tags policies`) lazy-load
  `vault_root_token` from the leader so post-setup tasks can authenticate
  without re-running `cluster_init`.
