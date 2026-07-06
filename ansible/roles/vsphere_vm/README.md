# vsphere_vm

Portable vCenter **VM lifecycle** role — clone VMs from a template (create) and
remove them (destroy) via `community.vmware.vmware_guest`. Idempotent; all
modules run on the control node against vCenter. **Host-centric**: the play
targets the VM hosts themselves and every host provisions ITS OWN VM from its
own vars in single task calls (a hand-authored guests list is not an input).
Hosts build in parallel for free.

> For declarative, state-file-managed fleets prefer Terraform. This role is for
> Ansible-native, inventory-driven create/destroy (e.g. spinning up SOE desktops
> to then hand to `baseline`).

## Requirements

- `community.vmware` **>= 6.x** + `pyvmomi` on the controller (4.x is incompatible with pyVmomi 9).
- vCenter credentials (Vault path or `vsphere_vm_password`).

## Phases (tags)

| Tag | Runs |
|---|---|
| `preflight` | assert inputs + resolve the vCenter password |
| `create` | clone/ensure guests whose `state` ≠ `absent` |
| `destroy` | remove guests whose `state` = `absent` — **`never` tag**, needs `vsphere_vm_allow_destroy=true` |

A no-tag run creates/ensures; destroy is opt-in only.

## Robust spin-up (NIC reconnect + bounded waits)

After a clone+customize, vSphere frequently clears the new NIC's "connect at
power on" (a long-standing bug), so the guest boots with a dead NIC and never
gets an IP — a naive `wait_for_ip` then hangs for 20+ minutes. The `create`
phase handles this automatically:

1. the clone itself does **not** wait for an IP (`wait_for_ip_address: false`),
   so a disconnected NIC can't block it;
2. a bounded **multi-pass** poll-and-reconnect (the `connect` phase) then runs over
   every desired-present VM: each pass force-reconnects every still-disconnected
   NIC by MAC (multi-NIC safe, wrapped in a rescue) and briefly polls for an IP —
   whichever pass lands just after the async guest customization finishes makes
   the reconnect stick;
3. the total wait is **bounded** — `connect_passes × connect_pass_retries ×
   connect_pass_delay` seconds (default 8 × 5 × 10 = ~400 s max) — and reports a
   clear message instead of hanging if a NIC is genuinely misconfigured.

| Var | Default | Purpose |
|---|---|---|
| `vsphere_vm_connect_nics` | `true` | reconnect NICs after build |
| `vsphere_vm_connect_passes` | `8` | reconnect+poll cycles before giving up |
| `vsphere_vm_connect_pass_retries` | `5` | IP polls per pass |
| `vsphere_vm_connect_pass_delay` | `10` | seconds between polls within a pass |
| `vsphere_vm_create_retries` | `3` | retry transient vCenter errors |
| `vsphere_vm_create_delay` | `15` | seconds between create retries |

> Needs `community.vmware` ≥ 4 with a matching `pyVmomi` (≤ 8.0.2.x — newer
> pyVmomi removed `VmomiSupport.VmomiJSONEncoder` that the modules still call).

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

```yaml
# group_vars / play vars
vsphere_vm_server: "vcenter.example.com"
vsphere_vm_vault_secret: "kv/data/platform/vsphere/vcenter/runtime"
vsphere_vm_datacenter: "Datacenter"
vsphere_vm_esxi_host: "192.0.2.11"
vsphere_vm_resource_pool: "/Datacenter/host/192.0.2.11/Resources"
vsphere_vm_datastore: "datastore1"
```

Create / ensure:

```bash
ansible-playbook -i inventories/<env>/hosts.yml playbook.yml
```

Destroy (set `vsphere_vm_state: absent` on the host, then):

```bash
ansible-playbook -i inventories/<env>/hosts.yml playbook.yml \
  --tags destroy -e vsphere_vm_allow_destroy=true
```

## Host-centric (the only model)

Each play host **is** its VM: the host builds its own VM from its own vars in
single task calls — no group loop, no `hostvars[]` indirection. Vars resolve in
the host's own context (inline inventory + host_vars + group_vars merged), so
placement is your choice. The plan is **purely inventory-derived** — the role does
**not** gather vCenter's inventory to test existence (that whole-inventory
`vmware_vm_info` read was slow and told us nothing the idempotent modules can't
resolve themselves, cheaper, by name). It goes straight to an idempotent create
that reconciles an existing VM as a fast per-VM no-op; each host prints its own
plan line (ENSURE PRESENT / DESTROY).

