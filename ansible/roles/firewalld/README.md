# firewalld

## TL;DR

Configures **firewalld** on EL- and Debian-family hosts: renders custom
service/zone XML under `/etc/firewalld/{services,zones}/`, then applies
runtime bindings of source CIDRs (preferred) or interfaces to zones.
Env-agnostic — every configurable value defaults empty and is set from
`inventories/<env>/group_vars/` or `host_vars/`.

```bash
ansible-playbook -i inventories/<env>/hosts.yml <playbook>.yml --tags firewalld
```

## Requirements

Install collections before running (repo `requirements.yml`, or ad-hoc):

    ansible-galaxy collection install -r requirements.yml

| Collection | When | Used for |
|---|---|---|
| `ansible.posix` | always | `firewalld` module (services, zones, bindings) |

## Key variables

Full list: `defaults/main.yml`. Contract: `meta/argument_specs.yml`.

**Required** = value must be correct for a successful run (defaults often work).
**Optional** = safe to leave default / empty; phase stays off or uses built-ins.

Nothing is required — every list defaults empty and every phase is skipped
until the corresponding variable is populated.

| Req | Variable | Default | Purpose |
|---|---|---|---|
| Optional | `firewalld_enabled` | `true` | Master toggle; `false` skips the role |
| Optional | `firewalld_default_zone` | `""` | Default zone for unclassified traffic; empty = unchanged |
| Optional | `firewalld_services` | `[]` | Custom service XML definitions (name/ports/protocols/…) |
| Optional | `firewalld_zones` | `[]` | Custom zone XML definitions (name/target/services/…) |
| Optional | `firewalld_source_zone_bindings` | `[]` | `[{zone, source}]` — preferred L3 CIDR-to-zone pattern |
| Optional | `firewalld_interface_zone_bindings` | `[]` | `[{zone, interface}]` — L2 pattern, use sparingly (shares the vSwitch uplink, no real kernel-level boundary) |
| Optional | `firewalld_services_remove` | `[]` | Service short names to delete |
| Optional | `firewalld_zones_remove` | `[]` | Zone short names to delete |
| Optional | `firewalld_reload` | `true` | Reload firewalld after XML changes |
| Optional | `firewalld_packages` | `[firewalld]` | Package(s) to install |
| Optional | `firewall_rules` | `[]` | Legacy `"22/tcp/ssh"` or `{port,protocol,service}` list; opens ports directly in `firewalld_default_zone` |

## Usage

```yaml
- hosts: docker_hosts
  roles:
    - role: firewalld
      tags: [firewalld]
```

Run it:

```bash
ansible-playbook -i inventories/<env>/hosts.yml <playbook>.yml --tags firewalld
```

## Tag safety

`reload` is a bare `meta: flush_handlers` checkpoint — it only reloads
firewalld if an earlier task in the *same* invocation (`services`, `zones`,
`cleanup`) notified the handler. Run alone (`--tags reload`), it is a no-op;
it does not force a live reload of already-applied config.
