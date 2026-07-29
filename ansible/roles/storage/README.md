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

## Key variables

Full list: `defaults/main.yml`. Contract, including the per-volume field
schema for `storage_volumes` entries: `meta/argument_specs.yml`.

**Required** = value must be correct for a successful run (defaults often exist).
**Optional** = safe to leave default / empty.
**When X** = required only if that feature is on.

| Req | Variable | Default | Purpose |
|---|---|---|---|
| **Required** | `storage_volumes` | `[]` | Declarative volume list driving every phase — empty means nothing to do |
| Optional | `storage_profile` / `storage_profiles` | `""` / `{}` | Select a named preset from a catalogue instead of declaring `storage_volumes` inline |
| Optional | `storage_provision` | `false` | Arms creation and formatting — the only switch that does |
| Optional | `storage_require_fresh` | `true` | Provisioning refuses a disk carrying an existing filesystem or partition signature |
| When NFS | `storage_manage_nfs` | `false` | Mount declared `kind: nfs` volumes — without it they are skipped, not failed |
| When CIFS | `storage_manage_cifs` | `false` | Mount declared `kind: cifs` volumes — without it they are skipped, not failed |
| Optional | `storage_cifs_credentials_dir` | `/etc/cifs-credentials` | Root-owned `0700` directory holding one `0600` credentials file per CIFS volume |

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

```yaml
- name: Manage storage
  hosts: storage_hosts
  roles:
    - role: storage
      tags: [storage]
```

The role escalates privilege on the individual tasks that need root, so the
play carries no `become`.

Run:

```bash
export ANSIBLE_VAULT_PASSWORD=$(cat ~/secrets/vault-password.txt)
ansible-playbook -i inventories/<env>/hosts.yml playbooks/ops_storage.yml -e storage_target=storage_hosts
```

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

**Device selection**

- `disk: auto` picks the first blank non-root disk in kernel enumeration
  order, and that order is **not stable** across reboots, controllers, or
  cloud platforms. With two or more blank disks where placement matters, pin a
  stable selector (`by-size:`, `by-serial:`, `by-wwn:`, or an explicit
  `/dev/...` path) instead.
- Partition names are derived from the disk basename: `sda` → `sda1`, but
  `nvme0n1` → `nvme0n1p1`. The basename prefixes that take the `p` form are
  listed in `storage_part_suffix_devices`; add a new controller through
  `storage_part_suffix_devices_extra` rather than forking the role.

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
