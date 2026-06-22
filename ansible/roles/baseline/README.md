# baseline

Distro-agnostic **SOE Linux baseline** (AlmaLinux / RHEL / Ubuntu). Establishes a
consistent, hardened foundation before app roles run. Everything beyond the core
OS setup is **opt-in via override host/group vars**, so enabling a capability
never surprises an existing fleet.

## Execution order

Phases run in a deliberate dependency order, each idempotent and individually
taggable. Running with **no tags runs everything**; tags only *narrow* the run.

```
identity (hostname/tz)
  → storage (disks/mounts via the `storage` role)
    → CA trust            (trust corp PKI before talking to mirrors)
      → proxy             (Squid client — before any upstream fetch)
        → repos           (RHSM/EPEL/CRB + Artifactory/internal mirrors)
          → crypto / FIPS (uses tooling from the repos above)
            → packages    (update → required/optional → version-lock → tools → absent)
              → OS tuning  (sysctls: map_count, keepalive, ipv6)
                → distro specifics (users/sudo/keys, fail2ban, pip)
                  → ssh → network → firewall → bastion → chrony
                    → selinux → fapolicyd → CIS(openscap→role→custom)
                      → banner → logging → breakglass → agents → backup
```

## What it does

Core (always): hostname/timezone, disks/mounts (via the universal
[`storage`](../storage/) role), package baseline, OS-tuning sysctls, logging &
auditd tuning, chrony time sync, the SSH server engine, breakglass local-admin,
fail2ban (Ubuntu), borg backup hooks.

Opt-in (distro-agnostic — enabling a capability never surprises an existing fleet):

| Area | Toggle | Notes |
|---|---|---|
| **Outbound proxy** (Squid) | `baseline_proxy_url` | env + dnf/apt proxy; internal mirrors bypass via `no_proxy` |
| **Login banner** | `baseline_banner_enabled` | env/host/user banner + pre-auth `/etc/issue` (on by default) |
| **Bastion conduit** | `baseline_bastion_enabled` | router for SSH jump + Prometheus scrape-through, allow-listed |
| **FIPS / crypto policy** (EL) | `fips_enabled` / `baseline_crypto_policy` | `fips-mode-setup` + `update-crypto-policies`, idempotent; reboot-aware |
| **OpenSCAP remediation** (EL) | `baseline_cis_openscap_enabled` | SSG datastream + `oscap --remediate` |
| **Full package upgrade** | `baseline_packages_update` | otherwise only the declared package sets are installed |
| **Version-locked packages** | `baseline_packages_version_locked` | dnf versionlock / apt hold |
| **Troubleshooting tools** | `install_troubleshooting_tools` | lean by default; install `sos`/`strace`/… on demand |
| **NTP servers** | `baseline_ntp_servers` / `_pools` | chrony; empty = keep distro default config |
| **SSH forwarding / bastion** | `baseline_ssh_*` | dynamic sshd drop-in + optional ProxyJump |
| **Network** (IP/DNS) | `baseline_network_manage` | NetworkManager/`nmcli`; dynamic interface (`ens*/eth*/eno*`) |
| **Firewall** (zones/services) | `baseline_firewall_manage` | firewalld; custom service/zone XML templates |
| **SELinux** (EL) | `baseline_selinux_state` | set `enforcing`/`permissive`/`disabled` |
| **fapolicyd** (EL) | `baseline_fapolicyd_enabled` | template-driven config + custom rules; permissive by default |
| **CIS hardening** | `baseline_cis_upstream_enabled` / `baseline_cis_custom_enabled` | upstream benchmark role hook + custom org controls |
| **IPv6 disable** | `baseline_disable_ipv6` | sysctl, per-host |
| **Observability/security agents** | `baseline_enable_*` | node_exporter, velociraptor, splunk_forwarder, stroom_agent, clamav (each its own role) |

## Usage

```yaml
- hosts: linux_hosts
  roles:
    - baseline
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

## Package baseline

```yaml
baseline_packages_update: false              # run a full OS upgrade first
baseline_packages_required: [chrony, curl, jq]
baseline_packages_optional: [tree, htop]
baseline_packages_version_locked:            # installed, then pinned
  - docker-ce
  - containerd.io
install_troubleshooting_tools: false         # sos/strace/tcpdump/… on demand
```

Required + optional install in one transaction. Version-locked packages are
installed then pinned (dnf `versionlock` on EL, `apt-mark hold` on Debian).
`cloud-utils-growpart` is installed by the `storage` role, not here. The legacy
`dnf_packages` list is still honoured (folded into the required set).

## FIPS & crypto policy (EL)

```yaml
fips_enabled: true                  # fips-mode-setup --enable, idempotent
baseline_crypto_policy: "FUTURE"    # update-crypto-policies --set (ignored under FIPS)
baseline_allow_reboot: false        # FIPS needs a reboot; true lets the role do it
```

State is checked before acting, so re-runs are no-ops. A FIPS toggle requires a
reboot — with `baseline_allow_reboot: false` (default) the role logs that a
reboot is pending instead of rebooting the fleet from under you.

## Time sync (chrony)

```yaml
baseline_ntp_servers: ["192.0.2.1"]   # [] = keep distro default config
baseline_ntp_pools: ["pool.ntp.org"]
baseline_ntp_allow: ["192.0.2.0/24"]  # subnets this host serves time to
```

chrony is always installed and running; the managed `chrony.conf` is only
written when servers/pools are provided.

## SSH server

Dynamic `sshd_config.d` drop-in (keepalive + forwarding policy) plus an optional
`ssh_config.d` ProxyJump for bastion egress:

```yaml
baseline_ssh_allow_tcp_forwarding: true    # bastions set true
baseline_ssh_allow_agent_forwarding: false
baseline_ssh_permit_root_login: "prohibit-password"
baseline_ssh_bastion_host: "bastion.mgt.example.com"
baseline_ssh_bastion_match_hosts: "192.0.2.*"
```

## CIS hardening

CIS is layered so the vendor remediation is never patched in place — they run in
this order: **OpenSCAP → upstream role → custom controls → extra patch files.**

```yaml
# 1. OpenSCAP / SCAP Security Guide remediation ("harden via openscap"):
baseline_cis_openscap_enabled: true
baseline_cis_openscap_profile: "xccdf_org.ssgproject.content_profile_cis"
baseline_cis_openscap_remediate: true      # false = scan/report only
# datastream auto-resolves to ssg-<distro><ver>-ds.xml; override if needed.

