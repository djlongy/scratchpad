# vsphere_vm

## TL;DR

Portable vCenter **VM lifecycle** role — clones VMs from a template (create)
and removes them (destroy) via `community.vmware.vmware_guest`. Idempotent
and **host-centric**: the play targets the VM hosts themselves and every host
provisions ITS OWN VM from its own vars in single task calls (no
hand-authored guests list); hosts build in parallel for free. For
declarative, state-file-managed fleets prefer Terraform — this role is for
Ansible-native, inventory-driven create/destroy (e.g. spinning up SOE
desktops to then hand to `baseline`).

```bash
# ensure / create (add the host to inventory with its vsphere_vm_* vars first)
ansible-playbook -i inventories/<env>/hosts.yml playbooks/site.yml --limit <newhost>
```

## Requirements

Install collections before running (repo `requirements.yml`, or ad-hoc):

    ansible-galaxy collection install -r requirements.yml

| Collection | When | Used for |
|---|---|---|
| `community.vmware` | always | clone/reconfigure, tags, categories, power state, disk/network |
| `vmware.vmware` | When a nested folder path is set, or `vsphere_vm_provision_via_guestinfo: true` | official folder module — creates the whole nested folder path in one call (the `community.vmware` folder module is single-level only); also the GOSC-free clone + `guestinfo.*` advanced settings under guestinfo mode |
| `community.hashi_vault` | When `vsphere_vm_vault_secret` is set | vCenter password lookup (preflight) |

`community.vmware` **>= 4.x** needs a matching `pyVmomi` **<= 8.0.2.x** on the
controller — pyVmomi 9 removed `VmomiSupport.VmomiJSONEncoder`, which the
modules still call.

## Key variables

Full list: `defaults/main.yml`. Contract: `meta/argument_specs.yml` (marks
only `vsphere_vm_server` and `vsphere_vm_datacenter` `required: true`; the
role's own preflight/spec asserts require more to actually clone a VM — see
below).

**Required** = value must be correct for a successful run (defaults often work).
**Optional** = safe to leave default / empty; phase stays off or uses built-ins.
**When X** = required only if that feature/path is in use.

| Req | Variable | Default | Purpose |
|---|---|---|---|
| **Required** | `vsphere_vm_server` | none | vCenter FQDN (preflight asserts it) |
| **Required** | `vsphere_vm_datacenter` | none | Target datacenter (preflight asserts it) |
| **Required** | `vsphere_vm_template` | none | Source template to clone (`spec.yml` asserts it) |
| When credentials | `vsphere_vm_vault_secret` (+ `vsphere_vm_username`, `vsphere_vm_password_field`) **or** `vsphere_vm_password` | `administrator@vsphere.local` / `admin_password` / `""` | Preflight asserts a password source — Vault path or direct password |
| When placement | `vsphere_vm_cluster` **or** `vsphere_vm_esxi_host` (+ `vsphere_vm_resource_pool` for standalone hosts) | none / `Resources` | Where the VM lands |
| Optional | `vsphere_vm_datastore` | `""` | Default datastore for the clone |
| Optional | `vsphere_vm_network` / `vsphere_vm_netmask` / `ansible_host` | `""` / `255.255.255.0` / n/a | Auto-derived static NIC — dvPortGroup, netmask, and the guest's static IP |
| Optional | `vsphere_vm_dns` | `[]` | DNS servers for guest customization |
| Optional | `vsphere_vm_hardware` | `{num_cpus: 2, memory_mb: 4096}` | Native `vmware_guest` `hardware` dict (module shape 1:1) |
| Optional | `vsphere_vm_disk` | `[{size_gb: 40, type: thin}]` | Native `vmware_guest` disk list, primary first |
| Optional | `vsphere_vm_wait_for_ssh` | `false` | Chain an on-guest role in the same play (waits for SSH + gathers facts) |
| Optional | `vsphere_vm_provision_via_guestinfo` | `false` | Provision via cloud-init GuestInfo instead of GOSC (no NIC-disconnect race) |
| When destructive | `vsphere_vm_force_redeploy` / `vsphere_vm_force_destroy` | `false` / `false` | Arms wipe+rebuild / delete — pass via `-e`, never leave `true` in inventory |

Put values shared by all VMs (datacenter, datastore, network, gateway, dns,
domain, default cpu/mem/disk, template) in `group_vars/<group>.yml`; put
genuinely per-host values (the IP `ansible_host`, name, a bigger disk, a
different template) in `host_vars/<host>.yml`.

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
# playbook.yml — target the VM hosts; every vCenter call delegates to localhost,
# so the (possibly not-yet-existing) hosts are never SSHed.
- hosts: vmware_vms
  gather_facts: false
  become: false
  roles:
    - vsphere_vm
```

Create / ensure:

```bash
ansible-playbook -i inventories/<env>/hosts.yml playbook.yml
```

Destroy (set `vsphere_vm_state: absent` on the host, then):

```bash
ansible-playbook -i inventories/<env>/hosts.yml playbook.yml \
  -e vsphere_vm_force_destroy=true
