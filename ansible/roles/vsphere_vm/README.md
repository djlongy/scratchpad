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

## TL;DR

**Most common: provision a VM.** Add the host to inventory (with its `vsphere_vm_*` vars) and run scoped to it — a no-tag run creates/ensures.

```bash
# ensure / create
ansible-playbook -i inventories/<env>/hosts.yml playbooks/site.yml --limit <newhost>
# wipe + rebuild, then CONTINUE the rest of the play
ansible-playbook -i inventories/<env>/hosts.yml playbooks/site.yml \
  -e vsphere_vm_force_redeploy=true --limit <newhost>
```

## Minimum required variables

The role's `meta/argument_specs.yml` marks only **two** vars `required: true` —
`vsphere_vm_server` and `vsphere_vm_datacenter` — but that is not enough to
actually clone a working VM. The role's own runtime asserts (`preflight.yml`,
`spec.yml`) and `community.vmware.vmware_guest` need the following minimal set:

| Var | Why it's needed |
|---|---|
| `vsphere_vm_server` | vCenter FQDN (**hard-required**; preflight asserts it). |
| `vsphere_vm_datacenter` | Target datacenter (**hard-required**; preflight asserts it). |
| **Credentials** — `vsphere_vm_vault_secret` (+ `vsphere_vm_username`, `vsphere_vm_password_field`) **OR** `vsphere_vm_password` | preflight asserts a password source. `vsphere_vm_username` defaults to `administrator@vsphere.local`; `vsphere_vm_password_field` defaults to `password`. |
| **Placement** — `vsphere_vm_cluster` **OR** `vsphere_vm_esxi_host` | Where the VM lands. For a standalone ESXi host also set `vsphere_vm_resource_pool` (its default pool is `Resources`). |
| `vsphere_vm_datastore` | Default datastore for the clone (role default is empty). |
| `vsphere_vm_template` | Source template to clone (`spec.yml` asserts it; not in argument_specs but effectively required). |
| **Networking** — `vsphere_vm_network`, `vsphere_vm_netmask`, and per-host `ansible_host` | `vsphere_vm_network` is the dvPortGroup (role default empty); `ansible_host` becomes the guest's static IP; `vsphere_vm_netmask` defaults to `255.255.255.0`. `vsphere_vm_dns` is optional but usually wanted. |

Tiny copy-pasteable minimal example (DRS cluster, Vault-backed credentials):

```yaml
# group_vars/vmware_vms.yml — the shared minimum
vsphere_vm_server: "vcenter.example.com"
vsphere_vm_datacenter: "Datacenter"
vsphere_vm_vault_secret: "kv-mgt/platform/vsphere/runtime"   # holds the vCenter password
# vsphere_vm_username / vsphere_vm_password_field default to administrator@vsphere.local / password
vsphere_vm_cluster: "Cluster"          # or: vsphere_vm_esxi_host + vsphere_vm_resource_pool
vsphere_vm_datastore: "datastore1"
vsphere_vm_template: "linux-almalinux-9-main"
vsphere_vm_network: "VLAN10-SVC"
vsphere_vm_netmask: "255.255.255.0"
vsphere_vm_dns: [192.0.2.53]

# host_vars/web01.yml (or inline beside the host) — the per-host unique
ansible_host: 192.0.2.50               # becomes the VM's static IP
```

For a standalone ESXi host swap the placement line for
`vsphere_vm_esxi_host: 192.0.2.11` plus `vsphere_vm_resource_pool: Resources`.
Everything else (hardware, disk, gateway, tags, provisioning mode) has a sane
default — see the sections below to tune it.

## Requirements

- `community.vmware` **>= 4.x** with a matching `pyVmomi` **<= 8.0.2.x** on the
  controller (pyVmomi 9 removed `VmomiSupport.VmomiJSONEncoder`, which the
  modules still call).
- vCenter credentials (Vault path or `vsphere_vm_password`).

## Phases (tags)

| Gate | Runs |
|---|---|
| (default) | create/ensure present guests |
| `-e vsphere_vm_force_redeploy=true` | **delete → rebuild**, then the **rest of the play continues** |
| same + `--tags redeploy` | wipe+rebuild **vsphere-only** (later roles tag-skipped) |
| `-e vsphere_vm_force_destroy=true` (with `state: absent`) | remove the guest on a full play |
| same + `--tags destroy` | remove, vsphere-only pass |