# 2. Upstream benchmark role (install via requirements.yml — NOT vendored):
baseline_cis_upstream_enabled: true
baseline_cis_upstream_role: "ansible-role-8-cis"

# 3. This repo's custom controls (follows the legacy flag by default):
baseline_cis_hardening: true               # → baseline_cis_custom_enabled
baseline_cis_disable_usb_storage: true     # merged into the module blacklist
baseline_cis_fix_world_writable: true      # strip world-write bit (perm 0002)
baseline_cis_world_writable_excludes:      # …but exclude paths that need it
  - /var/lib/docker                        # container overlays are legitimately 0002

# 4. Extra org "custom patch" task files, applied last:
baseline_cis_custom_task_files:
  - /etc/ansible/cis-patches/site.yml
```

The custom set is a pragmatic CIS Level-1 **subset** — network sysctls,
core-dump restriction, insecure-module blacklist (+ `usb-storage`), pwquality,
password aging, and the world-writable sweep (excluding `/var/lib/docker`).
`ip_forward` is left untouched (bastions/k8s need it). FIPS is **not** here — it
lives in the crypto phase above. `setroubleshoot` is enforced **absent** via
`baseline_packages_absent` (SOE policy). All control lists are overridable.

> **`/opt` mode & containers** — `/opt` is `0755 root:root` (`baseline_opt_mode`),
> the distro default and CIS-compliant (CIS flags world-*writable* dirs, not
> world-readable ones). To run containers from `/opt`, label a dedicated subtree
> `container_file_t` via `baseline_selinux_fcontexts` rather than relabelling all
> of `/opt`:
> ```yaml
> baseline_selinux_fcontexts:
>   - {path: "/opt/containers(/.*)?", setype: container_file_t, restore: /opt/containers}
> ```

## Outbound proxy (Squid client)

Point the host's package managers + environment at a Squid proxy for general
internet egress (configured **before** repos/packages). Internal mirrors
(Artifactory) and the estate domain bypass via `no_proxy`:

```yaml
baseline_proxy_url: "http://squid.mgt.example.com:3128"
baseline_proxy_no_proxy: "localhost,127.0.0.1,::1,artifactory.mgt.example.com,.example.com"
```

The Squid **server** itself is the separate [`squid`](../squid/) role.

## Login banner

Every host gets an interactive banner (env / host / logged-in user) plus a
pre-auth `/etc/issue`:

```yaml
baseline_banner_enabled: true
baseline_banner_env_label: "{{ env | default('UNKNOWN') | upper }}"   # MGT / DEV / PROD
baseline_banner_message: "Authorised access only."
```

## Bastion conduit

A bastion bridges the monitoring network and the private environment behind it.
Operators SSH-ProxyJump in; **Prometheus scrapes backend exporters through the
bastion**, because those nodes have no direct route from the monitoring VLAN. The
bastion is turned into a least-privilege router: IP forwarding on, traffic
allow-listed to the monitoring source IPs and exporter ports only.

```yaml
baseline_bastion_enabled: true              # set on the bastion inventory group
baseline_ssh_allow_tcp_forwarding: true     # SSH jump side
baseline_bastion_ip_forward: true
baseline_bastion_monitoring_ips: ["192.0.2.74"]   # Prometheus source IP(s)
baseline_bastion_exporter_ports: ["9100/tcp"]
baseline_bastion_masquerade: true           # SNAT so backends can reply
baseline_bastion_backend_subnets: ["198.51.100.0/24 198.51.100.1"]   # optional routes
baseline_bastion_route_conn: "ens192"
```

## Observability / security agents

Each agent is its own bare-noun role; baseline just decides whether to apply it.
Configure each through its own role's vars (see `roles/<agent>/defaults`).

```yaml
baseline_enable_node_exporter: true
baseline_enable_velociraptor: false
baseline_enable_splunk_forwarder: false
baseline_enable_stroom_agent: false
baseline_enable_clamav: false
```

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

`baseline` (everything), plus per-area: `hostname`, `storage`, `ca_trust`,
`proxy`, `repos`, `crypto`/`fips`, `packages`, `tuning`/`sysctl`/`ipv6`, `ssh`,
`pip`, `network`, `firewall`, `bastion`, `chrony`/`ntp`, `selinux`, `fapolicyd`,
`cis`/`hardening`/`openscap`, `banner`, `logging`, `breakglass`, `agents`
(+ `node_exporter`, `velociraptor`, `splunk_forwarder`, `stroom_agent`,
`clamav`), `backup`. `reboot` is `never`-tagged (explicit opt-in).

See `defaults/main.yml` for the full variable surface.
