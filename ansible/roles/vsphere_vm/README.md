# vsphere_vm

Portable vCenter **VM lifecycle** role — clones VMs from a template (create) and
removes them (destroy) via `community.vmware.vmware_guest`. Idempotent; all
modules run on the control node against vCenter. **Host-centric**: the play
targets the VM hosts themselves and every host provisions ITS OWN VM from its
own vars in single task calls (a hand-authored guests list is not an input).
Hosts build in parallel for free.

## TL;DR

Add the host to inventory (with its `vsphere_vm_*` vars) and run scoped to it —
a no-tag run creates/ensures.

```bash
ansible-playbook -i inventories/<env>/hosts.yml playbooks/vsphere_vm.yml
```

## Requirements

Install collections before running (repo `requirements.yml`, or ad-hoc):

    ansible-galaxy collection install -r requirements.yml

| Collection | When | Used for |
|---|---|---|
| `community.vmware` | always | Clone/reconfigure/destroy (`vmware_guest`), disk, network, tags, single-level folder, power state, guest facts |
| `vmware.vmware` | When `vsphere_vm_provision_via_guestinfo` or a nested folder path | GOSC-free template deploy, `guestinfo.*` advanced settings, multi-level folder creation |
| `community.hashi_vault` | When `vsphere_vm_vault_secret` is set (no `_password`) | vCenter password lookup from Vault |

`community.vmware` **>= 4.x** requires a matching `pyVmomi` **<= 8.0.2.x** on
the controller (pyVmomi 9 removed `VmomiSupport.VmomiJSONEncoder`, which the
modules still call).

## Key variables

Full list: `defaults/main.yml`. Contract: `meta/argument_specs.yml`.

**Required** = value must be correct for a successful run (defaults often work).
**Optional** = safe to leave default / empty; phase stays off or uses built-ins.
**When X** = required only if that feature is on.

`meta/argument_specs.yml` only marks two vars `required: true`; the role's own
preflight asserts (`preflight.yml`, `spec.yml`) require more to actually clone
a working VM — those are marked "When" below.

| Req | Variable | Default | Purpose |
|---|---|---|---|
| **Required** | `vsphere_vm_server` | — | vCenter FQDN (preflight asserts it) |
| **Required** | `vsphere_vm_datacenter` | — | Target datacenter (preflight asserts it) |
| **Required** | `vsphere_vm_template` | — | Source template to clone (`spec.yml` asserts it) |
| When no `_password` | `vsphere_vm_vault_secret` | — | Vault path holding the vCenter password |
| When no Vault secret | `vsphere_vm_password` | `""` | vCenter password directly (bypasses the Vault lookup) |
| When no `_esxi_host` | `vsphere_vm_cluster` | — | Target DRS cluster |
| When no `_cluster` | `vsphere_vm_esxi_host` | — | Standalone ESXi host (also set `vsphere_vm_resource_pool`) |
| Optional | `vsphere_vm_datastore` | `""` | Default datastore for clones |
| Optional | `vsphere_vm_network` | `""` | dvPortGroup for the auto-derived static NIC |
| Optional | `vsphere_vm_cpu` | `2` | vCPU count |
| Optional | `vsphere_vm_memory` | `4096` | Memory in MB |
| Optional | `vsphere_vm_provision_via_guestinfo` | `false` | Clone via cloud-init GuestInfo instead of GOSC (no NIC-disconnect race) |
| Optional | `vsphere_vm_wait_for_ssh` | `false` | Handoff so an on-guest role can follow in the same play |
| When redeploying | `vsphere_vm_force_redeploy` | `false` | **DESTRUCTIVE** — delete guest + disks, rebuild from template |
| When destroying | `vsphere_vm_force_destroy` | `false` | **DESTRUCTIVE** — delete guest when state is `absent` |

## Minimum configuration

```yaml
# group_vars/vsphere_vm_hosts.yml
---
# Required
vsphere_vm_server: service.example.internal
vsphere_vm_datacenter: DC1
vsphere_vm_template: /path/to/compose.yml.j2
```

## Usage

```yaml
# playbooks/vsphere_vm.yml — target the VM hosts; every vCenter call delegates
# to localhost, so the (possibly not-yet-existing) hosts are never SSHed.
- hosts: vmware_vms
  gather_facts: false
  become: false
  roles:
    - role: vsphere_vm
```

Create / ensure:

```bash
ansible-playbook -i inventories/<env>/hosts.yml playbooks/vsphere_vm.yml
```

Destroy (set `vsphere_vm_state: absent` on the host, then):

```bash
ansible-playbook -i inventories/<env>/hosts.yml playbooks/vsphere_vm.yml \
  --tags destroy -e vsphere_vm_force_destroy=true
```

Redeploy (delete → rebuild, then the rest of the play continues):

```bash
ansible-playbook -i inventories/<env>/hosts.yml playbooks/vsphere_vm.yml \
  -e vsphere_vm_force_redeploy=true
```

