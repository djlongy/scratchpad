# baseline

## TL;DR

Distro-agnostic SOE Linux baseline (AlmaLinux / RHEL / Ubuntu). Establishes a
consistent, hardened foundation before app roles run. Everything beyond the core OS
setup is opt-in via override host/group vars, so enabling a capability never surprises
an existing fleet.

```bash
ansible-playbook -i inventories/<env>/hosts.yml playbooks/vault_solo_e2e.yml --tags baseline
```

## Requirements

Install collections before running (repo `requirements.yml`, or ad-hoc):

    ansible-galaxy collection install -r requirements.yml

| Collection | When | Used for |
|---|---|---|
| `ansible.posix` | always | OS-tuning sysctls; opt-in firewalld/SELinux phases |
| `community.general` | always | timezone; opt-in RHSM registration/network/proxy/CIS/Ubuntu firewall phases |
| `community.hashi_vault` | When no declared breakglass password is set | Vault fallback lookup for the breakglass password |

## Key variables

Full list: `defaults/main.yml`. Contract: `meta/argument_specs.yml`.

**Required** = value must be correct for a successful run (defaults often work).
**Optional** = safe to leave default / empty; phase stays off or uses built-ins.
**When X** = required only if that feature is on.

| Req | Variable | Default | Purpose |
|---|---|---|---|
| Optional | `baseline_timezone` | `UTC` | System timezone |
| Optional | `baseline_storage_volumes` | `[]` | Disk/mount definitions passed to the `storage` role |
| Optional | `baseline_allow_reboot` | `false` | Apply FIPS/SELinux reboot-requiring changes in one pass and reboot once |
| Optional | `baseline_packages_required` / `_optional` | `[]` | Packages installed every run |
| Optional | `fips_enabled` | `false` | Enable FIPS mode (EL); needs a reboot |
| When network management is wanted | `baseline_network_manage` | `false` | Enable NetworkManager-driven IP/DNS config |
| When firewall management is wanted | `baseline_firewall_manage` | `false` | Enable firewalld zone/service config |
| When a SELinux state change is wanted | `baseline_selinux_state` | `""` | `enforcing`/`permissive`/`disabled` (`""` = no change) |
| When a breakglass account is wanted | `baseline_breakglass_password` / `_vault_secret` | `""` | Emergency local-admin password (declared-var-first, HashiCorp Vault fallback); skipped when both are empty |
| Optional | `baseline_bastion_enabled` | `false` | Turn this host into an SSH + Prometheus bastion router |
| Optional | `baseline_enable_node_exporter` (+ `_velociraptor`/`_splunk_forwarder`/`_stroom_agent`/`_clamav`) | `false` | Toggle each observability/security agent role |
| Optional | `baseline_cis_hardening` / `_cis_openscap_enabled` / `_cis_upstream_enabled` | `false` | Layered CIS hardening (custom / OpenSCAP / upstream benchmark role) |
| Optional | `baseline_proxy_url` | `""` | Outbound Squid proxy URL for package managers |

## Usage

```yaml
- hosts: linux_hosts
  roles:
    - role: baseline
      tags: [baseline]
```

Run it:

```bash
ansible-playbook -i inventories/<env>/hosts.yml playbooks/vault_solo_e2e.yml --tags baseline
```

Enable observability/security agents — each is its own bare-noun role; baseline just
decides whether to apply it (configure each through its own role's vars under
`roles/<agent>/defaults`):

```yaml
baseline_enable_node_exporter: true
baseline_enable_velociraptor: false
baseline_enable_splunk_forwarder: false
baseline_enable_stroom_agent: false
baseline_enable_clamav: false
```

## Preconditions

- Vault breakglass fallback: the secret must already exist at
  `baseline_breakglass_vault_secret`'s path before the run — the role reads it, it does
  not create it.
- `baseline_cis_upstream_enabled: true` requires the upstream CIS role (e.g.
  `ansible-role-8-cis`) already present via `requirements.yml` — it is not vendored in
  this role.

## Behaviour

Phases run in a deliberate dependency order, each idempotent and individually
taggable. A no-tags run runs everything; tags only narrow the run.

```
identity (hostname/tz)
  -> storage (disks/mounts via the `storage` role)
    -> CA trust -> proxy -> repos -> crypto/FIPS -> packages
      -> OS tuning (sysctls) -> distro specifics (users/sudo/keys, fail2ban, pip)
        -> ssh -> network -> firewall -> bastion -> chrony
          -> selinux -> fapolicyd -> CIS (openscap -> role -> custom)
            -> banner -> logging -> breakglass -> agents -> backup
```

Core (always applied): hostname/timezone, disks/mounts (via the `storage` role),
package baseline, OS-tuning sysctls, logging & auditd tuning, chrony time sync, the SSH
server engine, breakglass local-admin (only when a password/secret is configured),
fail2ban (Ubuntu), borg backup hooks. Everything else (proxy, banner, bastion,
FIPS/crypto, OpenSCAP, network, firewall, SELinux, fapolicyd, CIS hardening, IPv6
disable, observability/security agents) is opt-in via its own toggle var.

CIS hardening is layered so vendor remediation is never patched in place: **OpenSCAP ->
upstream role -> custom controls -> extra patch files** (`baseline_cis_openscap_enabled`
-> `baseline_cis_upstream_enabled` -> `baseline_cis_hardening` ->
`baseline_cis_custom_task_files`). The custom set is a pragmatic CIS Level-1 subset —
network sysctls, core-dump restriction, insecure-module blacklist, pwquality, password
aging, and a world-writable sweep (`baseline_cis_world_writable_excludes` keeps
container overlay paths out of it). `ip_forward` is left untouched (bastions/k8s need
it) — FIPS lives in the crypto phase, not CIS.

fapolicyd (EL) is template-driven (`fapolicyd.conf` + custom `rules.d`) and defaults
`baseline_fapolicyd_permissive: true` — validate in permissive before flipping it to
`false` and enforcing.

## Tag safety

`reboot` is `never`-tagged — it never fires on a normal run and must be requested
explicitly with `--tags reboot`.
