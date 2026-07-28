# inventories/vault

Sample 3-node HA inventory for `hashicorp_vault_container`.

| File | Purpose |
|---|---|
| `hosts.yml` | vault-01..03 (replace IPs) |
| `group_vars/vault/main.yml` | Non-secrets — **Path A** skeleton |
| `group_vars/vault/vault.yml.example` | Secrets template → encrypt as `vault.yml` |

## Auth path (Path A)

This inventory is **Path A** (simple FreeIPA LDAP groups):

1. Policies + tenant mounts always declared in `main.yml`
2. LDAP **off** until FreeIPA groups + bind password exist
3. `identity_groups: []` — Path A only (Identity is Path B)
4. CI / AppRole / userpass empty until needed

Full map: `roles/hashicorp_vault_container/README.md` → **Auth & RBAC map**  
Copy-paste: `roles/hashicorp_vault_container/examples/`

## Enable LDAP when ready

1. FreeIPA: create `vault-acme-prod`, `vault-acme-dev`, `vault-globex-prod`, `vault-admins`
2. `cp group_vars/vault/vault.yml.example group_vars/vault/vault.yml` and set bind password
3. `ansible-vault encrypt group_vars/vault/vault.yml`
4. Set `hashicorp_vault_ldap_enabled: true` in `main.yml`
5. `ansible-playbook -i inventories/vault/hosts.yml playbooks/vault_cluster.yml --tags policies,ldap`
