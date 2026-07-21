# storage

## TL;DR

Universal, hardware/name-agnostic disk role. Provisions, grows, formats, and
mounts LVM or plain storage on any Linux host — VM, bare metal, or cloud
image — regardless of disk size or device naming (`sda` / `nvme0n1` / `vda`
/ `mmcblk0`). One role handles growing, provisioning, mount hygiene, and
repair via a single declarative list.

```bash
ansible-playbook -i inventories/<env>/hosts.yml playbooks/<playbook>.yml --tags grow       # resize existing
ansible-playbook -i inventories/<env>/hosts.yml playbooks/<playbook>.yml --tags provision  # opt-in, fresh disks
```

## Requirements

Install collections before running (repo `requirements.yml`, or ad-hoc):

    ansible-galaxy collection install -r requirements.yml

| Collection | When | Used for |
|---|---|---|
| `ansible.posix` | always | mounting volumes (`ansible.posix.mount`) |
| `community.general` | When provisioning (`--tags provision`) | partitioning, LVM, filesystem creation |
| `community.general` | When SELinux management (EL) | `sefcontext` |

## Key variables

Full list: `defaults/main.yml`. Contract: `meta/argument_specs.yml`.

**Required** = value must be correct for a successful run (defaults often work).
**Optional** = safe to leave default / empty; phase stays off or uses built-ins.
**When X** = required only if that feature is on.

| Req | Variable | Default | Purpose |
|---|---|---|---|
| Optional | `storage_volumes` | `[]` | The master list — one entry per volume, drives every phase |
| Optional | `storage_profile` | `""` | Key into `storage_profiles`; empty uses `storage_volumes` verbatim |
| Optional | `storage_profiles` | `{}` | Named presets resolving to a `storage_volumes` list |
| Optional | `storage_manage_packages` | `true` | Install prerequisite packages (parted, lvm2, xfsprogs, …) first |
| Optional | `storage_grow` | `true` | Run the automatic, non-destructive grow pass |
| When creating disks | `storage_provision` | `false` | Allow opt-in provisioning (create/format) |
| Optional | `storage_require_fresh` | `true` | Provisioning refuses disks with an existing filesystem/partition signature |
| Optional | `storage_manage_fstab` | `true` | Manage UUID + `nofail` fstab entries and mounts |
| Optional | `storage_manage_selinux` | `true` | Apply SELinux fcontext + restorecon on EL |

Each `storage_volumes` entry needs only `name`, `mount`, and either
(`vg` + `lv`) or `disk` — the full field schema is in
`meta/argument_specs.yml`.

## Usage

Provision + mount a fresh data disk at `/opt`:

```yaml
- hosts: workers
  become: true
  roles:
    - role: storage
      vars:
        storage_provision: true
        storage_volumes:
          - name: opt
            disk: auto
            vg: vg_data
            lv: lv_opt
            mount: /opt
```

Run it:

```bash
ansible-playbook -i inventories/<env>/hosts.yml playbooks/<playbook>.yml --tags provision
```

## Preconditions

- On RHEL with RHSM, base repos are subscription-gated — register the host
  before this role, pre-bake `parted`/`lvm2`/`xfsprogs`/
  `cloud-utils-growpart` into the image, or set
  `storage_manage_packages: false`. Otherwise package install fails.
- A disk is only "blank" if it has no partitions and `blkid -p` finds no
  signature — stale metadata on an otherwise-empty disk defeats
  auto-selection.

## Behaviour

- Grow is automatic and non-destructive — it only enlarges existing stacks
  after the underlying disk grew.
- Provision is opt-in (`storage_provision: true` or `--tags provision`)
  and FRESH-guarded (`storage_require_fresh`, default `true`): it refuses
  any disk with an existing filesystem or partition signature.
- The disk backing `/` is discovered at runtime and excluded from `auto`
  selection and from provisioning.
- `disk: auto` resolves in kernel enumeration order (`sda` before `sdb`,
  `nvme0n1` before `nvme0n2`), which is not stable across
  reboots/controllers/clouds — with two or more blank disks where
  placement matters, pin a stable selector (`by-size:`, `by-serial:`,
  `by-wwn:`, or an explicit path) instead of `auto`.
- `discover` always runs (tagged `always`) regardless of which other
  `--tags` you pass, because every other phase depends on its facts.
- Installs its own prerequisite packages (`storage_manage_packages`,
  default `true`), so it has no dependency on a baseline role having run
  first — safe to run before it.
