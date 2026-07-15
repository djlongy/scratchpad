# storage

Universal, hardware/name-agnostic disk role. Provisions, grows, formats, and
mounts **LVM or plain** storage on **any** Linux host — VM, bare metal, or
cloud image — regardless of disk size or device naming (`sda` / `nvme0n1` /
`vda` / `mmcblk0`).

It consolidates four older patterns (grow, provision, mount-hygiene, repair)
into one role driven by a single declarative list.

## How it works

One list, `storage_volumes`, where each entry describes a volume end-to-end.
The same entry drives every phase:

```
packages -> discover -> grow -> provision -> mount -> selinux
```

A no-tags run is a full idempotent reconcile. Refinement tags narrow it for
fast iteration:

| Tag | Phase | Default |
|-----|-------|---------|
| `--tags grow` | rescan -> growpart/resizepart -> pvresize -> lvextend -> fs grow | auto |
| `--tags provision` | FRESH guard -> parted -> pv/vg/lv (or partition) -> mkfs | opt-in |
| `--tags mount` | mkdir -> UUID fstab (nofail) -> mount -> assert | auto |
| `--tags selinux` | semanage fcontext + restorecon (EL) | auto |

`discover` (read-only device discovery + selector resolution) always runs
because the other phases depend on its facts.

## Safety model

- **Grow is automatic and non-destructive** — it only enlarges existing
  stacks after the underlying disk grew.
- **Provision is opt-in** (`storage_provision: true` or `--tags provision`)
  **and FRESH-guarded** (`storage_require_fresh`, default true): it refuses
  any disk with an existing filesystem or partition signature.
- The disk backing `/` is discovered at runtime and excluded from `auto`
  selection and from provisioning.

## Ordering & prerequisites

This role is **layer-0 / self-contained**: run it first (before a baseline
role, before any app role), because logging filesystems (`/var/log`) and
app data dirs (`/opt`, …) depend on disks being sized and mounted.

It installs its own prerequisites — `packages` is the first phase
(`storage_manage_packages: true`), pulling `parted, lvm2, xfsprogs,
e2fsprogs, cloud-guest-utils`/`cloud-utils-growpart` per distro — so it has
**no dependency** on anything baseline does.

One caveat: those packages come from the distro's **base repos**. On
AlmaLinux/Rocky/Debian/Ubuntu they install without registration. On **RHEL
with RHSM**, base repos are subscription-gated, so if you run this before the
host is registered the install fails — in that case either (a) pre-bake the
tools into your template (`lvm2`/`xfsprogs` are already present on an LVM
template; add `parted` + `cloud-utils-growpart`) and/or (b) set
`storage_manage_packages: false`, or (c) register the host before this role.

## Selectors — choosing among multiple disks

`disk: auto` picks the first **blank** non-root disk in kernel enumeration
order (`sda` before `sdb`, `nvme0n1` before `nvme0n2`). With two `auto`
volumes each gets a distinct disk (first, then second). But kernel order is
**not stable** across reboots/controllers/clouds, so when you have **two or
more empty disks and it matters which volume lands where**, pin them with a
stable selector instead of `auto`:

| Selector | Use when |
|----------|----------|
| `auto` | exactly one blank disk, or volumes are interchangeable |
| `by-size:50G` | blank disks differ in size |
| `by-serial:<S>` / `by-wwn:<W>` | same-size disks (each has a unique ID) |
| `/dev/sdb` | names are stable in your environment (least portable) |

Find stable IDs with `lsblk -dpo NAME,SIZE,SERIAL,WWN`. A disk is only
"blank" if it has no partitions and `blkid -p` finds no signature; the
FRESH guard is the backstop if a selector ever points at a non-empty disk.

## Volume schema

| Field | Meaning |
|-------|---------|
| `name` | logical handle (required) |
| `disk` | `auto` \| `/dev/sdb` \| `by-size:50G` \| `by-serial:X` \| `by-id:X` \| `by-wwn:X` |
| `lvm` | `true` LVM stack \| `false` plain partition |
| `partition` | LVM: partition the disk vs. whole-disk PV |
| `partition_number` | partition to create/grow (default 1) |
| `vg` / `lv` | LVM names |
| `size` | `40G` \| `100%FREE` \| `50%VG` |
| `fstype` | `xfs` \| `ext4` |
| `mount` | mount point (`''` = manage block stack only) |
| `opts` | fstab options (`nofail` enforced) |
| `sefcontext` | SELinux fcontext type (EL) |
| `provision` / `grow` | participate in each pass |
| `owner` / `group` / `mode` | mountpoint perms |

Per-volume defaults (`storage_default_*`) fill anything omitted, so a minimal
entry is just `name`, `mount`, and either (`vg` + `lv`) or `disk`.

## Selectors

Device names reorder across reboots and clouds, so prefer stable selectors:

- `auto` — first blank, non-root disk (consumed in list order)
- `by-size:50G` — first blank disk of that size
- `by-serial:` / `by-id:` / `by-wwn:` — match a stable identifier
- `/dev/sdb` — explicit path (least stable)

Partition device names are **derived**, never assumed: `nvme0n1` + part 1 =
`nvme0n1p1`, `sda` + 1 = `sda1` (also `mmcblk`/`loop`/`nbd` take the `p`).

## Examples

Minimal — provision + mount a fresh data disk at `/opt`:

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

Modeling a typical estate (grow-only root `/var`, data `/opt`, plain `/data`):

```yaml
storage_volumes:
  # grow-only — never created, only resized when /dev/sda grows
  - name: var
    lvm: true
    vg: sysvg
    lv: lv_var
    mount: /var
    partition_number: 3
    disk: /dev/sda
    provision: false
    grow: true
  # LVM data disk
  - name: opt
    disk: auto
    vg: vg_data
    lv: lv_opt
    size: 100%FREE
    fstype: xfs
    mount: /opt
    sefcontext: usr_t
    provision: true
  # plain (no LVM) cloud data partition
  - name: data
    lvm: false
    disk: by-size:100G
    fstype: ext4
    mount: /data
    provision: true
```

Named profile:

```yaml
storage_profile: docker
storage_profiles:
  docker:
    - name: docker
      vg: vg_data
      lv: lv_docker
      mount: /var/lib/docker
      provision: true
```
