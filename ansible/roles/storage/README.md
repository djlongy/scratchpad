# storage

## TL;DR

Provisions, grows, formats, and mounts **LVM or plain** storage on any Linux
host — VM, bare metal, or cloud image — regardless of disk size or device
naming (`sda` / `nvme0n1` / `vda` / `mmcblk0`), and mounts **NFS and CIFS**
volumes from the same declarative list. Growing is automatic and
non-destructive; creating and formatting is opt-in.

```bash
ansible-playbook -i inventories/<env>/hosts.yml playbooks/ops_storage.yml -e storage_target=<group>
```

## Requirements

Install collections before running (repo `requirements.yml`, or ad-hoc):

    ansible-galaxy collection install -r requirements.yml

| Collection | When | Used for |
|---|---|---|
| `community.general` | When `storage_provision` | Partitioning, LVM, and filesystem creation (`parted` / `lvg` / `lvol` / `filesystem`) |
| `community.general` | When `storage_manage_selinux` | SELinux file contexts (`sefcontext`) |
| `ansible.posix` | When `storage_manage_fstab` | Mount and fstab management (`mount`), local and network |

## Quick start — I added a second disk, now what?

The most common consumption of this role, end to end. You attached a blank
20 G disk to a guest and want it as `/opt/app`:

```yaml
# group_vars/<group>.yml  (or role vars — see Usage)
storage_provision: true          # arm create/format — nothing else does
storage_volumes:
  - name: app
    disk: "by-size:20G"          # see "Choosing the disk" below
    lvm: true
    vg: vg_app
    lv: lv_app
    size: "100%FREE"
    fstype: xfs
    mount: /opt/app
```

```bash
ansible-playbook -i inventories/<env>/hosts.yml playbooks/ops_storage.yml -e storage_target=<group>
```

That one entry drives every phase: the disk is located by the selector,
partitioned, made a PV, wrapped in `vg_app/lv_app`, formatted `xfs`, mounted
at `/opt/app`, and given a `UUID=` + `nofail` fstab entry. Re-runs are
idempotent; if the underlying vmdk is later resized, the grow phase extends
partition → PV → LV → filesystem automatically on the next converge.

Remove `storage_provision: true` once built if you want converges to be
adopt/grow/mount-only — the stack it built keeps working (the opt-in arms
*creation*, not existence).

## Choosing the disk (`disk:` selectors)

`disk:` answers exactly one question: *which block device may this volume
consume?* Five forms are supported (anything else is rejected by validation):

