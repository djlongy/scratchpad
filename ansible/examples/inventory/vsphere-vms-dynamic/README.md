# vSphere Dynamic Inventory (Sanitized Template)

This folder contains a production-style, sanitized `vmware.vmware.vms` inventory template for multi-tenant vSphere estates.

- File: `vmware_vms.yml`
- Plugin: `vmware.vmware.vms`
- Safe defaults: powered-on only, allocated IP only, path-based hostnames
- Grouping: tenant, folder, resource pool, guest OS, tags, env/role conventions

## Why this template is useful

- Handles chaotic naming by using folder hierarchy as the primary organizer.
- Produces readable hostnames while still having UUID fallback uniqueness.
- Selects SSH-friendly IPs for multi-NIC VMs.
- Gives clean Ansible group patterns for both broad and precise targeting.

## Requirements

1. Install collection:

```bash
ansible-galaxy collection install vmware.vmware
```

2. Install Python deps (path depends on collection location):

```bash
pip install -r ~/.ansible/collections/ansible_collections/vmware/vmware/requirements.txt
# or
pip install -r collections/ansible_collections/vmware/vmware/requirements.txt
```

3. Export auth env vars:

```bash
export VMWARE_HOST="vcsa.example.com"
export VMWARE_USER="svc_ansible@vsphere.local"
export VMWARE_PASSWORD="<secret>"
```

## Quick start

```bash
ansible-inventory -i vmware_vms.yml --graph
ansible-inventory -i vmware_vms.yml --list
```

## Grouping model

This template creates groups that are easy to reason about in day-to-day operations:

- `tenant_*` from folder hierarchy (`/DC/vm/<tenant>/...`)
- `folder_*` from immediate parent folder
- `rp_*` from resource pool
- `guestos_*` from `config.guestId`
- `env_*` and `role_*` from tag categories (`Environment`, `Group`)
- `tag_*` from all tag values
- `power_*` from runtime power state
- `path_*` full hierarchy groups (optional but enabled)

## Target examples

Ad-hoc command against a whole group:

```bash
ansible -i vmware_vms.yml tenant_tenantA -m ping
```

Single host:

```bash
ansible -i vmware_vms.yml tenantA_prod_web_web01 -m ping
```

Intersect groups for precise blasts:

```bash
ansible -i vmware_vms.yml 'tenant_tenantA:&env_prod:&power_poweredOn' -m ping
ansible -i vmware_vms.yml 'role_db:&rp_Resource_Pool' -m shell -a 'hostname -f'
```

Playbook with limit:

```bash
ansible-playbook -i vmware_vms.yml playbook.yml -l 'tenant_tenantB:&guestos_ubuntu64Guest'
```

## Powered on + allocated IP filter

This is already enabled in `filter_expressions`:

- Excludes powered off VMs
- Excludes VMs with no allocated guest IP on any NIC

If you need all VMs (including templates or powered off), comment out those filters.

## Multi-tenant conventions (recommended)

Use folder hierarchy like:

```text
/DC1/vm/tenantA/dev/app
/DC1/vm/tenantA/prod/app
/DC1/vm/tenantB/prod/data
```

Use tag categories like:

- `Environment`: `dev`, `prod`
- `Group`: `web`, `db`, `iam`, `vault`

Then your targets stay clean and predictable even when VM names are inconsistent.

## Ansible-friendly output tips

- Keep prefixes short and explicit (`tenant_`, `env_`, `role_`, `rp_`).
- Use intersections rather than long host lists.
- Use `--graph` first, then `-l` with playbooks.

## Optional toggles in the template

`vmware_vms.yml` includes commented options you can enable when needed:

- `search_paths` for faster scoped inventory
- explicit `hostname/username/password` fields (instead of env vars)
- `sanitize_property_names`
- `flatten_nested_properties`
- `gather_compute_objects`