**Where to put the vars:**

| Value | Put it in | Why |
|---|---|---|
| Shared by all the VMs (datacenter, datastore, network, gateway, dns, domain, default cpu/mem/disk, template) | **`group_vars/<group>.yml`** | DRY — set once for the group |
| Genuinely per-host (the IP `ansible_host`, name, a bigger disk, a different template) | **`host_vars/<host>.yml`** (or inline beside the host) | one place per unique value |

Inline-everything in `hosts.yml` works for a handful of VMs, but at scale it
mixes topology with config and repeats shared values — prefer **group_vars for
the common spec + minimal host_vars for the uniques** (usually just the IP).

```yaml
# group_vars/vmware_vms.yml — the common spec
vsphere_vm_template: linux-almalinux-9-main
vsphere_vm_hardware:            # native vmware_guest dict (replaces the role default)
  num_cpus: 2
  memory_mb: 4096
vsphere_vm_network: "VLAN10-SVC"
vsphere_vm_dns: [192.0.2.53]
domain: example.com

# host_vars/web01.yml (or inline) — just the unique bits
ansible_host: 192.0.2.50        # becomes the VM's static IP
```

Per-host `vsphere_vm_networks` / `vsphere_vm_customization` override the
auto-derived NIC/customization entirely when you need something custom.

## Drop-in mapping for exported host_vars

If your hosts already carry bare exported vars — `cpus`, `memory` (MB), `disks`,
`networks`, `folder`, `template`, plus customisation `domain` / `hostname` /
`dns_server_list` — bind them to the role **once** in the group's group_vars.
Group_vars templates resolve per host, so each host's values flow through and
the role is a drop-in replacement (no per-host edits):

```yaml
# group_vars/<vm_group>.yml — marry the exports to the role
vsphere_vm_template: "{{ template }}"
vsphere_vm_folder: "{{ folder }}"
vsphere_vm_hardware:
  num_cpus: "{{ cpus }}"
  memory_mb: "{{ memory }}"
vsphere_vm_disk: "{{ disks }}"
vsphere_vm_networks: "{{ networks }}"
vsphere_vm_customization:
  hostname: "{{ hostname | default(inventory_hostname) }}"
  domain: "{{ domain }}"
  dns_servers: "{{ dns_server_list }}"
```

`disks` / `networks` must already be vmware_guest-shaped lists
(`[{size_gb, type}, ...]` / `[{name, type, ip, netmask, gateway}, ...]`) — they
pass through 1:1.

## Canonical hostnames (messy inventory keys → clean names)

If your inventory keys are awkward for Linux/vCenter (e.g. `controller_1`,
`prod_controller_1`), compute a canonical name in group_vars and feed it to the
role. It lowercases, turns non-alphanumerics into hyphens, and strips a
leading/trailing **environment token** so the env isn't doubled (the FQDN already
carries it as a subdomain).

```yaml
# group_vars/all.yml
base_domain: "example.com"          # env is set per-env group_vars (prod/dev/test/management)
canonical_env_tokens: ["{{ env }}", prod, dev, test, management, mgmt, mgt]
canonical_hostname: >-
  {{ inventory_hostname | lower
     | regex_replace('[^a-z0-9]+', '-')
     | regex_replace('^(' ~ (canonical_env_tokens | unique | join('|')) ~ ')-', '')
     | regex_replace('-(' ~ (canonical_env_tokens | unique | join('|')) ~ ')$', '')
     | regex_replace('^-+', '') | regex_replace('-+$', '') }}
canonical_domain: "{{ env }}.{{ base_domain }}"
canonical_fqdn: "{{ canonical_hostname }}.{{ canonical_domain }}"

# group_vars/<vm_group>.yml — feed it to the role
vsphere_vm_name: "{{ canonical_hostname }}"   # vCenter VM name + guest hostname
domain: "{{ canonical_domain }}"              # guest customization FQDN suffix
```

| inventory key (env=prod) | VM name / hostname | FQDN |
|---|---|---|
| `controller_1` | `controller-1` | `controller-1.prod.example.com` |
| `prod_controller_1` | `controller-1` | `controller-1.prod.example.com` |
| `management_db_2` | `db-2` | `db-2.prod.example.com` |