| Selector | Meaning | Use when |
|---|---|---|
| `auto` (or empty) | First **blank**, non-root disk in kernel enumeration order | Exactly one data disk, placement doesn't matter |
| `by-size:20G` | First blank disk of that size | The disk sizes on a host are distinct — the safest norm when disk sizes are distinct (pair the pin with whatever provisions the guest's disks) |
| `by-serial:<serial>` | Disk whose `lsblk` SERIAL matches | Same-size disks must not be confused; survives reboots and controller reordering |
| `by-wwn:<wwn>` | Disk whose `lsblk` WWN matches | Same as by-serial where the platform exposes WWNs |
| `/dev/sdb` | That exact device path | You are naming the device yourself — also the only implicit-selection escape hatch on hosts whose root is not a block device |

Find the values to pin against on the host:

```bash
lsblk -dn -o NAME,SIZE,SERIAL,WWN
# sda   100G  6000c29d…   0x6000c29d…
# sdb    20G  6000c29e…   0x6000c29e…
```

Semantics worth knowing before you pin:

- **`auto` order is not stable.** Kernel enumeration order changes across
  reboots, controllers, and cloud platforms. With two or more blank disks
  where placement matters, always pin `by-size:` / `by-serial:` / `by-wwn:`
  or an explicit path.
- **`auto` picks only from blank disks** (no filesystem signature, no
  partitions, not mounted). The `by-*` matchers search every **non-root**
  disk — so they can also target an existing stack for adoption — but a
  matched non-blank disk is still refused at provision time by the FRESH
  guard. In both pools the root disk (and any disk backing the root VG) is
  excluded before matching.
- **`by-size:` accepts both size conventions.** `by-size:50G` matches a disk
  that is 50 GiB (binary) *or* 50 GB (decimal ≈ 46.6 GiB) — hypervisors and
  clouds disagree about what a "50G disk" is, and a convention mismatch would
  otherwise silently match nothing.
- **An ambiguous `by-size:` pin is refused.** Two blank disks of the pinned
  size means the role could build on the wrong one — the run fails and tells
  you to disambiguate with `by-serial:` / `by-wwn:` / `/dev/...`. (Skipped
  once the VG already exists: adoption never touches a disk.)
- **First match wins, and each match is consumed** — two volumes cannot
  resolve to the same disk, including two volumes naming the same explicit
  `/dev/...` path.
- **`by-id:` is not supported.** Use `by-serial:` / `by-wwn:`, which `lsblk`
  reports directly.
- **Quote numeric serials**: `disk: "by-serial:68000"` — unquoted YAML
  integers would silently match nothing.
- Grow-only LVM volumes (existing VG) need no `disk:` at all — the stack is
  located through the VG.

## Per-volume fields (local volumes)

Every entry in `storage_volumes` describes one volume end to end. All fields
except `name` have defaults (`storage_default_*` in `defaults/main.yml`);
the authoritative schema is `meta/argument_specs.yml`.

| Field | Default | Purpose |
|---|---|---|
| `name` | — (required) | Volume label used in task output and the CIFS credentials filename |
| `kind` | `local` | `local` \| `nfs` \| `cifs` — network kinds bypass the block stack entirely |
| `disk` | `""` (= auto) | Disk selector — see above |
| `lvm` | `true` | Build PV → VG → LV; `false` formats the partition directly |
| `partition` | `true` | Create/use a partition; `false` uses the whole disk |
| `partition_number` | `1` | Partition to use/grow — e.g. `3` for the standard OS-disk `/var` layout |
| `vg` / `lv` | `""` | Volume group / logical volume names (LVM volumes) |
| `size` | `100%FREE` | LV size (`lvol` syntax: `20g`, `50%VG`, `100%FREE`) |
| `fstype` | `xfs` | Filesystem to create/grow (`xfs` \| `ext4`) |
| `mount` | `""` | Mount point. **A declared mount is a promise** — no filesystem behind it fails the play (see Behaviour) |
| `opts` | `defaults,noatime,nofail,x-systemd.device-timeout=30` | fstab options |
| `sefcontext` | `""` | SELinux file context applied to the mount (e.g. `usr_t`, `container_file_t`) |
| `provision` | `true` | Whether *this volume* may be created by the role (still needs role-level `storage_provision`); `false` = adopt a stack created elsewhere |
| `grow` | `true` | Whether the grow phase may extend this volume |
| `owner` / `group` / `mode` | `root` / `root` / `0755` | Applied to the mount point, and re-applied on the mounted filesystem root |

`provision` vs `storage_provision`: the role-level `storage_provision: true`
arms creation for the run; per-volume `provision: false` opts a single volume
out (adopt-only) even when the run is armed. `--tags provision` does **not**
arm anything (see Tag safety).

### Network volume fields

Set `kind: nfs` or `kind: cifs`. Network volumes are mount-only: they never
enter discovery, grow, or provisioning, and must declare no block-device
field.

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

## Key variables

Full list: `defaults/main.yml`. Contract: `meta/argument_specs.yml`.

| Req | Variable | Default | Purpose |
|---|---|---|---|
| **Required** | `storage_volumes` | `[]` | Declarative volume list (or resolve one from `storage_profiles` via `storage_profile`) — empty means nothing to do |
| Optional | `storage_profile` / `storage_profiles` | `""` / `{}` | Named preset selection; the demo catalog lives in `playbooks/group_vars/all/storage.yml` |
| Optional | `storage_provision` | `false` | Arms creation and formatting — the only switch that does |
| Optional | `storage_require_fresh` | `true` | Provisioning refuses a disk carrying an existing filesystem or partition signature |
| Optional | `storage_grow` | `true` | Run the automatic, non-destructive grow pass |
| Optional | `storage_manage_packages` | `true` | Install `parted`/`lvm2`/`xfsprogs`/etc. before acting |
| Optional | `storage_manage_fstab` | `true` | Manage UUID + `nofail` fstab entries and mount |
| Optional | `storage_manage_selinux` | `true` | Apply SELinux fcontext + restorecon on EL |
| Optional | `storage_debug` | `false` | Emit discovery/provisioning-plan debug output |
| Optional | `storage_part_suffix_devices` | `[nvme, mmcblk, loop, nbd]` | Basename prefixes that use `p` before the partition number (`nvme0n1p1` vs `sda1`) |
| Optional | `storage_part_suffix_devices_extra` | `[]` | Append more prefixes without restating the built-in list |
| When NFS | `storage_manage_nfs` | `false` | Mount declared `kind: nfs` volumes — without it they are skipped, not failed |
| When CIFS | `storage_manage_cifs` | `false` | Mount declared `kind: cifs` volumes — without it they are skipped, not failed |
| Optional | `storage_allow_nested_mounts` | `false` | Allow a network mount nested inside another declared mount (`/data` + `/data/sub`) |
| Optional | `storage_cifs_credentials_dir` | `/etc/cifs-credentials` | Root-owned `0700` directory holding one `0600` credentials file per CIFS volume |
| Optional | `storage_net_protected_mounts` | `[/, /boot, /etc, /usr, /var, /home]` | Mount points a **network** volume may never claim |
| Optional | `storage_default_nfs_fstype` / `storage_default_cifs_fstype` | `nfs4` / `cifs` | `fstype` applied per kind when an entry omits it |
| Optional | `storage_default_nfs_opts` / `storage_default_cifs_opts` | see `defaults/main.yml` | `opts` applied per kind when an entry omits it (`_netdev` + `nofail` always appended) |

## Storage profiles

A profile is a **named preset volume list**: declare the estate's common
layouts once, then select one per host group with a single var. The catalog
lives in `playbooks/group_vars/all/storage.yml` (do **not** redefine
`storage_profiles` in inventory — list/dict vars replace, not merge, and you
would drop every other profile).

Resolution precedence: `storage_profiles[storage_profile]` when
`storage_profile` is set, else `storage_volumes`.

Catalog highlights (see the file for all of them, including a two-disk
profile that mixes selectors and a plain non-LVM scratch volume):

```yaml
storage_profiles:
  # Grow-only /var on the OS disk (single-disk guests, no data disk).
  sys_var:
    - name: var
      lvm: true
      vg: sysvg
      lv: lv_var
      mount: /var
      partition_number: 3
      provision: false          # adopt the installer's stack, never create
      grow: true

  # Single second disk, unpinned — only safe with exactly one blank disk.
  app_auto:
    - name: opt
      disk: auto
      lvm: true
      vg: vg_data
      lv: lv_opt
      size: "100%FREE"
      fstype: xfs
      mount: /opt
      sefcontext: usr_t

  # The same volume pinned by exact size — the form to prefer. Pair the pin
  # with whatever provisions the guest's disks, so multi-disk guests never
  # grab the wrong blank disk.
  app_50g:
    - name: opt
      disk: "by-size:50G"
      lvm: true
      vg: vg_data
      lv: lv_opt
      size: "100%FREE"
      fstype: xfs
      mount: /opt
      sefcontext: usr_t
```

Select one per group:

```yaml
# group_vars/app_hosts.yml
storage_profile: app_50g
storage_provision: true          # still needed when first-time create is intended
```

Profiles compose with everything else: a profile-selected list flows through
the same validation, discovery, and phases as an inline `storage_volumes`.

## Minimum configuration

```yaml
# group_vars/storage_hosts.yml
---
# Required — one entry per volume; every field except `name` has a default
storage_volumes:
  - name: data
    disk: auto
    vg: vg_data
    lv: lv_data
    mount: /srv/data

  - name: archive
    kind: nfs
    server: nas.example.internal
    export: /export/archive
    mount: /srv/archive

  - name: media
    kind: cifs
    server: files.example.internal
    share: media
    mount: /srv/media
    credentials_username: svc-storage
    credentials_password: "{{ vault_storage_cifs_password }}"

# When creating volumes — this example creates the local stack
storage_provision: true

# When network volumes are declared — one gate per kind
storage_manage_nfs: true
storage_manage_cifs: true
```

## Usage

Via the ops playbook (the normal path):

```bash
export ANSIBLE_VAULT_PASSWORD=$(cat ~/secrets/vault-password.txt)
ansible-playbook -i inventories/<env>/hosts.yml playbooks/ops_storage.yml -e storage_target=storage_hosts
```

Inline in a play, with role vars:

```yaml
- name: Manage storage
  hosts: workers
  roles:
    - role: storage
      tags: [storage]
      vars:
        storage_provision: true
        storage_volumes:
          - name: opt
            disk: auto
            vg: vg_data
            lv: lv_opt
            mount: /opt
```

Local and network volumes live in the same list:

```yaml
- name: Stroom storage
  hosts: stroom
  roles:
    - role: storage
      vars:
        storage_manage_nfs: true
        storage_volumes:
          - name: stroom-index          # local — kind defaults to 'local'
            disk: auto
            vg: vg_stroom
            lv: lv_index
            mount: /stroomdata/stroom-index-p00

          - name: stroom-data
            kind: nfs
            server: nas.example.internal
            export: /mnt/user/stroom-data
            mount: /stroomdata/stroom-data-p00
            opts: "hard,noatime,x-systemd.mount-timeout=30"
```

The role escalates privilege on the individual tasks that need root, so the
play carries no `become`.

## Preconditions

- The prerequisite packages (`parted`, `lvm2`, `xfsprogs`, `e2fsprogs`, the
  growpart utility, SELinux policy tools) must be installable. Where the base
  repositories are entitlement-gated, register the host first, pre-bake the
  tools into the image, or set `storage_manage_packages: false`.
- Network volumes need the export or share to already exist on the server, and
  the host to be permitted by its access controls — this role mounts, it does
  not create shares or open network paths.
- Provisioning on a host whose `/` is not a block device (container overlayfs,
  a dataset-backed root) requires every volume to name an explicit
  `disk: /dev/...` — see **Behaviour**.

## Behaviour

**Phases and ordering**

- A no-tags run is a full idempotent reconcile:
  `resolve → validate → packages → discover → grow → provision → mount →
  mount_net → selinux`. Normalisation and validation carry every phase tag, so
  they run before any phase acts whichever tag selected it; `discover`
  (read-only device discovery and selector resolution) runs under every acting
  tag, because the other phases consume its facts.
- **The declaration is validated before anything acts**, failing fast with a
  message naming the offending volume(s): `kind` is one of `local` / `nfs` /
  `cifs`; local volumes declare no network fields and network volumes no
  block-device fields; `nfs` requires `server` + `export` and `cifs` requires
  `server` + `share`; every network volume declares a `mount`; no two volumes
  claim the same mount point; and each `disk` selector uses a supported,
  unpadded form (`auto`, `/dev/...`, `by-size:`, `by-serial:`, `by-wwn:`).
- **Network mount points are constrained further**: absolute, never one of
  `storage_net_protected_mounts`, never nested inside another declared mount
  unless `storage_allow_nested_mounts` is `true`, never already serving a
  different source (probed with `findmnt`), and never carrying `sefcontext` —
  host-side file contexts are local-only. That protected list applies to
  network volumes only; `/var` and its children are legitimate local mount
  points.
- **No two local LVM volumes may share a `vg`.** `discover` computes volume
  group existence once, before `provision` runs, so two volumes creating the
  *same new* group both see "absent" and both pass the provision filter. The
  second `lvg` call is then given a single-PV list and reduces the first
  volume's PV back out of the group. The role refuses the declaration rather
  than relying on that collision being unreachable.

**Grow and provision**

- **Grow is automatic and non-destructive** — it only enlarges an existing
  stack after the underlying disk grew (rescan → growpart → pvresize →
  lvextend → filesystem grow). `xfs` grow needs a live mount point: when the
  path is not mounted, LV and PV extension still run and the filesystem grow
  is **skipped with a warning** rather than aborting the play before the mount
  phase — re-run afterwards to finish it. `ext4` grows by device and does not
  need the mount.
- **Provisioning is armed by `storage_provision: true` and nothing else**, and
  is FRESH-guarded (`storage_require_fresh`, default `true`): it refuses any
  disk carrying an existing filesystem or partition signature. FRESH requires
  a real block device (`test -b`) and a clean signature probe — an unreadable
  path is never treated as empty.

**Root protection**

- The disk backing `/` is discovered at runtime. Three guards derive from it
  and from nothing else: `auto` rejects it, the `by-size:` / `by-serial:` /
  `by-wwn:` catalogue rejects it before matching, and the role refuses to
  create data volumes in a volume group whose PVs sit on it.
- On a block-backed root, discovery **fails closed** when no root disk can be
  identified. An empty list would turn all three guards into no-ops that still
  report success, so it is treated as an error rather than waved through.
- On a root that is not a block device there is no OS disk to name, so the
  role **degrades loudly**: it prints a warning at default verbosity naming
  the three guards as inert, and refuses implicit disk selection (`auto`,
  empty, `by-size:`, `by-serial:`, `by-wwn:`) for any volume that would be
  provisioned — those selectors pick from a pool that could not exclude the OS
  disk. An explicit `disk: /dev/...` is still allowed: that is the operator
  naming the device, and it is the escape hatch that keeps the role usable on
  such hosts. Read-only work is untouched — the refusal is gated on
  `storage_provision`, so discover, grow, and mount keep working.

**Mounting local volumes**

- **A declared `mount` with no filesystem fails the play** — it is never
  skipped with a warning. Per-volume `provision` does not mean "this volume is
  optional"; it selects who creates the filesystem. `provision: true` lets the
  role build the PV/VG/LV and run `mkfs`, so a missing filesystem means the
  role-level `storage_provision` opt-in was never passed. `provision: false`
  means adopt a filesystem created elsewhere, so a missing filesystem means
  the adoption target is not there — wrong disk matched, disk swapped, volume
  group not activated. Both are errors; the failure message names which.
- The assertion exists because the mount phase creates the mount-point
  directory **before** it probes for a filesystem UUID. Without it, an
  unformatted device leaves an empty directory on the root filesystem, nothing
  mounts over it, the play reports success, and the service writes its data to
  the OS disk — surfacing weeks later as a full root volume with every run
  green in between.
- **That failure aborts the host.** Every phase runs inside one `block:` with
  no `rescue:`, so volumes later in the same list never mount even when they
  are healthy (the mount loop expands all iterations before the first failure
  lands), and `mount_net` and `selinux` do not run at all. The empty
  mount-point directory is left behind. The effect is order-sensitive — a bad
  volume at the end of the list lets the earlier ones mount — so a partial
  mount set is not meaningful; fix and re-run. Recovery is either
  `storage_provision: true` for a stack that needs creating or finishing, or
  correcting the disk selector / attaching the right disk for an adopt-only
  volume.
- After a successful mount, `owner` / `group` / `mode` are re-applied on the
  live filesystem root, so the first provisioning run sets them rather than
  only a second converge.

**Network volumes**

- **Network volumes bypass the block layer entirely.** The list is split by
  `kind` before any phase runs: `discover`, `grow`, `provision`, `mount`, and
  `selinux` see local volumes only.
- **Network kinds are opt-in per kind.** A declared `nfs` / `cifs` volume
  whose `storage_manage_<kind>` flag is `false` is skipped with a message
  naming the flag — never a failure, so a host can carry the declaration ahead
  of the server being ready.
- `_netdev` and `nofail` are force-appended to every network mount: without
  `_netdev`, systemd may order the mount before the network is up and hang
  boot. Option strings are de-duplicated and sorted so the mount module does
  not rewrite `/etc/fstab` on every run.
- **CIFS passwords never reach `/etc/fstab`**, which is world-readable. They
  are written to `<storage_cifs_credentials_dir>/<name>.cred`, mode `0600`
  `root:root`, and referenced by `credentials=<path>`; any operator-supplied
  `credentials=` option is dropped in favour of the managed one. Supply the
  password from a secret store through inventory, never inline.
- `hard` is the default NFS mount option: an archive or audit mount should
  stall when the server goes away rather than silently truncate writes.
- Mount-point `owner` / `group` / `mode` are applied to a network volume's
  directory only while it is still empty; once the remote filesystem is
  mounted, ownership belongs to the server.
- Each network mount is read back with `findmnt` and asserted to be serving
  the declared source.

**Partition device naming**

Linux names partitions two ways:

| Disk basename | Partition 1 |
|---|---|
| `sda`, `vda`, `xvda`, … | `sda1` (bare number) |
| `nvme0n1`, `mmcblk0`, `loop0`, `nbd0`, … | `nvme0n1p1` (`p` + number) |

Discovery builds the partition device from that rule. If a new controller
needs the `p` form, **do not fork the role** — set inventory vars:

```yaml
# Append only (preferred)
storage_part_suffix_devices_extra:
  - mynewctrl

# Or replace the full list
storage_part_suffix_devices: [nvme, mmcblk, loop, nbd, mynewctrl]
```

## Out of scope

- Creating exports, shares, export ACLs, or network paths — this role is a
  client only.
- iSCSI, multipath, and any other block-over-network transport.
- SMB1/NT1, and Kerberised (`sec=krb5`) NFS — the client path is static
  `sec=sys` mounts.
- autofs / automounter maps — mounts are static fstab entries.
- Per-user CIFS credentials — one machine credential per volume, root-owned.
- Disk encryption, software RAID, and backups.

## Expected result

- Every declared local volume has its block stack present, a filesystem
  created or adopted, and is mounted at its declared path.
- Each local mount point has exactly one `/etc/fstab` entry, keyed by `UUID=`
  and carrying `nofail`; network mounts additionally carry `_netdev`.
- Verify: `findmnt <mount>` for the live mount, `findmnt --fstab <mount>` for
  the persistent entry.

## Tag safety

- `--tags provision` does **not** arm provisioning. Provisioning is armed by
  `storage_provision: true`; the tag narrows an already-armed run to the
  provision phase. Selecting the tag without the variable runs the phase and
  skips every task inside it.
- `discover` runs under `grow`, `provision`, `mount`, and `selinux` as well as
  its own tag — those phases consume its facts and cannot run without it.
- Normalisation and validation are tagged with the union of every phase tag, so
  no tag selection can act on an unvalidated declaration — and a tag this role
  does not own selects nothing from it.
