# vaultsolo inventory

Single-node **containerized HashiCorp Vault** lab inventory for the
[`vault_solo_e2e.yml`](../../playbooks/vault_solo_e2e.yml) playbook.

## What it proves

The full daisy chain on one VM:

```text
ssh_agent_key → vsphere_vm → yum_repos → storage → baseline → firewalld
→ [freeipa_client] → docker → hashicorp_vault_container
```

| Hosts in `vault_container` | Topology |
|---|---|
| 1 | Standalone Raft |
| 3 / 5 / … | Raft HA (odd only) |

## Quick start

```bash
# 1. Edit hosts + group_vars (IPs, vSphere, domain, secrets)
# 2. Greenfield / seed Vault (self-signed TLS, stock repos OK if yum_repos empty)
ansible-playbook -i inventories/vaultsolo/hosts.yml playbooks/vault_solo_e2e.yml

# 3. FreeIPA-issued TLS (requires freeipa_client_* + vault_tls_mode=freeipa)
ansible-playbook -i inventories/vaultsolo/hosts.yml playbooks/vault_solo_e2e.yml \
  -e vault_tls_mode=freeipa
```

## Naming

Recommended host identity split:

| Layer | Example |
|---|---|
| Inventory / vCenter name | `dev-vault-01` |
| OS hostname | `vault-01` |
| Host FQDN | `vault-01.dev.example.com` |
| Service URL | `vault.dev.example.com` |

`canonical_hostname` / `canonical_fqdn` in `group_vars` implement the strip of
the env prefix so FreeIPA principal, cert SAN, and DNS agree after renames.

## Related roles

- [`hashicorp_vault_container`](../../roles/hashicorp_vault_container/) — Vault itself
- [`freeipa_client`](../../roles/freeipa_client/) — enrolment + certmonger certs
- [`docker`](../../roles/docker/), [`storage`](../../roles/storage/), [`baseline`](../../roles/baseline/)
- [`vsphere_vm`](../../roles/vsphere_vm/) — optional VM provisioner
