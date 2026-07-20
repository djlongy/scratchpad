# firewalld

Manages **firewalld** on EL- and Debian-family hosts via:

1. **Custom service XML** templates dropped under `/etc/firewalld/services/`
2. **Custom zone XML** templates dropped under `/etc/firewalld/zones/`
3. **Runtime bindings** of source CIDRs and interfaces to zones (L3 segregation
   preferred over L2 per-NIC patterns)

Env-agnostic: every value lives in `inventories/<env>/group_vars/` or `host_vars/`.
The role's `defaults/main.yml` ships empty lists for all configurable surfaces.

## TL;DR

**Most common: apply updated firewall rules.** Edit `firewalld_services` / `firewalld_zones` / `firewalld_source_zone_bindings` in group_vars, then re-run — a no-tag run re-renders the XML and applies the bindings.

```bash
ansible-playbook -i inventories/<env>/hosts.yml playbooks/firewalld.yml [--tags services,bindings]
```

## Why XML templates instead of per-port firewalld module calls?

Defining a service once as `/etc/firewalld/services/harbor.xml` and then
referencing `harbor` in a zone is firewalld's native, declarative model. It:

- Survives `firewall-cmd --reload` and reboots without re-running Ansible
- Makes `firewall-cmd --list-all` output meaningful (services have names, not
  just port numbers)
- Decouples *what a service is* (port set, helpers, destination) from *which
  zone it lives in* — reuse across multiple zones
- Avoids the per-port loop pattern that's hard to audit and slow on big lists

## Quickstart

```yaml
# inventories/mgt/group_vars/docker_hosts.yml
firewalld_default_zone: trusted-mgmt

firewalld_services:
  - name: harbor
    short: Harbor Registry
    description: Container registry web UI + registry API
    ports:
      - { port: 80,  protocol: tcp }
      - { port: 443, protocol: tcp }
      - { port: 4443, protocol: tcp }

  - name: docker-swarm-control
    short: Docker Swarm control plane
    ports:
      - { port: 2377, protocol: tcp }      # Raft
      - { port: 7946, protocol: tcp }      # gossip
      - { port: 7946, protocol: udp }
      - { port: 4789, protocol: udp }      # VXLAN overlay

firewalld_zones:
  - name: trusted-mgmt
    short: Management
    description: Internal management network — admin services
    target: default
    services: [ssh, harbor, docker-swarm-control]

  - name: dmz-ingress
    short: DMZ Ingress
    description: Public-facing services
    target: default
    services: [http, https]

firewalld_source_zone_bindings:
  - { zone: trusted-mgmt, source: 10.0.10.0/24 }
  - { zone: dmz-ingress,  source: 0.0.0.0/0 }
```

Then in a playbook:

```yaml
- hosts: docker_hosts
  become: true
  roles:
    - firewalld
```

## Tags

| Tag | Phase |
|---|---|
| `firewalld` | All firewalld tasks |
| `install` | Package install + service start/enable |
| `services` | Render service XML files |
| `zones` | Render zone XML files |
| `cleanup` | Remove service/zone XML files |
| `default_zone` | Set the default zone |
| `bindings` | Apply source-CIDR and interface bindings |
| `legacy_rules` | Apply legacy `firewall_rules` list (back-compat) |
| `reload` | The flush_handlers checkpoint |

Run only the binding phase:

```bash
ansible-playbook -i inventories/mgt/hosts.yml playbooks/30_plat_baseline.yml \
  --tags bindings
```

## Variables

Full schema in `defaults/main.yml`. Key options:

| Variable | Default | Purpose |
|---|---|---|
| `firewalld_enabled` | `true` | Master toggle |
| `firewalld_default_zone` | `""` | Default zone (empty = unchanged) |
| `firewalld_services` | `[]` | Custom service XML definitions |
| `firewalld_zones` | `[]` | Custom zone XML definitions |
| `firewalld_source_zone_bindings` | `[]` | `[{zone, source}]` — preferred L3 pattern |
| `firewalld_interface_zone_bindings` | `[]` | `[{zone, interface}]` — L2, use sparingly |
| `firewalld_services_remove` | `[]` | Service short names to delete |
| `firewalld_zones_remove` | `[]` | Zone short names to delete |
| `firewalld_reload` | `true` | Reload after XML changes |
| `firewall_rules` | `[]` | **Legacy** — accepts `"22/tcp/ssh"` or `{port,protocol,service}` |

## Back-compat with `firewall_rules` (role only — inventory migrated)

Inventory and service roles use `firewalld_services` / `firewalld_zones` /
`firewalld_source_zone_bindings`. The legacy `firewall_rules` list remains
supported **inside this role only** for transitional callers; do not reintroduce
it in group_vars or host_vars.

Legacy formats still accepted by the role (when `firewall_rules` is non-empty):

```yaml
# String form
firewall_rules:
  - "22/tcp/ssh"
  - "5000-5100/tcp/elasticsearch"

# Dict form
firewall_rules:
  - { port: 22, protocol: tcp, service: ssh }
```

Entries open in `firewalld_default_zone` (or `public` if unset).

## Notes on the L2 vs L3 segregation debate

`firewalld_interface_zone_bindings` exists so you *can* bind a vNIC to a zone,
but **prefer `firewalld_source_zone_bindings`** in nearly all cases. On a
virtualised host, the extra vNICs in an L2-split design don't deliver real
bandwidth isolation (they share the vSwitch uplink), don't create a real
security boundary (the VM kernel routes between them), and triple your IP and
DNS surface. The defensible cases for a dedicated NIC are storage with custom
MTU/multipathing, or PCI passthrough for performance.
