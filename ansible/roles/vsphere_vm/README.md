# vsphere_vm

Portable vCenter **VM lifecycle** role — clone VMs from a template (create) and
remove them (destroy) via `community.vmware.vmware_guest`. Idempotent; all
modules run on the control node against vCenter, driven by the
`vsphere_vm_guests` list.

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
# playbook.yml — runs on the controller (localhost)
- hosts: localhost
  gather_facts: false
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

vsphere_vm_guests:
  - name: soe-desktop-01
    template: linux-almalinux-9-main
    cpus: 2
    memory_mb: 4096
    disk_gb: 60
    networks:
      - name: "VLAN10-SVC"
        type: static
        ip: 192.0.2.50
        netmask: 255.255.255.0
        gateway: 192.0.2.1
    customization:
      hostname: soe-desktop-01
      domain: example.com
      dns_servers: [192.0.2.53]
```

Create / ensure:

```bash
ansible-playbook -i inventories/<env>/hosts.yml playbook.yml
```

Destroy (mark the guest `state: absent`, then):

```bash
ansible-playbook -i inventories/<env>/hosts.yml playbook.yml \
  --tags destroy -e vsphere_vm_allow_destroy=true
```

## Inventory-derived guests (recommended)

Instead of hand-curating `vsphere_vm_guests`, derive **one VM per inventory
host** — each host *is* a VM, its spec in its vars. The role reads
`hostvars[host]`, which is the **merged** view of inline + host_vars +
group_vars, so placement is your choice.

```yaml
vsphere_vm_from_inventory: true
vsphere_vm_inventory_group: vmware_vms
```

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
vsphere_vm_cpus: 2
vsphere_vm_memory_mb: 4096
vsphere_vm_network: "VLAN10-SVC"
vsphere_vm_dns: [192.0.2.53]
domain: example.com

# host_vars/web01.yml (or inline) — just the unique bits
ansible_host: 192.0.2.50        # becomes the VM's static IP
```

Per-host `vsphere_vm_networks` / `vsphere_vm_customization` override the
auto-derived NIC/customization entirely when you need something custom.

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

## Known vSphere quirk (handled)

Clone + guest-customization can leave the new VM's NIC **disconnected** (a
long-standing vSphere bug), so the guest never gets an IP and `wait_for_ip`
hangs. The role forces every NIC `connected: true` + `start_connected: true` to
avoid this. If you still see a disconnected NIC, connect it in vCenter (or
`govc device.connect -vm <name> ethernet-0`).

## Variables

See `defaults/main.yml` for the full surface and per-guest overrides (cpus,
memory_mb, disk_gb, disk_type, firmware, folder, datastore, networks,
customization, state, wait_for_ip).