Chaining an on-guest role in the same play — set
**`vsphere_vm_wait_for_ssh: true`** and the role appends a handoff step: it
polls until the fresh guest answers SSH, gathers its facts, then a same-play
role runs on the guest:

```yaml
- hosts: vmware_vms
  gather_facts: false            # REQUIRED — the VM doesn't exist at play start
  become: true
  roles:
    - role: vsphere_vm
      vars:
        vsphere_vm_wait_for_ssh: true
    - role: storage              # runs ON the guest, as root, once SSH is up
```

```bash
ANSIBLE_HOST_KEY_CHECKING=False ansible-playbook -i inventories/<env>/hosts.yml playbook.yml
```

Tune the wait with `vsphere_vm_ssh_timeout` (default 300s).

## Preconditions

- GuestInfo mode (`vsphere_vm_provision_via_guestinfo: true`) requires a
  GuestInfo-ready template: cloud-init with `datasource_list: [VMware, ...]`,
  `allow_raw_data: true`, `disable_vmware_customization: true` — verify with
  `cloud-init query platform` → `vmware`.
- Chaining an on-guest role (`vsphere_vm_wait_for_ssh: true`) requires the
  template to already bake in the Ansible SSH user + authorized key — this
  role does not inject one.
- vCenter credentials: either `vsphere_vm_password` set directly, or a value
  already present at `vsphere_vm_vault_secret` in Vault.

## Behaviour

- **GOSC vs GuestInfo.** A clone with a vSphere Guest OS Customization spec
  (the default) fires an async first-boot event that drops the new VM's NIC,
  so a naive `wait_for_ip` hangs. The `create` phase handles this with a
  bounded multi-pass poll-and-reconnect over every desired-present VM,
  force-reconnecting any still-disconnected NIC by MAC. Total wait is bounded:
  `vsphere_vm_connect_passes × vsphere_vm_connect_pass_retries ×
  vsphere_vm_connect_pass_delay` seconds (default 8 × 10 × 10 = ~800s max; set
  `vsphere_vm_connect_nics: false` to trust vCenter and skip the phase
  entirely). GuestInfo mode never attaches a customization spec, so the NIC
  never disconnects and the reconnect phase converges on its first pass.
- **Naming.** `vsphere_vm_name` (vCenter object name) and
  `vsphere_vm_hostname` (guest OS hostname) are decoupled and both default to
  `canonical_hostname`, else `inventory_hostname` — override
  `vsphere_vm_name` on a shared vCenter where a clean canonical name isn't
  globally unique.
- **Gateway.** For the auto-derived single NIC: `vsphere_vm_gateway` set →
  used always; else `vsphere_vm_gateway_auto: true` → the first usable
  address of `ansible_host`'s subnet (respects `/25`–`/27`); else no gateway
  (an unrouted data/storage NIC).
- **Multi-NIC (GuestInfo mode).** Every entry in `vsphere_vm_networks`
  becomes its own cloud-init ethernet, keyed by an explicit guest device name
  (defaults follow VMware VMXNET3 slot order: `ens192`, `ens224`, `ens256`,
  …). The first NIC with a gateway (else NIC 0) carries the resolvers:
  ```yaml
  vsphere_vm_networks:
    - {name: "VLAN10-MGT",     interface: ens192, ip: 10.0.10.60, netmask: 255.255.255.0, gateway: 10.0.10.1}
    - {name: "VLAN30-STORAGE", interface: ens224, ip: 10.0.30.60, netmask: 255.255.255.0}
  ```
- Every play host **is** its VM: no group loop, no `hostvars[]` indirection —
  each host builds its own VM from its own vars in single task calls, so
  hosts build in parallel for free.

## Out of scope

- Per-NIC firewalld zones/services/rules — a separate role's job, keyed off
  the interface names this role assigns.
- Injecting the Ansible SSH user/key into the template — the template must
  already have it.
- Guest-level configuration — this role never touches the guest unless
  `vsphere_vm_wait_for_ssh` hands off to a chained role.

## Tag safety

Both destructive phases (`destroy`, `redeploy`) run `identify.yml` first: it
looks the VM up by **name + folder** (the same placement `create` used),
records the **vCenter instance UUID** (not the BIOS UUID, which clones can
duplicate), optionally verifies the resource pool when
`vsphere_vm_resource_pool` is set, then deletes with `uuid` +
`use_instance_uuid: true` — never by bare name. Not found at that placement →
no delete (idempotent). A mismatch on name/folder/pool → identify refuses to
arm the delete.

`--tags redeploy` / `--tags destroy` only narrow *which* roles run in a
daisy-chain play (a vsphere-only pass) — they do not by themselves delete
anything. `vsphere_vm_force_redeploy` / `vsphere_vm_force_destroy` are what
actually arm the destructive phase; both default `false` and must be passed
explicitly (`-e`) — never leave them `true` in inventory.
