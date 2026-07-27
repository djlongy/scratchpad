# storage

Universal, hardware/name-agnostic disk role. Provisions, grows, formats, and
mounts **LVM or plain** storage on **any** Linux host — VM, bare metal, or
cloud image — regardless of disk size or device naming (`sda` / `nvme0n1` /
`vda` / `mmcblk0`). The same declarative list also mounts **NFS and CIFS**
network volumes.

## TL;DR

**Most common: provision fresh data disks, picked by size, formatted `xfs`.**
Pin each volume to its disk with `by-size:` rather than `auto` — `auto` takes
the first blank disk in kernel enumeration order, which is not stable across
reboots, controllers, or clouds.

```yaml
# group_vars/<group>.yml
storage_provision: true
storage_profile: app-node
storage_profiles:
  app-node:
    - {name: opt,  disk: "by-size:50G", vg: vg_opt,  lv: lv_opt,  fstype: xfs, mount: /opt}
    - {name: data, disk: "by-size:20G", vg: vg_data, lv: lv_data, fstype: xfs, mount: /data}
```

```bash
ansible-playbook -i inventories/<env>/hosts.yml playbooks/ops_storage.yml -e storage_target=<group> --tags provision  # opt-in, fresh disks
ansible-playbook -i inventories/<env>/hosts.yml playbooks/ops_storage.yml -e storage_target=<group> --tags grow       # resize existing
```

Growing is the other half: `--tags grow` rescans, then runs growpart →
pvresize → lvextend → fs grow. It is automatic and non-destructive, so it
runs on every no-tags reconcile; provisioning is always opt-in.

## Requirements

Install collections before running (repo `requirements.yml`, or ad-hoc):

    ansible-galaxy collection install -r requirements.yml

| Collection | When | Used for |
|---|---|---|
| `community.general` | When `storage_provision` | disk partitioning + LVM + filesystem creation (`parted`/`lvg`/`lvol`/`filesystem`) |
| `community.general` | When `storage_manage_selinux` | SELinux fcontext (`sefcontext`) |
| `ansible.posix` | When `storage_manage_fstab` | mount management (`mount`), local and network |

## Key variables

Full list: `defaults/main.yml`. Contract: `meta/argument_specs.yml`.

**Required** = value must be correct for a successful run (defaults often work).
**Optional** = safe to leave default / empty; phase stays off or uses built-ins.

