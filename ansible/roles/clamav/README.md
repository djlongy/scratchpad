# clamav

Installs and configures ClamAV antivirus on Linux hosts. Manages:

- Base packages (scanner + freshclam)
- Freshclam configuration and initial signature database seed
- Optional resident `clamd` daemon (on-access / fast-scan client)
- Optional scheduled scan via systemd timer + oneshot service

Distro-agnostic — package names, config paths, and service names are keyed on
`ansible_os_family` (`RedHat` / `Debian`) in `vars/main.yml`.

## Prerequisites

**RedHat/EL only — EPEL required.**  ClamAV packages ship in EPEL, not the
base repos. EPEL must be enabled on the target host before this role runs. The
role does not install EPEL; rely on your baseline role or
`ansible.builtin.dnf: name: epel-release` before invoking `clamav`.

Debian/Ubuntu: all packages are in the main archive; no extra repo needed.

## Air-gap / private mirror

Set `clamav_database_mirror` to your internal mirror URL.  When set, the role
writes `DatabaseMirror <url>` into `freshclam.conf`; when empty (the default)
the distro default (`db.local.clamav.net`) is left in place.

## Tags

| Tag | Phase |
|-----|-------|
| `install` | Package installation |
| `configure` | freshclam.conf + initial DB seed |
| `service` | systemd service enable/start |
| `scan` | Scheduled scan timer + service unit deployment |

A no-tags run is a full idempotent reconcile.  Use tags to iterate quickly:

```bash
# Re-apply freshclam config only
ansible-playbook site.yml --tags configure

# Redeploy scan units only
ansible-playbook site.yml --tags scan
```

## Variable reference

| Variable | Default | Description |
|----------|---------|-------------|
| `clamav_enable_freshclam` | `true` | Enable and start the freshclam update service |
| `clamav_database_mirror` | `""` | Private/air-gap mirror URL; empty = distro default |
| `clamav_enable_daemon` | `false` | Install and start the resident `clamd` daemon |
| `clamav_scan_enabled` | `false` | Deploy and enable the systemd scheduled scan |
| `clamav_scan_paths` | `[/home, /tmp, /var/tmp]` | Directories to scan recursively |
| `clamav_scan_schedule` | `"Sun *-*-* 02:00:00"` | systemd `OnCalendar` expression |
| `clamav_remove_infected` | `false` | Pass `--remove` to clamscan (destructive) |
| `clamav_scan_log` | `/var/log/clamav/scan.log` | Path for the scheduled-scan log |

## Examples

### Minimal — freshclam only (no daemon, no scheduled scan)

```yaml
- hosts: webservers
  become: true
  roles:
    - role: clamav
```

### Enable the resident daemon and scheduled scans

```yaml
- hosts: fileservers
  become: true
  roles:
    - role: clamav
      vars:
        clamav_enable_daemon: true
        clamav_scan_enabled: true
        clamav_scan_paths:
          - /home
          - /srv/shares
          - /tmp
        clamav_scan_schedule: "Mon..Fri *-*-* 03:30:00"
        clamav_scan_log: /var/log/clamav/daily-scan.log
```

### Air-gap environment with internal mirror

```yaml
- hosts: isolated_hosts
  become: true
  roles:
    - role: clamav
      vars:
        clamav_database_mirror: "https://mirror.corp.example.com/clamav"
        clamav_enable_freshclam: true
```

## Distro notes

| Family | Base packages | Daemon packages | freshclam.conf |
|--------|---------------|-----------------|----------------|
| RedHat/EL (EPEL) | `clamav`, `clamav-update` | `clamd`, `clamav-server`, `clamav-server-systemd` | `/etc/freshclam.conf` |
| Debian/Ubuntu | `clamav`, `clamav-freshclam` | `clamav-daemon` | `/etc/clamav/freshclam.conf` |

On Debian/Ubuntu the `clamav-freshclam` package ships a pre-enabled systemd
service that may auto-seed the database on first install. The configure phase
guards the initial `freshclam` run with a stat check on `main.cvd`/`main.cld`
so a double-download never occurs.
