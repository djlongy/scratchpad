# clamav

## TL;DR

Installs and configures ClamAV antivirus on Linux hosts: base packages (scanner +
freshclam), freshclam configuration and initial signature database seed, an optional
resident `clamd` daemon, and an optional scheduled scan via a systemd timer. Distro
package names, config paths, and service names are keyed on `ansible_os_family`
(`RedHat` / `Debian`) in `vars/main.yml`.

```bash
ansible-playbook -i inventories/<env>/hosts.yml playbooks/<playbook>.yml --tags scan
```

## Requirements

None beyond `ansible.builtin`.

## Key variables

Full list: `defaults/main.yml`. Contract: `meta/argument_specs.yml`.

**Required** = value must be correct for a successful run (defaults often work).
**Optional** = safe to leave default / empty; phase stays off or uses built-ins.
**When X** = required only if that feature is on.

| Req | Variable | Default | Purpose |
|---|---|---|---|
| Optional | `clamav_enable_freshclam` | `true` | Enable and start the freshclam update service |
| Optional | `clamav_database_mirror` | `""` | Private/air-gap mirror URL; empty = distro default |
| Optional | `clamav_enable_daemon` | `false` | Install and start the resident `clamd` daemon (~700 MB RAM) |
| Optional | `clamav_scan_enabled` | `false` | Deploy and enable the systemd scheduled scan |
| Optional | `clamav_scan_paths` | `[/home, /tmp, /var/tmp]` | Directories to scan recursively |
| Optional | `clamav_scan_schedule` | `"Sun *-*-* 02:00:00"` | systemd `OnCalendar` expression |
| Optional | `clamav_remove_infected` | `false` | Pass `--remove` to clamscan (destructive) |
| Optional | `clamav_scan_log` | `/var/log/clamav/scan.log` | Path for the scheduled-scan log |
| When FIPS mode (EL9) | `clamav_fips_compatible` | `false` | Set true only once ClamAV ≥ 1.5 is installed (older ClamAV uses MD5, unusable under FIPS) |
| When air-gap / custom repo | `clamav_repo_baseurl` / `_gpgkey` | `""` | Internal mirror to obtain ClamAV ≥ 1.5; empty = distro/EPEL packages |

## Usage

```yaml
- hosts: fileservers
  become: true
  roles:
    - role: clamav
      vars:
        clamav_enable_daemon: true
        clamav_scan_enabled: true
        clamav_scan_paths: [/home, /srv/shares, /tmp]
```

Run it:

```bash
ansible-playbook -i inventories/<env>/hosts.yml playbooks/<playbook>.yml --tags scan
```

## Preconditions

**RedHat/EL only — EPEL required.** ClamAV packages ship in EPEL, not the base repos.
EPEL must be enabled on the target host before this role runs; the role does not
install EPEL itself. Debian/Ubuntu: all packages are in the main archive, no extra
repo needed.

## Behaviour

Setting `clamav_database_mirror` writes `DatabaseMirror <url>` into `freshclam.conf`;
leaving it empty leaves the distro default (`db.local.clamav.net`) in place.

On Debian/Ubuntu the `clamav-freshclam` package ships a pre-enabled systemd service
that may auto-seed the database on first install. The configure phase guards the
initial `freshclam` run with a stat check on `main.cvd`/`main.cld` so a double-download
never occurs.
