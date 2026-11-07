# vsphere_vm

## TL;DR

Host-centric vCenter VM lifecycle: clone from template, ensure, destroy. Each
play host builds **its own** guest from its own vars, so hosts fan out in
parallel. All vCenter calls run on the controller (`delegate_to: localhost`);
the only step that talks to the guest is the optional SSH handoff.

```bash
ansible-playbook -i inventories/<env>/hosts.yml playbooks/site.yml --limit <newhost>
```

## Requirements

```bash
ansible-galaxy collection install -r requirements.yml
```

| Collection | When | Used for |
|---|---|---|
| `community.vmware` | always | clone, existence probe, power, disk grow, NIC reconnect, tags |
| `vmware.vmware` | `vsphere_vm_folder` set, or GuestInfo mode | `folder` (create the path), `deploy_folder_template`, `vm_advanced_settings` |
| `community.hashi_vault` | `vsphere_vm_vault_secret` set and `vsphere_vm_password` empty | resolve the vCenter password |

`community.vmware` 4.x needs `pyVmomi` 8.0.2.x or older — pyVmomi 9 breaks its
JSON encoder.

## Key variables

Full list: `defaults/main.yml`. Contract: `meta/argument_specs.yml`.

**Required** = the run fails without it. **When X** = required only in that mode.

| Req | Variable | Default | Purpose |
|---|---|---|---|
| **Required** | `vsphere_vm_server` | `""` | vCenter FQDN |
| **Required** | `vsphere_vm_datacenter` | `""` | Target datacenter |
| **Required** | `vsphere_vm_template` | `""` | Source template to clone |
| **Required** | `vsphere_vm_vault_secret` **or** `vsphere_vm_password` | `""` | Password source. A non-empty password bypasses the Vault lookup |
| When ensuring | `vsphere_vm_network` **or** `vsphere_vm_networks[].name` | `""` | Portgroup. Preflight refuses to clone a VM with no usable NIC |
| When placement | `vsphere_vm_cluster` **or** `vsphere_vm_esxi_host` | unset | Where the VM lands |
| Optional | `vsphere_vm_enabled` | `true` | Master switch — `false` no-ops the whole role |
| Optional | `vsphere_vm_folder` | `""` | Folder under the DC VM root, no leading slash. Scopes every lookup this role makes |
| Optional | `vsphere_vm_state` | `poweredon` | `poweredon` or `absent` only; anything else is refused in preflight |
| Optional | `vsphere_vm_hardware` | `{num_cpus: 2, memory_mb: 4096}` | Native `hardware` dict; inventory replaces the whole dict |
| Optional | `vsphere_vm_disk` | `[{size_gb: 40, type: thin}]` | Disk list, primary first; sizes are grown to match |
| Optional | `vsphere_vm_wait_for_ip` | `true` | Fail the play when the guest never reports an IPv4 |
| Optional | `vsphere_vm_connect_nics` | `true` | Run the NIC reconnect phase at all |
| Optional | `vsphere_vm_pci_settings` | `{}` | PCI VMX settings; changed values require an offline reconfigure |
| Optional | `vsphere_vm_wait_for_ssh` | `false` | Wait on guest SSH so on-guest roles can chain in the same play |
| Optional | `vsphere_vm_provision_via_guestinfo` | `false` | cloud-init GuestInfo instead of GOSC |
| Optional | `vsphere_vm_gateway` / `_gateway_auto` | unset / `false` | Explicit gateway, or derive the first usable address |
| **Required** | `vsphere_vm_tag_defaults` + `vsphere_vm_tags` | `{}` | `{Category: tag}`, host map wins. Together they must cover `vsphere_vm_tags_required` or preflight refuses the build |
| Optional | `vsphere_vm_tags_required` | six categories | `Environment, Site, OS, Service, PatchWave, RebootPolicy` |
| Optional | `vsphere_vm_tag_taxonomy` | `{}` | When set, preflight also rejects categories and values it does not list |
| When destructive | `vsphere_vm_force_redeploy` / `vsphere_vm_force_destroy` | `false` | Arm the destructive action. Pass via `-e` only |

Shared values go in `group_vars/`; per-host values (`ansible_host`, VM name,
extra disk) go in `host_vars/`.

## Usage

```yaml
- hosts: vmware_vms
  gather_facts: false
  become: false
  roles:
    - vsphere_vm
```

```bash
# ensure (idempotent — reconciles an existing VM, does not rebuild it)
ansible-playbook -i inventories/<env>/hosts.yml playbook.yml

# destroy
ansible-playbook -i inventories/<env>/hosts.yml playbook.yml \
  -e vsphere_vm_state=absent -e vsphere_vm_force_destroy=true

# wipe + rebuild (play continues after)
ansible-playbook -i inventories/<env>/hosts.yml playbook.yml \
  -e vsphere_vm_force_redeploy=true
```

Chain an on-guest role in the same play:

