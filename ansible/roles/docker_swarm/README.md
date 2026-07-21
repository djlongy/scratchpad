# docker_swarm

## TL;DR

Bootstraps a Docker Swarm cluster: installs Docker CE, opens firewalld for overlay
traffic, pre-creates `docker_gwbridge` with a custom MTU/subnet, initialises the
cluster on an auto-picked bootstrap manager, recreates the ingress overlay encrypted,
and joins additional managers + workers via tokens. Scope is intentionally narrow —
services, NFS mounts, and stack-staging directories belong to per-app roles.

```bash
ansible-playbook -i inventories/<env>/hosts.yml playbooks/<playbook>.yml
```

## Requirements

Install collections before running (repo `requirements.yml`, or ad-hoc):

    ansible-galaxy collection install -r requirements.yml

| Collection | When | Used for |
|---|---|---|
| `ansible.posix` | always | firewalld rules (control/data plane), SELinux booleans, sysctls |
| `community.general` | always | Kernel module loading (`modprobe`) for overlay networking |

## Key variables

Full list: `defaults/main.yml`. Contract: `meta/argument_specs.yml`.

**Required** = value must be correct for a successful run (defaults often work).
**Optional** = safe to leave default / empty; phase stays off or uses built-ins.
**When X** = required only if that feature is on.

| Req | Variable | Default | Purpose |
|---|---|---|---|
| Optional | `docker_swarm_enabled` | `true` | Master switch — skip the role entirely |
| Optional | `docker_swarm_managers_group` / `_workers_group` | `swarm_managers` / `swarm_workers` | Inventory group names |
| Optional | `docker_swarm_bootstrap_host` | `""` | Override the auto-picked bootstrap manager |
| Optional | `docker_swarm_install_docker_enabled` | `true` | Set `false` when Docker is managed by another role/golden image |
| Optional | `docker_swarm_use_data_disk` | `false` | Relocate Docker's data-root onto a data-disk mount |
| Optional | `docker_swarm_advertise_addr` | `{{ ansible_host }}` | Control-plane address peers use to reach this node |
| When multi-NIC | `docker_swarm_data_path_addr` | `""` | Overlay data-path (east-west) bind address; fixed at join time |
| Optional | `docker_swarm_ingress_encrypted` | `true` | Recreate the default ingress overlay with IPSec ESP encryption |
| Optional | `docker_swarm_default_addr_pool` | `10.40.0.0/16` | Address pool for swarm-scoped overlays (`swarm init`) |
| Optional | `docker_swarm_gwbridge_subnet` / `_gateway` / `_mtu` | `10.42.0.0/24` / `10.42.0.1` / `1500` | Pre-created `docker_gwbridge` tuning |
| Optional | `docker_swarm_overlay_networks` | `[]` | Shared/estate-wide overlays created on bootstrap |
| When destroying | `docker_swarm_destroy_confirm` | `false` | Must be `true` via `-e` (with `--tags destroy`) to actually tear the cluster down |
| Optional | `docker_swarm_autolock` | `false` | Encrypt Raft logs at rest — capture the unlock key before enabling |
| Optional | `docker_swarm_manage_docker_group` | `false` | Align the local `docker` group GID to a directory group for SSSD-based socket access |

## Usage

```yaml
- name: Provision Docker Swarm cluster
  hosts: swarm_cluster        # = swarm_managers + swarm_workers
  become: true
  roles:
    - role: docker_swarm
```

Run it (no play-level tag — see [Tag safety](#tag-safety)):

```bash
# First-time bootstrap, or any re-converge — idempotent, no tags
ansible-playbook -i inventories/<env>/hosts.yml playbooks/<playbook>.yml

# Rolling restart of the LIVE cluster — standalone, never chained
ansible-playbook -i inventories/<env>/hosts.yml playbooks/<playbook>.yml --tags redeploy --limit swarm_workers
```

## Preconditions

Docker CE install is RHEL-family only (`dnf` + `yum_repository`). For Debian/Ubuntu,
set `docker_swarm_install_docker_enabled: false` and install Docker via a
distro-appropriate role first — this role does not do that install for you.

## Behaviour

**Bootstrap host pick** — there is no `swarm_bootstrap` inventory group; the role
discovers it at run time: use `docker_swarm_bootstrap_host` if set, otherwise probe
every manager via `docker info` and reuse one that already has an active swarm,
otherwise fall back to the first host in `swarm_managers`. List your preferred
bootstrap manager first in inventory and the override is never needed.

**Data-root relocation** — workers with a heavy write path (Splunk, Elasticsearch,
Postgres) benefit from putting Docker's data-root on a separate disk. Named volumes,
image layers, and swarm state all land under `<data-root>`. Setting
`docker_swarm_use_data_disk: true` renders `daemon.json` with the chosen
`docker_swarm_data_root` (default `/opt/docker`).

**Multi-NIC** — single-NIC is the default. For a segmented layout, set per-host in
`host_vars`:

```yaml
docker_swarm_advertise_addr: 10.10.0.31        # management vNIC (control plane)
docker_swarm_data_path_addr: 10.30.0.31        # data vNIC (overlay east-west)
docker_swarm_control_plane_zone: management
docker_swarm_data_plane_zone: data
```

Published ports are **not** bound to one NIC — the routing mesh answers on every
interface; restrict access with firewalld in the access zone. `data_path_addr` is
fixed at join time — changing it needs `leave --force` + rejoin.

**Firewall** — ports are grouped by traffic plane so multi-NIC hosts can land each
plane in the right zone; single-NIC hosts inherit `docker_swarm_firewall_zone` for
both:

```yaml
docker_swarm_control_plane_ports: [2377/tcp, 7946/tcp, 7946/udp]   # → advertise_addr NIC
docker_swarm_data_plane_ports: [4789/udp, 4500/udp]                # → data_path_addr NIC
docker_swarm_data_plane_protocols: [esp]                           # IPSec ESP (encrypted overlays)
```

**Other lifecycle notes** — `live-restore` is incompatible with Swarm mode; the role
intentionally omits it from `daemon.json`. `docker_gwbridge` can only be customised
before a host joins the swarm — it is immutable after that.

## Out of scope

- Provisioning the data-disk for `docker_swarm_use_data_disk` — an OS-baseline role
  does that.
- Binding interfaces to firewalld zones — the baseline/network role owns
  interface→zone assignment.
- Opening published "access" ports — that belongs to per-app roles.
- Source-based policy routing for asymmetric routing on a 3-NIC host — a
  baseline/network-role concern.

## Tag safety

- **Never put a play-level tag on the play that invokes this role.** A play-level tag
  propagates onto every role task and overrides `never`, which would let a plain
  `--tags swarm` re-trigger `redeploy`/`reconcile_nodes`/`destroy`.
- `redeploy` (rolling, node-by-node restart of the live cluster: drain → restart/reboot
  → active) is standalone — run it alone with `--limit`, never chained with other tags
  or other roles in the same play.
- `destroy` requires **both** `--tags destroy` and `-e docker_swarm_destroy_confirm=true`
  — either alone is a no-op. It does not uninstall Docker, remove firewall rules, wipe
  the data-root, or remove `docker_gwbridge` — a follow-up run without `--tags destroy`
  re-installs the swarm on top of the same data-root, and named volumes survive.
- `reconcile_nodes` (`never`-gated) removes `Down` nodes from the swarm — opt-in only,
  request it explicitly with `--tags reconcile_nodes`.
