# vSphere dynamic inventory — tag-grouped (minimal)

A lean, real-world `vmware.vmware.vms` dynamic inventory: **grouped by vCenter tag**,
**cached**, **powered-on only**. This is the natural companion to the
[`vsphere_vm`](../../../roles/vsphere_vm/) role (which stamps `Tenant` / `Environment`
tags on the VMs it creates) and the
[`vcenter_svc_accounts`](../../../roles/vcenter_svc_accounts/) role (which provisions the
least-privilege `svc-inventory` read-only account this config authenticates as).

- File: `tag_grouped.vms.yml`
- Plugin: `vmware.vmware.vms` (official collection — `ansible-galaxy collection install vmware.vmware`)

Prefer this when your organizing principle is **tags**; prefer the kitchen-sink
[`../vsphere-vms-dynamic/`](../vsphere-vms-dynamic/) template when it's **folder hierarchy**
or you want every knob documented.

```bash
# list / graph
ansible-inventory -i inventories/vmware/vsphere-tag-grouped/ --graph

# run a play against every VM tagged Environment=prod
ansible-playbook -i inventories/vmware/vsphere-tag-grouped/ site.yml --limit env_prod

# force a cache refresh
ansible-inventory -i inventories/vmware/vsphere-tag-grouped/ --list --flush-cache
```

## Groups it creates

Straight from vCenter tags:

- `tenant_<tag>` — from the `Tenant` tag category
- `env_<tag>` — from the `Environment` tag category
- `tag_<tag>` — every tag value on the VM

`ansible_host` is the guest IP (VMware Tools reported). Intersect groups for precise
targeting, e.g. `tenant_acme:&env_prod`.

## Notes baked into the config

- **`filter_expressions` is an EXCLUDE filter** — a host matching the expression is
  *dropped*. We exclude `poweredOff` to keep only powered-on VMs.
- **`tags_by_category.<Cat>` is `[{urn: name}]`**, so group keys unwrap it with
  `dict2items | flatten | map(attribute='value')`.
- **No secrets in the file** — the password comes from a Vault lookup (swap for an env
  var if you prefer). Authenticate as a least-privilege read-only account.
- **Caching** avoids the ~40 s cold vCenter fetch on every run: the first run populates
  `~/.ansible/tmp/inventory_cache` (jsonfile), subsequent runs within `cache_timeout`
  (1800 s) are ~1 s. `cache_prefix: vmware_tags_` keeps it distinct from the sibling
  configs sharing that directory.

## Enabling the plugin

`ansible.cfg` already lists it (see the repo `[inventory]` section):

```ini
[inventory]
enable_plugins = ..., vmware.vmware.vms
```
