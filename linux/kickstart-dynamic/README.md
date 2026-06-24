# Dynamic kickstart + Ansible re-IP — bootstrap a host from bare metal/VM

A proof-of-concept for **zero-touch provisioning** of any RHEL-family host
(RHEL / Oracle Linux / AlmaLinux / Rocky, EL8/9/10), physical or virtual:

1. **Kickstart** installs the OS and brings the host up on a **known, hardcoded
   staging IP** — auto-detecting the disk, firmware and NIC so the *same file*
   works on a 40 GB VM or a 2 TB physical server.
2. **Ansible** connects to that staging IP over SSH (key-based, passwordless
   sudo) and **re-provisions it**, including giving it its **proper permanent IP**
   and hostname.

The point: you never hand-edit per-machine, and you never rebuild a box just to
grow a partition.

```
kickstart-dynamic/
├── ks-rhel-family-dynamic.cfg   the kickstart (auto-detect + LVM headroom)
├── ip-rename.yml                Ansible playbook: staging IP -> permanent IP
├── boot-args.example            how to feed the kickstart to Anaconda
└── README.md                    this walkthrough
```

> All addresses here use the RFC5737 documentation range `192.0.2.0/24` and the
> domain `example.com` — replace with your own. The kickstart ships two
> placeholders: `__ROOTPW_HASH__` and the `ssh-ed25519` automation public key.

---

## Why it never needs a rebuild for "out of disk"

`%pre` grows the LVM **physical volume to fill the whole disk**, but creates each
logical volume at a **fixed size**. Everything not allocated stays as **free
extents in the volume group** — instant headroom you hand to whichever mount runs
tight, online:

```bash
lvextend -r -L +200G /dev/vg_system/var     # -r grows the xfs filesystem too; no reboot
vgs                                          # shows the spare space still free in the VG
```

| Disk | VG | LVs use | **Free headroom** |
|------|----|---------|-------------------|
| 40 GB VM | ~38 GB | ~34 GB | ~4.6 GB |
| 1 TB physical | ~998 GB | ~34 GB | **~964 GB** |

Same kickstart, any disk. Fixed sizes (not percentages) mean a big physical disk
leaves almost the whole drive free in the VG until *you* decide where it goes.

## What `%pre` auto-detects (so one file fits all hardware)

- **Disk** — first fixed, non-removable device, preferring `nvme* > vd* > sd* > hd*`
  (USB/removable skipped). NVMe and SATA/SCSI both just work.
- **Firmware** — `/sys/firmware/efi` → UEFI (`/boot/efi` ESP) or BIOS (`biosboot`).
- **NIC** — first link-up physical interface; no hardcoded `ens192`/`eth0`.
- **Static IP** — boot args `ksip= ksgw= ksdns= kshostname=` override the baked-in
  fallback `192.0.2.250/24`. See `boot-args.example`.

## Partition layout (CIS/STIG-aware, developer-friendly)

`/` 12G · `/var` 6G · `/var/log` 3G · `/var/log/audit` 2G · `/var/tmp` 2G ·
`/tmp` 3G · `/home` 2G · swap (RAM-based, capped 4G) · `/boot` 1G ·
`/boot/efi` 600M (UEFI). Auto-scales down on disks smaller than the footprint,
`/` floored at 10 GB.

Every separate filesystem gets `nodev,nosuid`. **`noexec` is applied only to the
log dirs** (`/var/log`, `/var/log/audit`) and `/boot` — *not* to `/home`, `/tmp`,
`/var/tmp` or `/var`, because:

- developers legitimately run scripts from home and `/tmp`;
- Ansible's `become_user`-to-non-root temp fallback executes modules from `/tmp`//`var/tmp`;
- RPM/dnf scriptlets execute out of `/var/tmp`.

For a locked-down STIG host, add `noexec` back to `/tmp`//`var/tmp` in the
`fsopts=()` array and point Ansible `remote_tmp` / RPM `_tmppath` at a non-`noexec`
path.

---

## End-to-end walkthrough

### 1. Fill the two placeholders

