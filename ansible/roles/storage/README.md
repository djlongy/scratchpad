# storage

Universal, hardware/name-agnostic disk role. Provisions, grows, formats, and
mounts **LVM or plain** storage on **any** Linux host — VM, bare metal, or
cloud image — regardless of disk size or device naming (`sda` / `nvme0n1` /
`vda` / `mmcblk0`). The same declarative list also mounts **NFS and CIFS**
network volumes.

## TL;DR

**Most common: grow a disk after expanding its backing volume.** `--tags grow` rescans, then runs growpart → pvresize → lvextend → fs grow (auto, non-destructive); provisioning fresh disks is opt-in.

```bash
ansible-playbook -i inventories/<env>/hosts.yml playbooks/ops_storage.yml -e storage_target=<group> --tags grow       # resize existing
ansible-playbook -i inventories/<env>/hosts.yml playbooks/ops_storage.yml -e storage_target=<group> --tags provision  # opt-in, fresh disks
```

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
| Optional | `storage_profile` / `storage_profiles` | `""` / `{}` | Named preset selection; the estate catalog lives in `playbooks/group_vars/all/storage.yml` |
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

## Minimum configuration

```yaml
# group_vars/storage_hosts.yml
---
# Required
storage_volumes: "REPLACE_ME_storage_volumes"
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
            disk: auto
            vg: vg_data
            lv: lv_opt
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
            disk: auto
            vg: vg_stroom
            lv: lv_index
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
