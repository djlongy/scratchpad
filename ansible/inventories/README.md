# Inventories

## Reference inventory

**`example/`** is the only inventory structure to copy. It is a complete
production-shaped skeleton: identity, underlay selection, VM naming, group
vars layout, and a few host groups.

```bash
cp -a inventories/example inventories/mgt
# edit inventories/mgt/group_vars/all/main.yml:
#   env, vault_kv_env, underlay.*, vsphere_vm_name_prefix
# edit inventories/mgt/hosts.yml IPs + groups
```

| After copy | `env` | Underlay | `vsphere_vm_name_prefix` |
|---|---|---|---|
| `mgt` | mgt | `underlay.mgt` | `mgta` |
| `dev` | dev | `underlay.dev` | `deva` |
| `prod` | prod | `underlay.prod` | `proda` |

Estate constants stay in `playbooks/group_vars/all/` (domain, networks,
canonical formulas) — one place for every inventory.

## Layout (filename standard)

```text
inventories/<name>/                 # one folder per environment
  hosts.yml                         # groups = resource nouns
  group_vars/
    all/
      main.yml                      # REQUIRED: env, segment, VM prefix
    <group>.yml                     # small groups: single file
    <group>/                        # large groups: directory of topics
      main.yml                      # non-secrets
      vault.yml.example             # secret shape (encrypt real vault.yml)
  host_vars/                        # rare per-host overrides only
    <inventory_hostname>.yml
```

## Identity & DNS

| Var | Where | Notes |
|---|---|---|
| `domain` | `playbooks/group_vars/all/all.yml` only | e.g. `example.com` |
| `env` | inventory `all/main.yml` | `mgt` \| `dev` \| `prod` |
| `tenancy` | estate default + inventory override | org label |
| `vault_kv_env` | inventory | usually == `env` |

| Kind | Pattern | Example |
|---|---|---|
| Host FQDN | `{{ canonical_hostname }}.{{ env }}.{{ domain }}` | `vault-01.prod.example.com` |
| Service URL | `<service>.{{ env }}.{{ domain }}` | `vault.prod.example.com` |
| FreeIPA realm DNS | `freeipa_server_domain` (separate) | `ipa.prod.example.com` |

## Host & VM naming (recommended)

Prefer **short service keys** in inventory. Put environment only in the
vCenter prefix and in the DNS subdomain.

| Layer | Source | Example |
|---|---|---|
| Inventory key | `hosts.yml` | `vault-01` |
| OS short hostname | `canonical_hostname` | `vault-01` |
| vCenter object | `vsphere_vm_name` | `proda-vault-01` |
| Guest customization name | `vsphere_vm_hostname` | `proda-vault-01` |
| DNS FQDN | `canonical_fqdn` | `vault-01.prod.example.com` |

Set once in `group_vars/all/main.yml`:

```yaml
vsphere_vm_name_prefix: "proda"
vsphere_vm_name: "{{ vsphere_vm_name_prefix }}-{{ canonical_hostname }}"
vsphere_vm_hostname: "{{ vsphere_vm_name }}"
```

`canonical_env_tokens` in playbook group_vars still strips accidental
prefixes (`proda-`, `mgt-`, …) if a host key includes them.

## Other folders in this directory

`test/`, `trial/`, `vault/`, `vmware/` are older scratchpad demos / dynamic
inventory plugins — not the estate template. Prefer `example/` for new work.

## Recommendations (design notes)

1. **One inventory folder = one env.** Do not invent parallel trees for
   “scenarios”; if a lab needs VLAN21, set `env: dev` and
   `underlay.dev_cluster` in that folder’s `all/main.yml`.
2. **Groups are nouns** (`vault`, `bastion`, `gitlab`) — never
   `install_vault` or `sw_vault`.
3. **Secrets in group dirs**, not `all/`: `group_vars/vault/vault.yml`
   (encrypted). Keep `main.yml` plaintext for non-secrets.
4. **No kitchen-sink `firewall_rules` in `all/`** — each role opens its
   own ports; bastion gets destination CIDRs in `bastion.yml`.
5. **IPs live on hosts** (or IPAM later); gateways/portgroups come from
   `underlay.*`, never hard-coded twice.
6. **Copy the folder, don’t fork the formulas** — domain/canonical/network
   catalog stay estate-wide so envs cannot drift.