# vsphere-only scope
ansible-playbook -i inventories/<env>/hosts.yml playbook.yml \
  --tags destroy -e vsphere_vm_force_destroy=true
```

Chaining an on-guest role (e.g. `storage`) in the same play:

```yaml
- hosts: vmware_vms
  gather_facts: false            # REQUIRED — the VM doesn't exist at play start
  become: true                   # for storage; vsphere_vm forces become:false on its own tasks
  roles:
    - role: vsphere_vm
      vars:
        vsphere_vm_wait_for_ssh: true
    - role: storage              # runs ON the guest, as root, once SSH is up
      vars:
        storage_provision: true
```

```bash
ANSIBLE_HOST_KEY_CHECKING=False \
ansible-playbook -i inventories/<env>/hosts.yml playbook.yml
```

## Preconditions

- vCenter reachable and credentials valid (Vault path or `vsphere_vm_password`).
- `vsphere_vm_template` must already exist in vCenter.
- GuestInfo provisioning mode needs a GuestInfo-ready template (Packer-baked):
  cloud-init with `datasource_list: [VMware, OVF, None]`, `allow_raw_data:
  true`, and `disable_vmware_customization: true`. Verify on a built host
  with `cloud-init query platform` → must return `vmware` (else it silently
  DHCPs).
- Standalone-host + GuestInfo: if the ESXi host's resource pool name is
  ambiguous, set `vsphere_vm_resource_pool` to the host's ROOT pool MOID
  (`govc object.collect -s /DC/host/<h> resourcePool`) instead of the name —
  the GOSC path (`vmware_guest`) wants the name, `deploy_folder_template`
  wants the MOID on an ambiguous host.
- Chaining `vsphere_vm_wait_for_ssh: true`: the template must already bake in
  the ansible SSH user + authorized key (the role does not inject one), and
  host-key checking must be off for the brand-new host
  (`ANSIBLE_HOST_KEY_CHECKING=False`) — else `wait_for_connection` just times
  out. Tune with `vsphere_vm_ssh_timeout` (default 300s).

## Behaviour

- **Destructive actions are var-gated, not `never`-tagged.** `-e
  vsphere_vm_force_redeploy=true` wipes and rebuilds the VM, then the **rest
  of the play continues**; add `--tags redeploy` to scope the rebuild to
  vsphere_vm alone (later roles tag-skip, so the guest comes back a bare
  template until a full or config run follows). `-e
  vsphere_vm_force_destroy=true` (with `vsphere_vm_state: absent`) removes
  the guest; add `--tags destroy` to scope it. Delete is idempotent, so
  redeploy also works on a host whose VM doesn't exist yet.
- **Delete is targeted by vCenter instance UUID, never by bare name.** Both
  destructive phases run `identify.yml` first: it looks the VM up by
  **name + folder** (multi-tenant-safe — a same-named VM under another
  folder can't be selected), records the instance UUID (not the BIOS UUID,
  which clones/imports can duplicate), and optionally verifies the resource
  pool. A VM *moved* out of its inventory folder is treated as missing
  (update `vsphere_vm_folder` rather than falling back to a name search); any
  name/folder/pool mismatch hard-fails instead of arming a delete.
- **GOSC creates are STAGED so the NIC never orphans.** `vmware_guest`'s
  one-call clone+customize+power-on creates the vNIC
  `connected=false/startConnected=false` (the flags in `networks:` do not
  survive that path), and a NIC that enters FIRST power-on disconnected is
  never reconnected by any layer — vpxd only restores NICs it disconnected
  itself, and the guest's deployPkg `enable-nics` either no-ops (guestId
  outside the GOSC matrix) or holds only until vCenter reasserts its stale
  device state after the GOSC reboot. `create.yml` therefore clones powered
  OFF with the GOSC spec, asserts every vNIC connected+start-connected while
  off, then powers on — the NIC stays connected through customization and the
  GOSC reboot (live-proven 5/5, ~80–90 s to IP, on correct and wrong template
  guestIds; findings.md on branch `worktree-nic-reconnect-fable`).
  `connect.yml`'s bounded multi-pass poll-and-reconnect
  (`vsphere_vm_connect_passes` × `vsphere_vm_connect_pass_retries` ×
  `vsphere_vm_connect_pass_delay`, default 8 × 10 × 10 ≈ 800s max) remains as
  defense-in-depth — it short-circuits on its IP pre-check and normally only
  waits for the guest to finish booting. Set `vsphere_vm_connect_nics: false`
  to skip it and trust vCenter instead.
- **GuestInfo mode retires the reconnect race.**
  `vsphere_vm_provision_via_guestinfo: true` clones powered-off with no
  `networks:`/`customization:` (zero GOSC), sets each vNIC natively with
  `vmware_guest_network`, then powers on — cloud-init's `vmware` datasource
  applies the static hostname + network on first boot, and `connect.yml`
  converges on its first pass. A `networks:` entry with `type: dhcp` (or no
  `ip`) falls back to `dhcp4: true` for that NIC only; static and DHCP NICs
  can mix.
- **Gateway is never guessed.** For the auto-derived single NIC: an explicit
  `vsphere_vm_gateway` always wins; else `vsphere_vm_gateway_auto: true`
  derives the subnet's first usable address from `ansible_host` +
  `vsphere_vm_netmask`; else no gateway is set (an unrouted data/storage
  NIC).
- **`vsphere_vm_wait_for_ssh: true` is the only guest-touching step.** Every
  other phase runs on the controller (`delegate_to: localhost`) against
  vCenter and never SSHes the guest. With it set, the role appends a final
  poll for guest SSH + a facts gather so an on-guest role can follow in the
  same play.

## The NIC-disconnect issue, in plain terms

**Problem.** A VM cloned from a template with guest customization (static IP /
hostname) boots with its network adapter disconnected and never gets an IP.
Forcing `connected: true` / `start_connected: true` in the clone call does not
help — those flags are lost on that path.

**Why.** `community.vmware.vmware_guest` attaches a customization spec to every
template clone (even without a `customization:` block) and creates the adapter
disconnected. vSphere only auto-reconnects adapters that *it* disconnected at
power-on; an adapter that was already disconnected before first power-on belongs
to nobody, so nothing ever reconnects it. The in-guest reconnect attempt by
VMware Tools either does nothing (template guest OS type not in the
customization support matrix) or is reverted by vCenter about 10 s after the
customization reboot.

**Sources.**
- Ansible: [ansible/ansible#45834](https://github.com/ansible/ansible/issues/45834)
- Terraform (same behaviour): [terraform-provider-vsphere#388](https://github.com/hashicorp/terraform-provider-vsphere/issues/388)
- Guest-side reconnect internals: [open-vm-tools#208](https://github.com/vmware/open-vm-tools/issues/208)
- vSphere NIC-disconnect flow: [Broadcom Web Services SDK — Disconnecting Virtual NICs](https://techdocs.broadcom.com/us/en/vmware-cis/vsphere/vsphere-sdks-tools/7-0/web-services-sdk-programming-guide/virtual-machine-guest-operations/guest-network-customization-for-instant-clone-virtual-machines/disconnecting-virtual-nics.html)

**How this role fixes it.** New VMs are built in three steps instead of one:
clone **powered off** (customization spec attached), set every adapter
*Connected* + *Connect at power on* while the VM is still off, then power on.
The adapter is live from the first boot onward and is never disconnected —
static IP in ~80–90 s, no reconnect loop needed. Existing VMs are reconciled
normally and never power-cycled.

## What the role expects from a template

Build the template so a clone can customize and connect on first boot:

1. **Correct guest OS type** (vCenter "Guest OS" / Packer `vm_guest_os_type`).
   Never `other5xLinux64Guest` — it is outside the customization support matrix.
   For AlmaLinux/RHEL 9 on hardware version 19 use `rhel9_64Guest`; the native
   `almalinux_64Guest` type requires hardware version 20
   ([packer-examples#383](https://github.com/vmware-samples/packer-examples-for-vsphere/issues/383)).
2. **VMware tooling packages** — kickstart `%packages` or
   `dnf install open-vm-tools perl` (open-vm-tools ≥ 12 carries the deployPkg
   customization plugin; perl is required by the classic Linux customization
   scripts).
3. **Network up by default** — kickstart `network --onboot=yes`; NetworkManager
   enabled (RHEL-family default).
4. **cloud-init (only if installed):** exactly one
   `disable_vmware_customization: true` line in `/etc/cloud/cloud.cfg` (delete
   duplicates — a stray `false` makes tools and cloud-init fight over
   customization). For GuestInfo mode also set
   `datasource_list: [ VMware, OVF, None ]` and run `cloud-init clean --logs
   --seed` when sealing.
5. **Seal before templating** — truncate `/etc/machine-id`, remove
   `/etc/ssh/ssh_host_*`, clear persistent-net rules.
6. **Template adapter left "Connect at power on"** (the packer default; the
   role re-asserts it per clone anyway).

Estate note: `linux-almalinux-9.7-main` still carries `other5xLinux64Guest` and
a duplicated `disable_vmware_customization` — both fixed in the packer vars
(`config/lidcombe/linux-almalinux-9.pkrvars.hcl`); rebuild the template to pick
them up. The staged create works even on the unfixed template.

## Out of scope

- DNS records for the guest — not created by this role.
- Per-NIC firewalld zones/services/rules — the role provisions the vNICs +
  IPs only; a `firewalld`/`baseline` role handles guest-side rules, keyed off
  the same interface names.
- Whole-inventory vCenter discovery — the plan is purely inventory-derived;
  the role never gathers vCenter's inventory to check whether a VM exists
  before cloning.