One opt-in var per destructive action (default false). Not `never`-tagged, so a
daisy-chain play can wipe the VM and keep going — same idea as
`audit_logging_mode` / ssh-agent unlock vars.

**Redeploy (full play continues):**

```bash
# delete → create → connect → then storage / baseline / freeipa / docker / vault …
ansible-playbook -i inventories/<env>/hosts.yml playbook.yml \
  -e vsphere_vm_force_redeploy=true
ansible-playbook ... -e vsphere_vm_force_redeploy=true --limit web01
```

**Redeploy (vsphere-only scope):**

```bash
ansible-playbook -i inventories/<env>/hosts.yml playbook.yml \
  --tags redeploy -e vsphere_vm_force_redeploy=true
```

The delete is idempotent, so redeploy also works on a host whose VM doesn't exist
yet (it just builds it — see *How a delete is targeted* below). The vsphere-only
scope rebuilds the VM but tag-skips every later role — the guest comes back a
bare template until a full (or config) run follows.

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
   connect_pass_delay` seconds (default 8 × 10 × 10 = ~800 s max) — and reports a
   clear message instead of hanging if a NIC is genuinely misconfigured.

| Var | Default | Purpose |
|---|---|---|
| `vsphere_vm_connect_nics` | `true` | reconnect NICs after build |
| `vsphere_vm_connect_passes` | `8` | reconnect+poll cycles before giving up |
| `vsphere_vm_connect_pass_retries` | `10` | IP polls per pass |
| `vsphere_vm_connect_pass_delay` | `10` | seconds between polls within a pass |
| `vsphere_vm_create_retries` | `3` | retry transient vCenter errors |
| `vsphere_vm_create_delay` | `15` | seconds between create retries |

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
  -e vsphere_vm_force_destroy=true
# vsphere-only scope
ansible-playbook -i inventories/<env>/hosts.yml playbook.yml \
  --tags destroy -e vsphere_vm_force_destroy=true
```

### How a delete is targeted

Both destructive phases (`destroy`, `redeploy`) run `identify.yml` first: it looks
the VM up with `community.vmware.vmware_guest_info` using **name + folder** (the
same placement create used), records the **vCenter instance UUID**, optionally
verifies **resource pool** when `vsphere_vm_resource_pool` is set, then the delete
runs `vmware_guest` with `uuid` + `use_instance_uuid: true` — never by bare name.

- **Name + folder (required for multi-tenant safety).** Tenants can share display
  names. Lookup is scoped to `_vsphere_guest.folder` (`/<DC>/vm/…` from
  `vsphere_vm_folder`). A same-named VM under another folder cannot be selected.
- **Instance UUID, not BIOS UUID** — the BIOS UUID (`hw_product_uuid`) can be
  duplicated across VMs by clones and imports; the instance UUID is unique within
  a vCenter. Delete is pinned to that UUID after identify.
- **Resource pool (when declared).** `vmware_guest_info` has no pool filter; if
  inventory sets `vsphere_vm_resource_pool`, identify re-reads the pinned UUID and
  **fails** if the live pool name does not match (exact or basename for full paths).
- **Not found at that placement → no delete.** Empty UUID skips the delete
  (idempotent destroy; redeploy doubles as create). A VM *moved* out of its
  inventory folder is treated as missing — update `vsphere_vm_folder` rather than
  falling back to a global name match (that would re-open wrong-VM risk).
- **Mismatch → hard fail.** If a result’s name/folder/pool do not match inventory,
  identify refuses to arm a delete.

This is the only place the role reads live vCenter state; the `plan` phase stays
strictly inventory-derived.

## Chaining on-guest roles in the same play (`vsphere_vm_wait_for_ssh`)

Every phase above runs on the control node (`delegate_to: localhost`) and never
touches the guest — so a plain `roles: [vsphere_vm, storage]` would have `storage`
try to SSH a VM that is still booting. Set **`vsphere_vm_wait_for_ssh: true`** and
the role appends a final handoff step: it waits until the fresh guest answers SSH
(a poll — it returns the instant SSH is up, not a fixed sleep) and gathers the
guest's facts. Now an on-guest role can follow in the **same play**:

