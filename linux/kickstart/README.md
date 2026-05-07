# EL9 Hardened Kickstart

Drop-in kickstart for unattended installs of a hardened EL9 workstation
or server. Tested on Oracle Linux 9.7; works the same way on RHEL 9,
AlmaLinux 9, and Rocky Linux 9.

## What you get

- GNOME desktop (`@gnome-desktop` group)
- **fapolicyd** enabled at boot, with a working VS Code allow rule
  pre-seeded under `/etc/fapolicyd/rules.d/31-vscode.rules` (saves the
  "VS Code installed in `%post` after fapolicyd's initial DB was built"
  trap — see [`linux/fapolicyd/`](../fapolicyd/) for the full
  troubleshooting story).
- **FIPS** mode enabled (`fips=1` kernel arg + `fips-mode-setup --enable`)
- **STIG** scan run via `oscap` against the
  `xccdf_org.ssgproject.content_profile_stig_gui` profile, with results
  written to `/root/stig-report.html` for review on first boot.
- **usbguard** seeded with a VMware passthrough rule so virtualised
  installs stay usable (USB peripherals don't get blocked at boot).
- LVM partitioning on the first disk, DHCP on the first NIC, SELinux
  enforcing, firewall on with SSH allowed.

## Usage

Boot from an EL9 install ISO and pass:

```
inst.ks=http://<server>:8080/ks-el9-hardened.cfg
```

(serve via `python3 -m http.server 8080` from this directory, or your
preferred kickstart host).

Search the cfg for `CHANGEME` before deploying — locale, hostname, root
and admin passwords need real values. Production should also swap the
plaintext passwords for `--iscrypted` SHA-512 hashes.

## Related

- [`linux/fapolicyd/`](../fapolicyd/) — debugging fapolicyd denials and
  whitelisting more applications post-install.
- [`ansible/files/fapolicyd-rule-templates/`](../../ansible/files/fapolicyd-rule-templates/)
  — Jinja templates for app-specific allow rules consumed by
  [`ansible/roles/common/tasks/fapolicyd.yml`](../../ansible/roles/common/tasks/fapolicyd.yml).
