# vsphere_vm

Host-centric vCenter VM lifecycle: clone from template, ensure, destroy.
Each play host builds **its own** guest from its own vars (parallel free).
All vCenter calls run on the controller (`delegate_to: localhost`).

```bash
ansible-playbook -i inventories/<env>/hosts.yml playbooks/site.yml --limit <newhost>
```

## Requirements

```bash
ansible-galaxy collection install -r requirements.yml
```

| Collection | When | Used for |
|---|---|---|
| `community.vmware` | always | clone, tags, power, disk, network |
| `vmware.vmware` | nested folder or GuestInfo | folder path, deploy_folder_template, advanced settings |
| `community.hashi_vault` | `vsphere_vm_vault_secret` set | password lookup |

`community.vmware` ≥ 4.x needs `pyVmomi` ≤ 8.0.2.x (pyVmomi 9 breaks JSON encoder).

## Key variables

Full list: `defaults/main.yml`. Contract: `meta/argument_specs.yml`.

| Req | Variable | Default | Purpose |
|---|---|---|---|
| **Required** | `vsphere_vm_server` | none | vCenter FQDN |
| **Required** | `vsphere_vm_datacenter` | none | Target datacenter |
| **Required** | `vsphere_vm_template` | none | Source template |
| When credentials | `vsphere_vm_vault_secret` **or** `vsphere_vm_password` | — | Password source |
| When placement | `vsphere_vm_cluster` **or** `vsphere_vm_esxi_host` | — | Where the VM lands |
| Optional | `vsphere_vm_hardware` | `{num_cpus: 2, memory_mb: 4096}` | Native `hardware` dict |
| Optional | `vsphere_vm_disk` | `[{size_gb: 40, type: thin}]` | Disk list, primary first |
| Optional | `vsphere_vm_wait_for_ssh` | `false` | Chain on-guest roles |
| Optional | `vsphere_vm_provision_via_guestinfo` | `false` | cloud-init GuestInfo (no GOSC race) |
| When destructive | `vsphere_vm_force_redeploy` / `_force_destroy` | `false` | Pass via `-e` only |

Shared values → `group_vars/`; per-host (`ansible_host`, name, extra disk) → `host_vars/`.

## Usage

```yaml
- hosts: vmware_vms
  gather_facts: false
  become: false
  roles:
    - vsphere_vm
```

```bash
# ensure
ansible-playbook -i inventories/<env>/hosts.yml playbook.yml

# destroy
ansible-playbook -i inventories/<env>/hosts.yml playbook.yml \
  -e vsphere_vm_state=absent -e vsphere_vm_force_destroy=true

# wipe + rebuild (play continues after)
ansible-playbook -i inventories/<env>/hosts.yml playbook.yml \
  -e vsphere_vm_force_redeploy=true
```

Chain an on-guest role:

```yaml
- hosts: vmware_vms
  gather_facts: false
  roles:
    - role: vsphere_vm
      vars: { vsphere_vm_wait_for_ssh: true }
    - role: storage
      vars: { storage_provision: true }
```

```bash
ANSIBLE_HOST_KEY_CHECKING=False ansible-playbook -i inventories/<env>/hosts.yml playbook.yml
```

## Behaviour (must know)

1. **Destructive is var-gated**, not `never`-tagged. `force_redeploy` / `force_destroy` arm the action; `--tags` only narrows scope.
2. **Delete by instance UUID**, after name+folder lookup (`identify.yml`). Same-name VMs in other folders cannot be selected. Moved VMs read as absent — update `vsphere_vm_folder`.
3. **GOSC creates are staged**: clone powered off → assert NIC start-connected → power on. One-call clone+power-on orphans the NIC. `connect.yml` is defense-in-depth (IP pre-check short-circuits healthy VMs).
4. **GuestInfo mode** (`provision_via_guestinfo: true`): no GOSC, cloud-init applies network. Needs a GuestInfo-ready template (`cloud-init query platform` → `vmware`).
5. **Gateway is never guessed**: explicit → `gateway_auto` first-usable → omit (unrouted).
6. **`wait_for_ssh` is the only guest SSH step.** Everything else is controller → vCenter.

## Template checklist

1. Guest OS type in GOSC matrix (`rhel9_64Guest` for Alma/RHEL 9 on HW 19 — not `other5xLinux64Guest`)
2. `open-vm-tools` + `perl`
3. Network on by default (`network --onboot=yes`)
4. If cloud-init: one `disable_vmware_customization: true`; GuestInfo also needs `datasource_list: [VMware, OVF, None]`
5. Seal: truncate machine-id, drop host keys, clear persistent-net rules

## Out of scope

- DNS records, firewalld zones, whole-inventory vCenter discovery