```yaml
- hosts: vmware_vms
  gather_facts: false
  roles:
    - role: vsphere_vm
      vars: {vsphere_vm_wait_for_ssh: true}
    - role: storage
      vars: {storage_provision: true}
```

```bash
ANSIBLE_HOST_KEY_CHECKING=False ansible-playbook -i inventories/<env>/hosts.yml playbook.yml
```

## Preconditions

- The template exists in the target datacenter and matches the checklist below.
- With `vsphere_vm_wait_for_ssh: true`, the template bakes the Ansible user and
  its public key, and the play sets `gather_facts: false` — facts are gathered by
  the role after the guest answers.
- `vsphere_vm_gateway_auto` only works with a `255.255.255.x` netmask. The
  first-usable formula is last-octet arithmetic, so preflight refuses shorter
  prefixes rather than emitting a wrong gateway.

## Behaviour

1. **Phases run in order**: preflight → spec → plan → *[redeploy]* → create →
   connect → *[handoff]* → *[destroy]*. `plan` is inventory-only and prints the
   full build plan for every host before anything is touched.
2. **Ensure is idempotent, and the existence probe fails closed.** Create probes
   for the VM by name **and folder**; a probe error is never read as "absent"
   (that would attempt a second clone), and a probe result whose name or folder
   does not match inventory aborts rather than reconciling the wrong object.
   Existing VMs take a reconcile path — no re-clone, no power cycle. CPU and
   memory changes reach an existing VM only when it is found powered off, so
   shut the guest down, then run ensure; it applies the hardware and powers on.
3. **Deletes are pinned to an instance UUID**, resolved by name + folder in
   `identify.yml`. Same-name VMs in other folders cannot be selected. When
   `vsphere_vm_resource_pool` is set, the pinned UUID's live pool is re-read and
   must match, or the delete is refused. A moved VM reads as absent — update
   `vsphere_vm_folder`.
4. **GOSC creates are staged**: clone powered off → assert every vNIC
   start-connected → power on. A one-call clone + power-on orphans the NIC.
5. **NIC reconnect is bounded and fails closed.** It is defense-in-depth for VMs
   left disconnected by older runs or power events; an IP pre-check
   short-circuits healthy VMs. Every pass nests a NIC-gather loop and an IP-poll
   loop, so the worst case is `connect_passes × (gather + poll)` — 6 minutes on
   the defaults, tunable via `vsphere_vm_connect_passes`,
   `_connect_gather_retries`, `_connect_gather_delay`, `_connect_pass_retries`,
   `_connect_pass_delay`. Every lookup is folder-scoped, and a vCenter lookup
   that *fails* aborts in about 10 seconds instead of retrying — retrying cannot
   fix a lookup error, and treating it as "no IP yet" burned the whole budget.
6. **`vsphere_vm_wait_for_ip` governs how long, not whether.** Left `true`
   (default), a guest that never reports an IPv4 fails the play instead of going
   green. Set `false` and the NICs are still healed — exactly one pass, no IP
   poll, no assert.
7. **GuestInfo mode** (`vsphere_vm_provision_via_guestinfo: true`): no GOSC,
   cloud-init applies the network from `guestinfo.metadata`. The metadata is
   injected on first create only, so re-running ensure neither rewrites
   cloud-init nor reboots the guest. Needs a GuestInfo-ready template
   (`cloud-init query platform` reports `vmware`).
8. **The gateway is never guessed**: explicit `vsphere_vm_gateway` → `gateway_auto`
   first-usable → omitted entirely (unrouted).
9. **`wait_for_ssh` is the only guest-facing step.** Everything else is
   controller → vCenter.

## Tag safety

Destructive actions are **variable**-gated, not `never`-tagged. `--tags redeploy`
or `--tags destroy` only narrows which phases run; without
`vsphere_vm_force_redeploy` / `vsphere_vm_force_destroy` the run stays an
ensure and says so. Leave both `false` in inventory and pass them with `-e`.

Available tags: `vsphere_vm`, `preflight`, `plan`, `create`, `connect`,
`redeploy`, `handoff`, `destroy`, `vm_tags`. Preflight, spec and plan additionally carry
`always`, so a tagged-subset run still resolves the credential and the guest
spec that every other phase reads.

## Template checklist

1. Guest OS type in the GOSC matrix (`rhel9_64Guest` for Alma/RHEL 9 on HW 19 —
   not `other5xLinux64Guest`)
2. `open-vm-tools` and `perl` installed
3. Network on by default (`network --onboot=yes`)
4. With cloud-init: set `disable_vmware_customization: true`. GuestInfo mode also
   needs `datasource_list: [VMware, OVF, None]`
5. Seal it: truncate machine-id, drop host keys, clear persistent-net rules

## Out of scope

- DNS records, firewalld zones, and whole-inventory vCenter discovery.
- Anything inside the guest beyond waiting for SSH — chain an on-guest role.