```yaml
# provision + prepare in one play, one run
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

Requirements for the handoff to succeed (else `wait_for_connection` just times out):

- the play sets **`gather_facts: false`** (the handoff gathers facts itself, after the guest is up);
- the **template bakes in the ansible SSH user + authorized key** (this role does not inject one);
- **host-key checking is off** for the brand-new host (`ANSIBLE_HOST_KEY_CHECKING=False`).

Pair an extra `vsphere_vm_disk` entry with a `storage_volumes` entry pinned
`by-size:<same-size>` + `size: 100%FREE` and the data disk is provisioned and
mounted in the same run — see the swarm-lab group_vars for a worked example.
Tune the wait with `vsphere_vm_ssh_timeout` (default 300s). Left unset/false, the
role stays localhost-only (original behaviour).

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
vsphere_vm_cpu: 2               # → hardware.num_cpus
vsphere_vm_memory: 4096         # MB → hardware.memory_mb
# vsphere_vm_hardware: {boot_firmware: efi}   # extra native vmware_guest hardware keys
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
vsphere_vm_cpu: "{{ cpus }}"
vsphere_vm_memory: "{{ memory }}"
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

## Gateway (derived NIC)

For the auto-derived single NIC, the gateway follows this precedence:

1. **`vsphere_vm_gateway` set** → use it (always wins).
2. **`vsphere_vm_gateway_auto: true`** (and no explicit gateway) → the **first usable**
   address of the host's subnet, computed from `ansible_host` + `vsphere_vm_netmask`
   (respects `/25`–`/27` — e.g. `.65` on a `/26`, not a hardcoded `.1`).
3. **neither** → **no gateway** — an unrouted data/storage NIC (same as omitting
   `gateway` on an entry in a custom `vsphere_vm_networks` list).

The role never silently guesses a gateway: an unset gateway means "unrouted" unless you
opt into `vsphere_vm_gateway_auto`. Set the flag per host/group for routable fleets that
don't want to spell out every gateway.

## Known vSphere quirk (handled — but see the permanent fix)

Clone **with a vSphere Guest OS Customization (GOSC) spec** (`customization:`) toggles the new
VM's NIC **disconnected** during the guest's async first-boot customization, so it never gets an
IP and `wait_for_ip` hangs. Forcing `connected/start_connected: true` at create time is necessary
but **not sufficient** — GOSC reverts it after the clone returns. That is why `connect.yml` runs a
bounded **multi-pass** reconnect over all managed VMs (the pass that lands just after customization
finishes makes it stick). If you still see a disconnected NIC: `govc device.connect -vm <name> ethernet-0`.

**Root cause + permanent fix (documented once, cross-environment):** knowledge base →
`60-Domains/Infrastructure/vSphere — Cloned VM NIC Disconnects Under GOSC; Use cloud-init GuestInfo.md`.
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
  (e.g. `resgroup-362`, via `govc object.collect -s /DC/host/<h> resourcePool`). The GOSC path
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
  - {name: "VLAN10-MGT",     interface: ens192, ip: 10.0.10.60, netmask: 255.255.255.0, gateway: 10.0.10.1}
  - {name: "VLAN30-STORAGE", interface: ens224, ip: 10.0.30.60,    netmask: 255.255.255.0}   # data plane, no gateway
  - {name: "VLAN40-ACCESS",  interface: ens256, ip: 10.0.40.60,    netmask: 255.255.255.0}   # ingress plane
```

`vsphere_vm` provisions the vNICs + IPs; **per-NIC firewalld zones/services/rules are a `firewalld`/
`baseline` role's job** (guest-side, post-provision), keyed off these interface names — not this role.

Optional: set a guest's `guestinfo_userdata` to a raw `#cloud-config` string to also inject
`guestinfo.userdata` (packages, users, an authoritative resolv.conf, …).

## Variables

See `defaults/main.yml` for the full surface. Everything is a per-host/group
`vsphere_vm_*` var (hardware, disk, folder, datastore, networks, customization,
state, tags, wait_for_ip) resolved in each host's own context — normal Ansible
precedence, so a host/group value replaces the role default wholesale. The
hardware/disk/networks/customization values are **native
`community.vmware.vmware_guest` dicts passed through 1:1** — anything the module
accepts is valid; hardware keys you omit inherit from the source template.
