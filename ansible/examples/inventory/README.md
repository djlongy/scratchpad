# VMware vSphere Dynamic Inventory

Reference implementation for Ansible dynamic inventory against VMware vCenter using the `community.vmware.vmware_vm_inventory` plugin.

Designed for real-world homelabs and enterprise environments with multi-environment (prod/staging/dev/DR) and multi-tenant vSphere clusters.

## Files

| File | Purpose |
|------|---------|
| `vmware.yml` | Inventory plugin config — no secrets, ready to commit |
| `load_vmware_env.sh` | Sources `VMWARE_*` env vars from HashiCorp Vault |

## Prerequisites

```bash
# Install the collection (community.vmware >= 4.0)
ansible-galaxy collection install community.vmware

# Or pin it in collections/requirements.yml:
# - name: community.vmware
#   version: ">=4.0,<5"
```

## Quick Start

```bash
# 1. Authenticate to Vault
vault login

# 2. Source credentials into your shell
source scripts/load_vmware_env.sh

# 3. Test the inventory
ansible-inventory -i inventory/vmware.yml --list

# 4. Run a playbook
ansible -i inventory/vmware.yml poweredOn -m ping
```

## Credential Management

Credentials are **never** stored in `vmware.yml`. They're passed via environment variables sourced from HashiCorp Vault:

```
VMWARE_HOST     — vCenter FQDN or IP
VMWARE_USER     — Service account username
VMWARE_PASSWORD — Service account password
```

`load_vmware_env.sh` tries a primary service-account path first, then falls back to a shared admin credential — useful when the dedicated svc account isn't yet provisioned.

### Targeting a specific vCenter

```bash
source scripts/load_vmware_env.sh vcenter-prod.example.com
source scripts/load_vmware_env.sh vcenter-dr.example.com
```

## Multi-Environment Setup

Create one inventory file per environment, each sourcing different env vars:

```
inventory/
  vmware_prod.yml      ← VMWARE_HOST=vcenter-prod.example.com
  vmware_dr.yml        ← VMWARE_HOST=vcenter-dr.example.com
  vmware_staging.yml   ← VMWARE_HOST=vcenter-staging.example.com
```

Add a vSphere **Tag Category** named `environment` with tags `prod`, `staging`, `dev`, `dr`. Then in `vmware.yml`:

```yaml
keyed_groups:
  - key: tags.environment
    prefix: env
```

This produces groups: `env_prod`, `env_staging`, `env_dev`, `env_dr`.

## Multi-Tenant Setup

Add a vSphere **Tag Category** named `tenant` and tag VMs accordingly. In `vmware.yml`:

```yaml
keyed_groups:
  - key: tags.tenant
    prefix: tenant

filters:
  - "'acme' in tags.get('tenant', [])"   # scope to a single tenant
```

For full isolation, combine with per-tenant Vault policies and separate `load_vmware_env.sh` credential paths.

## Performance Tips

- **Enable caching** (`cache: true`) on large estates — reduces vCenter API calls
- **Limit `properties`** to only what your playbooks consume
- For 1000+ VMs, consider splitting by datacenter using the `datacenter` filter

## Ansible Configuration

Add to `ansible.cfg` to enable the plugin:

```ini
[inventory]
enable_plugins = community.vmware.vmware_vm_inventory, yaml, ini
```

## See Also

- [community.vmware.vmware_vm_inventory docs](https://docs.ansible.com/ansible/latest/collections/community/vmware/vmware_vm_inventory_inventory.html)
- [vSphere Tag API](https://developer.vmware.com/apis/vsphere-automation/latest/cis/tagging/)
- [HashiCorp Vault KV v2](https://developer.hashicorp.com/vault/docs/secrets/kv/kv-v2)