The **`baseline`** role consumes the same `canonical_hostname` / `canonical_fqdn`
for the OS hostname + `/etc/hosts`. (Falls back to `inventory_hostname` when the
pattern isn't in use.)

## Multi-tenant vCenter naming

On a **shared vCenter**, a clean canonical name isn't enough: if every tenant
names their VM `freeipa`, vCenter shows `freeipa`, `freeipa (1)`, `freeipa (2)` —
ambiguous. So the role **decouples** the two names:

| | comes from | value |
|---|---|---|
| **vCenter VM name** (`vsphere_vm_name`) | tenant-scoped, globally unique | `acme-prod-freeipa-01` |
| **guest OS hostname** (`vsphere_vm_hostname`) | clean / canonical | `freeipa-01` |

```yaml
# group_vars/all.yml
tenant: "acme"                                    # your tenancy identifier
# ... canonical_hostname / canonical_domain as above ...

# group_vars/<vm_group>.yml
vsphere_vm_name: "{{ tenant }}-{{ env }}-{{ canonical_hostname }}"  # vCenter: acme-prod-freeipa-01
vsphere_vm_hostname: "{{ canonical_hostname }}"                     # OS:     freeipa-01
domain: "{{ canonical_domain }}"                                   # FQDN:   freeipa-01.prod.example.com
# organise the inventory tree per tenant/env too:
vsphere_vm_folder: "/Datacenter/vm/{{ tenant }}/{{ env }}"
```

Result for inventory key `freeipa_1` (tenant `acme`, env `prod`):

- vCenter VM: **`acme-prod-freeipa-1`** in folder `/…/acme/prod` — unique across
  tenants, and you can read tenant + env + host straight off the name.
- OS hostname: **`freeipa-1`**; FQDN **`freeipa-1.prod.example.com`** — clean,
  no tenant noise inside the guest.

Tune the vCenter name to taste (add site/datacenter, use dots, etc.) — it's just
`vsphere_vm_name`; only the guest-side `vsphere_vm_hostname` stays canonical.

## Connection

Credentials come from Vault (`vsphere_vm_vault_secret` +
`vsphere_vm_password_field`) unless `vsphere_vm_password` is set directly.
`vsphere_vm_validate_certs` defaults to `false` for self-signed lab vCenters.

## Placement

Use **either** `vsphere_vm_cluster` (DRS cluster) **or** `vsphere_vm_esxi_host`
(standalone host). For standalone hosts also set `vsphere_vm_resource_pool` to
that host's default pool ("Resources").

## Known vSphere quirk (handled — but see the permanent fix)

Clone **with a vSphere Guest OS Customization (GOSC) spec** (`customization:`) toggles the new
VM's NIC **disconnected** during the guest's async first-boot customization, so it never gets an
IP and `wait_for_ip` hangs. Forcing `connected/start_connected: true` at create time is necessary
but **not sufficient** — GOSC reverts it after the clone returns. That is why `connect.yml` runs a
bounded **multi-pass** reconnect over all managed VMs (the pass that lands just after customization
finishes makes it stick). If you still see a disconnected NIC: `govc device.connect -vm <name> ethernet-0`.

**Root cause references:** Broadcom KB 425280, terraform-provider-vsphere#388, cloud-init VMware datasource docs.
The durable cure — **now implemented** as `vsphere_vm_provision_via_guestinfo` (see next section) —
is to stop using GOSC and drive first-boot config via the cloud-init VMware GuestInfo datasource.

## Provisioning mode: cloud-init GuestInfo (recommended — retires the reconnect race)

Set `vsphere_vm_provision_via_guestinfo: true` (e.g. in `group_vars/all.yml` for the inventory) and
the role provisions with **zero GOSC**, so the NIC never disconnects — no reconnect race, `connect.yml`
converges on its first pass. It reads the **same per-host fields** as the default path (nothing new
to specify for the common case):

```yaml
# group_vars/<vm_group>.yml
vsphere_vm_provision_via_guestinfo: true      # the only change vs the GOSC default
```

How it stays all-native (no `govc`): `create.yml` (1) clones **powered-off with no `networks:` and no
`customization:`** — so `vmware_guest` attaches no GOSC spec — injecting the rendered cloud-init metadata
as `guestinfo.metadata`; (2) sets each vNIC's portgroup + `start_connected` with `vmware_guest_network`
(also no GOSC); (3) powers on. The NIC is live at first boot, so cloud-init's `vmware` datasource applies
the static hostname + network. Verified end-to-end (NIC `Connected: true` throughout, exact static IP,
`cloud-init query platform` → `vmware`).

### Dual-mode specifics (post-synthesis)

- **Per-host mode choice** — set `vsphere_vm_provision_via_guestinfo` in a host's vars. One
  inventory can mix GOSC and GuestInfo hosts; unset falls back to the role-wide (group) flag.
- **Clone engine** — GuestInfo mode clones with `vmware.vmware.deploy_folder_template`, which has NO
  customization surface (GOSC-proof by construction; `vmware_guest` attaches a GOSC spec to template
  clones even without `customization:`). Hardware/disk/guestinfo are applied by a follow-up plain
  reconfigure. Requires the `vmware.vmware` collection (already in requirements.yml).
- **Standalone-host gotcha** — `deploy_folder_template` resolves `vsphere_vm_resource_pool` by name
  OR MOID; if the host has an ambiguous pool name (nested "Resources"), set the host ROOT pool MOID
  (e.g. `resgroup-123`, via `govc object.collect -s /DC/host/<h> resourcePool`). The GOSC path
  (`vmware_guest`) wants the NAME — keep both notes in mind when mixing modes.
- **Per-NIC DHCP fallback** — a `networks:` entry with `type: dhcp` (or no `ip`) renders
  `dhcp4: true` for that NIC only; static and DHCP NICs can mix in one guest.
- **Both modes live-verified** (AlmaLinux 9.8, 2026-07): GOSC auto-reconnects (~30s) when the
  template guestId is correct (`rhel9_64Guest` — "other*" guestIds break the reconnect); GuestInfo
  shows zero customization events with the NIC connected throughout.

**Requires a GuestInfo-ready template** (Packer-baked): cloud-init with `datasource_list: [VMware, OVF,
None]`, `allow_raw_data: true`, and `disable_vmware_customization: true`. Verify on a built host with
`cloud-init query platform` → must return `vmware` (else it silently DHCPs — and every lab VLAN has
DHCP, so that means an IP-conflict risk; pick target IPs not already allocated in inventory).

**Multi-NIC** — every entry in a guest's `networks` becomes its own cloud-init ethernet, keyed by an
**explicit** guest device name (cloud-init's RHEL renderer ignores netplan `match:` globs). Defaults follow
VMware VMXNET3 slot order (`ens192`, `ens224`, `ens256`, … in list order); override per NIC with `interface:`.
The first NIC with a gateway (else NIC 0) carries the resolvers.

```yaml
# host_vars/monster-01.yml — a (deliberately gnarly) 3-NIC guest
vsphere_vm_networks:
  - {name: "VLAN10-MGT",     interface: ens192, ip: 192.0.2.60, netmask: 255.255.255.0, gateway: 192.0.2.1}
  - {name: "VLAN30-STORAGE", interface: ens224, ip: 10.0.30.60,    netmask: 255.255.255.0}   # data plane, no gateway
  - {name: "VLAN40-ACCESS",  interface: ens256, ip: 10.0.40.60,    netmask: 255.255.255.0}   # ingress plane
```

`vsphere_vm` provisions the vNICs + IPs; **per-NIC firewalld zones/services/rules are a `firewalld`/
`baseline` role's job** (guest-side, post-provision), keyed off these interface names — not this role.

Optional: set a guest's `guestinfo_userdata` to a raw `#cloud-config` string to also inject
`guestinfo.userdata` (packages, users, an authoritative resolv.conf, …).

## vCenter tags & nested folders

Created VMs can be **placed in a nested folder tree** and **tagged** in the same run
(both provisioning modes):

- **Folders** — `vsphere_vm_folder` (host/group scope) may be a nested path, e.g.
  `/Datacenter/vm/prod/app`. The role creates the whole tree in one call
  (`vmware.vmware.folder`) before placing the VM, so intermediate folders need not
  pre-exist.
- **Tags** — set `vsphere_vm_tags` (host/group scope) as a
  `{Category: Tag}` map, e.g. `{Tenant: prod, Environment: alma}`. The role ensures each
  category (single-cardinality) and tag exists, then associates them with the VM.

```yaml
vsphere_vm_folder: "/Datacenter/vm/prod/app"
vsphere_vm_tags: {Tenant: prod, Environment: alma}
```

## Variables

See `defaults/main.yml` for the full surface. Everything is a per-host/group
`vsphere_vm_*` var (hardware, disk, folder, datastore, networks, customization,
state, tags, wait_for_ip) resolved in each host's own context — normal Ansible
precedence, so a host/group value replaces the role default wholesale. The
hardware/disk/networks/customization values are **native
`community.vmware.vmware_guest` dicts passed through 1:1** — anything the module
accepts is valid; hardware keys you omit inherit from the source template.

---

## Dual-mode usage guide

The role provisions each guest through ONE of two engines. Both consume the **same
per-host spec** — the flag picks the engine.

| | Mode 1: GOSC (default) | Mode 2: cloud-init GuestInfo |
|---|---|---|
| Mechanism | clone + vSphere customization spec | GOSC-free clone + `guestinfo.metadata` |
| NIC behaviour | disconnects, auto-reconnects (~30 s) | never disconnects |
| Template needs | correct guestId (`rhel9_64Guest`, never `other*`), open-vm-tools + perl | + cloud-init, `datasource_list: [VMware, OVF, None]`, `allow_raw_data: true`, `disable_vmware_customization: true` |
| `resource_pool` | pool **name** | name or **MOID** (MOID required if ambiguous, e.g. nested "Resources") |
| Flag | (unset / `false`) | `provision_via_guestinfo: true` per guest, or `vsphere_vm_provision_via_guestinfo: true` role-wide |

Resolution order: **per-host flag → role-wide (group) flag → `false` (GOSC)**.
One run can mix both modes; the guest list is split internally.

### The per-host spec (identical for both modes)

Host_vars shown; every key is also settable group-wide in group_vars:

```yaml
vsphere_vm_name: web-01                        # vCenter VM name  (default: inventory_hostname)
vsphere_vm_template: linux-almalinux-9-main    # source template  (required)
vsphere_vm_provision_via_guestinfo: true       # engine override  (optional)
vsphere_vm_hardware:                # native vmware_guest dict   (optional; replaces role default)
  num_cpus: 2
  memory_mb: 4096
vsphere_vm_disk:                    # native vmware_guest list   (optional; replaces role default)
  - {size_gb: 60, type: thin}       #   primary disk first (>= template size)
  - {size_gb: 50, type: thin}       #   extra data disks follow
vsphere_vm_networks:                # one entry per vNIC, in slot order (ens192/ens224/ens256)
  - name: "VLAN10-SVC"              #   portgroup                (required)
    type: static                    #   static | dhcp            (dhcp / missing ip → dhcp4)
    ip: 192.0.2.50                  #   required for static
    netmask: 255.255.255.0
    gateway: 192.0.2.1              #   ONLY on the primary NIC (default route + DNS)
    interface: ens192               #   override guest device name (optional)
vsphere_vm_customization:           # consumed as GOSC spec (mode 1) OR cloud-init metadata (mode 2)
  hostname: web-01
  domain: example.com               #   mode 1 FQDN suffix; harmless in mode 2
  dns_servers: [192.0.2.53]
vsphere_vm_guestinfo_userdata: |    # mode 2 only (optional raw #cloud-config)
  #cloud-config
  packages: [chrony]
vsphere_vm_state: poweredon
```

**Mode-specific field notes**
- *GOSC:* `customization.domain` is used; `guestinfo_userdata`/`interface:` are ignored.
- *GuestInfo:* `customization.hostname` + `dns_servers` feed the metadata (`domain` unused);
  every NIC needs `type: static` **and** `ip`, or that NIC intentionally renders `dhcp4: true` —
  a missing/typo'd `ip` silently becomes DHCP, so verify statics after first boot.

### Verifying a GuestInfo provision (acceptance checks)

```bash
# 1. metadata actually injected (BEFORE power-on):
govc object.collect -s /DC/vm/<vm> 'config.extraConfig["guestinfo.metadata"]' | base64 -d
# 2. no GOSC fired:
govc events -n 20 /DC/vm/<vm> | grep -ciE 'customiz|deployPkg'    # want 0
# 3. in-guest datasource:
cloud-init query platform                                          # want: vmware
```