| Req | Variable | Default | Purpose |
|---|---|---|---|
| **Required** | `storage_volumes` | `[]` | Declarative volume list (or resolve one from `storage_profiles` via `storage_profile`) — empty means nothing to do |
| Optional | `storage_profile` / `storage_profiles` | `""` / `{}` | Named preset selection — see [Storage profiles](#storage-profiles); the catalog lives in `playbooks/group_vars/all/storage.yml` |
| Optional | `storage_manage_packages` | `true` | Install `parted`/`lvm2`/`xfsprogs`/etc. before acting |
| Optional | `storage_grow` | `true` | Run the automatic, non-destructive grow pass |
| Optional | `storage_provision` | `false` | Allow opt-in provisioning (create/format) |
| Optional | `storage_require_fresh` | `true` | Provisioning refuses disks with an existing signature |
| Optional | `storage_manage_fstab` | `true` | Manage UUID + `nofail` fstab entries and mount |
| Optional | `storage_manage_selinux` | `true` | Apply SELinux fcontext + restorecon on EL |
| Optional | `storage_debug` | `false` | Emit discovery/provisioning-plan debug output |
| Optional | `storage_part_suffix_devices` | `[nvme, mmcblk, loop, nbd]` | Basename prefixes that use `p` before the partition number (`nvme0n1p1` vs `sda1`) |
| Optional | `storage_part_suffix_devices_extra` | `[]` | Append more prefixes without restating the built-in list |
| **Required for NFS** | `storage_manage_nfs` | `false` | Mount declared `kind: nfs` volumes — without it they are skipped, not failed |
| **Required for CIFS** | `storage_manage_cifs` | `false` | Mount declared `kind: cifs` volumes — without it they are skipped, not failed |
| Optional | `storage_allow_nested_mounts` | `false` | Allow a network mount nested inside another declared mount (`/data` + `/data/sub`) |
| Optional | `storage_cifs_credentials_dir` | `/etc/cifs-credentials` | Root-owned `0700` directory holding one `0600` credentials file per CIFS volume |
| Optional | `storage_net_protected_mounts` | `[/, /boot, /etc, /usr, /var, /home]` | Mount points a **network** volume may never claim |
| Optional | `storage_default_nfs_fstype` / `storage_default_cifs_fstype` | `nfs4` / `cifs` | `fstype` applied per kind when an entry omits it |
| Optional | `storage_default_nfs_opts` / `storage_default_cifs_opts` | see `defaults/main.yml` | `opts` applied per kind when an entry omits it (`_netdev` + `nofail` are always appended) |

### Disk selectors

Every local volume resolves its `disk` field to a concrete device during the
read-only `discover` phase. **Prefer `by-size:`** — it is stable across reboots
and re-imaging, and it is the only selector that stays correct when a template
grows a new disk or the controller renumbers.

| Selector | Example | Matches on | Use when |
|---|---|---|---|
| `by-size:` | `by-size:50G` | Disk capacity, rounded to GiB | **Default choice.** Disks differ in size — the normal case for a VM built from a template |
| `by-serial:` | `by-serial:VB1a2b3c4d` | `lsblk` serial | Two blank disks share a size |
| `by-wwn:` | `by-wwn:0x5000c500a1b2c3d4` | `lsblk` WWN | SAN / multipath, or the serial is not exposed |
| explicit path | `/dev/sdb`, `/dev/disk/by-id/…` | The path as given | Bare metal with fixed cabling, or you need a `by-id`/`by-path` alias |
| `auto` | `auto` | First blank non-root disk | Single data disk only — enumeration order is **not stable** |

Omit `disk` entirely on a grow-only LVM volume: once the VG exists the volume
is located through it, and the selector is not consulted at all.

**`by-size:` accepts both size conventions.** Hypervisors and clouds disagree
on what a "50G disk" is, so `by-size:50G` matches a disk whose rounded GiB
equals *either* 50 GiB (binary, 53,687,091,200 B) *or* 50 GB (decimal,
50,000,000,000 B ≈ 47 GiB). You do not need to know which convention the
platform used.

**An ambiguous pin is a hard failure, not a coin flip.** If two *blank* disks
match the pinned size, the role stops and names them rather than risk building
on the wrong one:

```
Volume 'data' pins disk by-size:50G but 2 blank disks match (sdb, sdc).
Disambiguate with by-serial:/by-wwn: or an explicit /dev path.
```

The check is skipped once the volume's VG exists — adoption never writes to a
disk, so ambiguity is harmless at that point.

**Sizing the LV is a separate field.** `disk:` chooses the *device*; `size:`
chooses how much of the VG the *logical volume* takes (`100%FREE` by default).
`by-size:50G` with `size: 100%FREE` means "find the 50G disk, then give the LV
all of it".

### Network volume fields

Set `kind: nfs` or `kind: cifs` on an entry in `storage_volumes`. Network
volumes are mount-only: they never enter discovery, grow or provisioning, and
must declare no block-device field.

| Field | Kind | Purpose |
|---|---|---|
| `server` | both | NFS/SMB server hostname or IP |
| `export` | nfs | Export path on the server |
| `share` | cifs | SMB share name |
| `mount` | both | Mount point — **required** (a network volume that is not mounted does nothing) |
| `fstype` | both | `nfs` \| `nfs4` (default `nfs4`), `cifs` (default) |
| `opts` | both | Mount options; `_netdev` and `nofail` are force-appended |
| `credentials_username` / `credentials_password` / `credentials_domain` | cifs | Written to `0600 root:root` `<credentials_dir>/<name>.cred`; supply the password from Vault via inventory |
| `owner` / `group` / `mode` | both | Applied to the mount-point directory **before** it is mounted over |

### Extending partition device naming

Linux names partitions two ways:

| Disk basename | Partition 1 |
|---|---|
| `sda`, `vda`, `xvda`, … | `sda1` (bare number) |
| `nvme0n1`, `mmcblk0`, `loop0`, `nbd0`, … | `nvme0n1p1` (`p` + number) |

Discovery builds `_part_dev` from that rule. If a new controller needs the
`p` form, **do not fork the role** — set inventory vars:

```yaml
# Append only (preferred)
storage_part_suffix_devices_extra:
  - mynewctrl

# Or replace the full list
storage_part_suffix_devices:
  - nvme
  - mmcblk
  - loop
  - nbd
  - mynewctrl
```

## Storage profiles

A profile is a **named, reusable `storage_volumes` list**. Instead of repeating
the same disk layout on every host, declare each layout once in a catalog and
have each host pick one by name. This is the recommended way to drive the role
once you have more than a couple of hosts.

```yaml
storage_profiles:          # the catalog — one key per named layout
  <profile-name>:
    - {name: …, disk: "by-size:…", …}
storage_profile: <profile-name>   # the selection — one per host or group
```

**Resolution order** is `storage_profile` → `storage_volumes` → legacy
`datavols`. A non-empty `storage_profile` wins outright: `storage_volumes` is
ignored, not merged. Leave `storage_profile` empty (`""`, the default) to use
`storage_volumes` verbatim.

### Where the catalog lives

Put the catalog somewhere every host can see it and the selection next to the
host or group it applies to:

| What | Where | Why |
|---|---|---|
| `storage_profiles` (the catalog) | `playbooks/group_vars/all/storage.yml` | Written once, visible everywhere |
| `storage_profile` (the selection) | `group_vars/<group>.yml` or `host_vars/<host>.yml` | One line per host/group |

### A catalog

```yaml
# playbooks/group_vars/all/storage.yml
---
storage_profiles:

  # Single 50G data disk — the common application node.
  app-node:
    - name: opt
      disk: "by-size:50G"
      lvm: true
      vg: vg_opt
      lv: lv_opt
      size: "100%FREE"
      fstype: xfs
      mount: /opt

  # Two disks, distinguished by size — no serials needed.
  db-node:
    - {name: data, disk: "by-size:100G", vg: vg_data, lv: lv_data, size: "100%FREE", fstype: xfs, mount: /var/lib/pgsql}
    - {name: wal,  disk: "by-size:20G",  vg: vg_wal,  lv: lv_wal,  size: "100%FREE", fstype: xfs, mount: /var/lib/pgsql/wal}

  # Same size twice — by-size alone would be ambiguous, so pin the serials.
  log-node:
    - {name: log01, disk: "by-serial:VB1a2b3c4d", vg: vg_log01, lv: lv_log01, fstype: xfs, mount: /var/log/app01}
    - {name: log02, disk: "by-serial:VB5e6f7a8b", vg: vg_log02, lv: lv_log02, fstype: xfs, mount: /var/log/app02}

  # One disk carved into two LVs, plus an NFS share in the same list.
  worker:
    - {name: opt,   disk: "by-size:80G", vg: vg_app, lv: lv_opt,   size: 40G,       fstype: xfs, mount: /opt}
    - {name: cache, vg: vg_app,          lv: lv_cache, size: "100%FREE",            fstype: xfs, mount: /var/cache/app}
    - {name: shared, kind: nfs, server: nas1.example.internal, export: /export/shared, mount: /shared}
```

The second entry of `worker` omits `disk` deliberately: it is a second LV in a
VG the first entry already created, so it is located through `vg_app` and needs
no selector.

### Selecting one

```yaml
# group_vars/app_servers.yml
storage_profile: app-node
storage_provision: true        # required — provisioning is opt-in

# host_vars/db01.example.com.yml
storage_profile: db-node       # this host overrides the group
```

```bash
ansible-playbook -i inventories/<env>/hosts.yml playbooks/ops_storage.yml -e storage_target=app_servers --tags provision
```

Override the selection for a one-off run without editing inventory:

```bash
ansible-playbook … -e storage_profile=db-node --tags provision
```

### Notes

- **An unknown profile name is a hard failure.** `storage_profiles[storage_profile]`
  is looked up with no fallback, so a typo fails the play with an undefined-key
  error rather than silently doing nothing. That is deliberate — a silent no-op
  on a provisioning run looks identical to success.
- Every field except `name` is optional; anything omitted falls back to the
  `storage_default_*` values in `defaults/main.yml` (`lvm: true`,
  `partition: true`, `fstype: xfs`, `size: 100%FREE`, …). The examples above
  spell out `fstype: xfs` for clarity even though it is already the default.
- A profile is a plain list, so network volumes (`kind: nfs` / `kind: cifs`)
  belong in it too — see `worker` above.
- Profiles are resolved before validation, so all rules V1–V9 apply to the
  resolved list exactly as if it had been written into `storage_volumes`.

## Minimum configuration

```yaml
# group_vars/storage_hosts.yml
---
# Required — a volume list, or a profile name that resolves to one
storage_provision: true
storage_volumes:
  - name: data
    disk: "by-size:50G"
    vg: vg_data
    lv: lv_data
    fstype: xfs
    mount: /data
```
## Usage

```yaml
- hosts: workers
  become: true
  roles:
    - role: storage
      vars:
        storage_provision: true
        storage_volumes:
          - name: opt
            disk: "by-size:50G"
            vg: vg_data
            lv: lv_opt
            fstype: xfs
            mount: /opt
```

```bash
ansible-playbook -i inventories/<env>/hosts.yml playbooks/ops_storage.yml -e storage_target=workers
```

Network volumes live in the same list:

```yaml
- hosts: stroom
  roles:
    - role: storage
      vars:
        storage_manage_nfs: true
        storage_manage_cifs: true
        storage_volumes:
          - name: stroom-index          # local — kind defaults to 'local'
            disk: "by-size:200G"
            vg: vg_stroom
            lv: lv_index
            fstype: xfs
            mount: /stroomdata/stroom-index-p00

          - name: stroom-data
            kind: nfs
            server: nas1.example.internal
            export: /mnt/user/stroom-data
            mount: /stroomdata/stroom-data-p00
            opts: "hard,noatime,x-systemd.mount-timeout=30"

          - name: media-archive
            kind: cifs
            server: nas2.example.internal
            share: archive
            mount: /mnt/archive
            opts: "vers=3.1.1,uid=0,gid=0,file_mode=0640,dir_mode=0750"
            credentials_username: svc-example
            credentials_password: "{{ vault_storage_cifs_password }}"
```

Each entry in `storage_volumes` describes a volume end-to-end; the full
field schema is in `meta/argument_specs.yml`.

## Preconditions

- On RHEL with RHSM, the base package repos `storage_manage_packages` relies
  on (`parted`, `lvm2`, `xfsprogs`, `cloud-utils-growpart`, …) are
  subscription-gated — running this role before the host is registered fails
  the install. Register the host first, pre-bake the tools into the
  template, or set `storage_manage_packages: false`.
- Network volumes need the export/share to already exist on the server and
  the host to be permitted by its export ACL — this role mounts, it does not
  create shares or open firewall paths.

## Behaviour

- Grow is automatic and non-destructive — it only enlarges existing stacks
  after the underlying disk grew.
- Provision is opt-in (`storage_provision: true` or `--tags provision`)
  **and** FRESH-guarded (`storage_require_fresh`, default `true`): it
  refuses any disk with an existing filesystem or partition signature.
- The disk backing `/` is discovered at runtime and excluded from `auto`
  selection and from provisioning.
- A no-tags run is a full idempotent reconcile
  (`validate → packages → discover → grow → provision → mount → mount_net →
  selinux`); the `discover` phase (read-only device discovery + selector
  resolution) always runs regardless of tags, because every other phase
  depends on its facts.
- **Validation runs on every invocation, under the `always` tag**, before any
  phase acts — a declaration error surfaces whichever refinement tag was
  selected. Rules V1–V7 and V9 are pure checks over the declared list; V8
  probes the host with `findmnt` and only when network volumes are enabled.
- **Network volumes bypass the block layer entirely.** The list is split by
  `kind` before any phase runs: `discover` / `grow` / `provision` / `mount` /
  `selinux` see local volumes only.
- **Network kinds are opt-in per kind.** A declared `nfs`/`cifs` volume whose
  `storage_manage_<kind>` flag is `false` is skipped with a message naming the
  flag — it is never a failure, so a host can carry the declaration ahead of
  the server being ready.
- `_netdev` and `nofail` are force-appended to every network mount: without
  `_netdev` systemd may order the mount before the network is up and hang
  boot. Option strings are de-duplicated and sorted so `ansible.posix.mount`
  does not rewrite `/etc/fstab` on every run.
- **CIFS passwords never reach `/etc/fstab`** (it is world-readable). They are
  written to `<storage_cifs_credentials_dir>/<name>.cred`, mode `0600`
  `root:root`, and referenced by `credentials=<path>`; any operator-supplied
  `credentials=` option is dropped in favour of the managed one.
- `hard` is the default NFS mount option: an audit/archive mount should stall
  when the server goes away rather than silently truncate writes.
- Mount-point ownership (`owner`/`group`/`mode`) is applied to a network
  volume's directory only while it is still empty; once the remote filesystem
  is mounted, ownership belongs to the server.
- The protected-mount list (`storage_net_protected_mounts`) constrains
  **network** volumes only. `/var` and `/var/lib/dirsrv` are legitimate local
  mount points in the estate profile catalogue.
- `disk: auto` picks the first blank non-root disk in kernel enumeration
  order, but that order is **not stable** across reboots, controllers, or
  clouds. With two or more blank disks where placement matters, pin a
  stable selector (`by-size:`, `by-serial:`, `by-wwn:`, or an explicit
  `/dev/...` path) instead of `auto`.

## Validation rules

Every rule fails fast with a message naming the offending volume(s).

| # | Rule |
|---|---|
| V1 | No two volumes claim the same mount point — local and network checked together |
| V2 | A `local` volume declares no `server` / `export` / `share` / `credentials_*` |
| V3 | An `nfs`/`cifs` volume declares no `disk` / `lvm` / `vg` / `lv` / `size` / `partition` / `partition_number` / `provision: true` |
| V4 | `nfs` requires `server` + `export`; `cifs` requires `server` + `share` |
| V5 | `mount` is required and non-empty on network volumes |
| V6 | `kind` is one of `local`, `nfs`, `cifs` |
| V7 | A network `mount` is an absolute path and not in `storage_net_protected_mounts` |
| V8 | Runtime shadow check — a network mount point already serving a **different** source is refused rather than clobbered |
| V9 | A network `mount` is not nested inside another declared `mount` unless `storage_allow_nested_mounts: true` |

## Pairing with server roles

This role is **client only**. Export/share creation is the server role:

| Client (`storage`) | Server role | Server field → client field |
|---|---|---|
| `kind: nfs` | `nfs_server` | export `path` → `export`; host → `server` |
| `kind: cifs` | `cifs_server` | share `name` → `share`; host → `server`; Samba user → `credentials_*` |

Enable the matching gate (`storage_manage_nfs` / `storage_manage_cifs`). Specialised
clients still exist for other patterns (`nfs_home_client`, autofs `nfs_client`,
legacy `smb`).

## Out of scope

Network-filesystem features this role deliberately **does not support** —
declare them elsewhere or extend the role first:

- iSCSI, multipath, and any other block-over-network transport.
- SMB1/NT1, and `sec=krb5` (Kerberised) NFS (server may offer krb5p for
  per-user exports; this client path is `sec=sys` static mounts).
- autofs / automounter maps — mounts are static fstab entries.
- Per-user CIFS credentials — one machine credential per volume, `root`-owned.
- Creating exports/shares, export ACLs, or firewall paths — use `nfs_server` /
  `cifs_server`.