```bash
# root password hash (console/break-glass only; root SSH is disabled)
openssl passwd -6 'choose-a-strong-passphrase'        # -> $6$...
sed -i "s#__ROOTPW_HASH__#<paste the \$6\$ hash>#" ks-rhel-family-dynamic.cfg

# automation SSH key — paste your Ansible account's PUBLIC key into the
# authorized_keys heredoc in the %post section.
```

> In a real pipeline, inject the hash at build time from your secrets manager
> instead of committing it.

### 2. Build an unattended install ISO (OEMDRV)

Anaconda automatically runs a kickstart found on a volume labelled `OEMDRV` — no
boot-menu editing, no PXE required:

```bash
cp ks-rhel-family-dynamic.cfg ks.cfg
xorriso -as mkisofs -V OEMDRV -o oemdrv.iso -J -r ks.cfg
```

### 3. Boot the target (physical or VM)

- **VM:** attach the installer DVD as CD1 and `oemdrv.iso` as CD2; boot from CD.
- **Physical:** write the installer to one USB and `oemdrv.iso` to another, boot
  the installer. (Add `console=ttyS0,115200` via `boot-args.example` to watch over
  serial/IPMI.)

Anaconda finds `OEMDRV`, runs the kickstart unattended, partitions with LVM +
headroom, creates the `ansible` account, and powers off. The host comes back up on
the **staging IP `192.0.2.250`**.

### 4. Ansible connects and gives it its real identity

```bash
ansible-galaxy collection install community.general          # for the nmcli module

export ANSIBLE_HOST_KEY_CHECKING=False
export ANSIBLE_PRIVATE_KEY_FILE=~/.ssh/ansible.pem
ansible-playbook -i '192.0.2.250,' -u ansible ip-rename.yml \
  -e new_hostname=web-01 -e new_ip=192.0.2.55
```

What happens:

```
TASK [Set permanent hostname]                              changed
TASK [Stage the permanent static IP into the connection profile]  changed  (community.general.nmcli)
TASK [Reboot to apply the new IP (fire-and-forget)]        changed
PLAY [Confirm the host on its permanent IP]
TASK [Wait for SSH on the new IP]                          ok: [192.0.2.55]
TASK [Report success] "web-01 is up on 192.0.2.55 ... ready for role-based provisioning."
PLAY RECAP   192.0.2.55: ok=3 failed=0 unreachable=0
```

The host is now on `192.0.2.55` as `web-01`, reachable by Ansible with passwordless
sudo — ready for your normal role-based playbooks.

> **Why a reboot?** `community.general.nmcli` writes the new IP into the NM
> connection *profile* but does not reactivate the live link, so the change lands
> on next boot. The playbook registers the new IP as a second-play target *before*
> the change, then reboots fire-and-forget and reconnects on the new address — so a
> single `ansible-playbook` run does the whole flip and verifies it.

---

## How the connection works (no `/tmp` exec needed)

Ansible copies its Python modules to `~/.ansible/tmp/` on the remote — the
connecting user's home. You connect as `ansible`, so modules run from
`/home/ansible/.ansible/tmp/`, and `/home` is **not** `noexec`, so it just works
out of the box. The dev-friendly mount options above also cover the `become_user`
and RPM cases.

## Why this is "production-shaped" without bloat

The kickstart does only what *must* happen at install time — partition layout +
mount options (irreversible without a rebuild), kdump reservation
(`%addon com_redhat_kdump`, since the EL8-only `crashkernel=auto` is invalid on
EL9+), and the SSH handoff account. Everything else — subscription/repos, identity
join, NTP servers, full CIS remediation, monitoring agents — is left to Ansible, so
the image stays generic and config stays centrally managed.

## Validation

This flow was validated end-to-end on a hypervisor: a blank UEFI VM installed
unattended from the OEMDRV ISO, came up on the staging IP, and a single
`ip-rename.yml` run moved it to its permanent IP and hostname with passwordless
sudo confirmed on the new address. The CIS mount options, kdump, free VG headroom,
and an online `lvextend` (grow `/var` with no reboot) were all confirmed on the
running host.
