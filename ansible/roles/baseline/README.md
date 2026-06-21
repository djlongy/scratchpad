# baseline

Distro-agnostic **SOE Linux baseline** (AlmaLinux / RHEL / Ubuntu). Establishes a
consistent, hardened foundation before app roles run. Everything beyond the core
OS setup is **opt-in via override host/group vars**, so enabling a capability
never surprises an existing fleet.

> **openclaw moved out** — the bot account now lives in the dedicated
> [`soe`](../soe/) role. Apply `soe` alongside `baseline` where needed.

## What it does

Core (always): hostname/timezone, packages, `/opt` data-disk mount, logging &
auditd tuning, breakglass local-admin, SSH hardening, IPv6 toggle, fail2ban
(Ubuntu), borg backup hooks.

SOE-desktop (opt-in, distro-agnostic):

| Area | Toggle | Notes |
|---|---|---|
| **Network** (IP/DNS) | `baseline_network_manage` | NetworkManager/`nmcli`; dynamic interface (`ens*/eth*/eno*`) |
| **Firewall** (zones/services) | `baseline_firewall_manage` | firewalld; custom service/zone XML templates |
| **SELinux** (EL) | `baseline_selinux_state` | set `enforcing`/`permissive`/`disabled` |
| **fapolicyd** (EL) | `baseline_fapolicyd_enabled` | template-driven config + custom rules; permissive by default |
| **CIS hardening** | `baseline_cis_hardening` | pragmatic CIS L1 subset, **no FIPS** |
| **IPv6 disable** | `baseline_disable_ipv6` | sysctl, per-host |

## Usage

```yaml
- hosts: soe_desktops
  roles:
    - baseline
    - soe        # openclaw etc.
```

## Network

```yaml
baseline_network_manage: true
# interface auto-detected; override if needed:
baseline_network_interface: ens192
baseline_network_ip4: "192.0.2.50/24"     # default {{ ansible_host }}/prefix
baseline_network_gateway: "192.0.2.1"     # default .1 of the host /24
baseline_network_dns: ["192.0.2.53"]
baseline_network_search: ["example.com"]
```

Ubuntu Server uses netplan (not NetworkManager) — leave `baseline_network_manage:
false` there and manage netplan out of band. Ubuntu **Desktop** uses NM and works.

## Firewall (zones / services)

```yaml
baseline_firewall_manage: true
baseline_firewall_zone: public
baseline_firewall_bind_interface: true     # bind primary iface to the zone
baseline_firewall_services: [ssh, https]
baseline_firewall_ports: ["8080/tcp"]

# Custom firewalld service/zone definitions, rendered from the shared XML
# templates (same schema as the firewalld role):
baseline_firewall_custom_services:
  - name: myapp
    short: "My App"
    ports: [{port: "8080", protocol: tcp}]
baseline_firewall_custom_zones:
  - name: dmz-ingress
    target: default
    services: [ssh, https, myapp]
```

Custom service XML lands in `/etc/firewalld/services/<name>.xml` and zones in
`/etc/firewalld/zones/<name>.xml`, then firewalld reloads before bindings apply.

## SELinux (EL)

```yaml
baseline_selinux_state: enforcing          # "" = no change
baseline_selinux_policy: targeted
```

## fapolicyd (EL)

Template-driven (`fapolicyd.conf` + custom `rules.d`), permissive by default —
validate in permissive before enforcing.

```yaml
baseline_fapolicyd_enabled: true
baseline_fapolicyd_permissive: true        # set false only after validation
baseline_fapolicyd_rules:
  - "allow perm=any all : dir=/opt/myapp/"
```

## CIS hardening (no FIPS)

```yaml
baseline_cis_hardening: true
```

A pragmatic CIS Level-1 **subset** — network sysctls, core-dump restriction,
insecure-module blacklist, pwquality, password aging. **Deliberately never
enables FIPS.** `ip_forward` is left untouched (routers/k8s need it). For full,
certifiable CIS use [ansible-lockdown](https://github.com/ansible-lockdown).
All control lists (`baseline_cis_sysctl`, `_blacklist_modules`, `_pwquality`,
`_login_defs`) are overridable.

## Trusted CAs (OS trust store)

Install your own CA(s) so the host trusts internal TLS (Artifactory/Nexus/etc.).
Distro-agnostic (EL `update-ca-trust`, Debian `update-ca-certificates`):

```yaml
baseline_trusted_cas:
  - {name: corp-root, src: files/corp-root.pem}
  - {name: nexus-ca, content: "-----BEGIN CERTIFICATE----- ..."}
# Or pull from a URL (no need to commit the cert to the repo):
baseline_trusted_ca_urls:
  - {name: corp-root, url: "https://pki.example.com/ca/root.pem"}
  - {name: nexus-ca, url: "https://nexus.example.com/ca.crt", checksum: "sha256:..."}
```

## Custom repositories (air-gapped)

Point hosts at an internal mirror (Artifactory/Nexus) instead of the internet:

```yaml
# EL
baseline_dnf_repos:
  - {name: corp-baseos, baseurl: "https://nexus.example.com/repository/baseos/", gpgcheck: true, gpgkey: "https://nexus.example.com/RPM-GPG-KEY"}
baseline_dnf_repos_disable: [baseos, appstream]   # disable stock repos for true air-gap
# Debian
baseline_apt_repos:
  - {repo: "deb [trusted=yes] https://nexus.example.com/repository/apt jammy main", filename: corp}
```

## pip index + packages

Point pip at an internal PyPI mirror and install packages through it:

```yaml
baseline_pip_index_url: "https://nexus.example.com/repository/pypi/simple"
baseline_pip_trusted_host: "nexus.example.com"
baseline_pip_packages: [requests, pyvmomi]
```

## Tags

`baseline` (everything), plus per-area: `ca_trust`, `repos`, `pip`, `network`,
`firewall`, `selinux`, `fapolicyd`, `cis`/`hardening`, `ipv6`, `logging`,
`backup`, `breakglass`, `ssh`. `reboot` is `never`-tagged (explicit opt-in).

See `defaults/main.yml` for the full variable surface.
