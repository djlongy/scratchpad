# docker_swarm

Bootstrap a **Docker Swarm cluster** — install Docker CE, open firewalld
for overlay traffic, pre-create `docker_gwbridge` with custom MTU + subnet,
initialise the cluster on an auto-picked bootstrap manager, recreate the
ingress overlay with encryption enabled, and join additional managers +
workers via tokens.

Scope is intentionally narrow: this role gets you a working swarm and
nothing more. Services, NFS mounts, and stack-staging directories belong
to per-app roles or the calling playbook.

Fully portable — every input lives in inventory. No environment label,
IP, domain, or hostname is hardcoded.

## TL;DR

**Most common: rolling-restart the live cluster.** `redeploy` is `never`-gated — it drains and restarts nodes one by one; pair with `--limit` so you don't bounce all the managers at once.

```bash
ansible-playbook -i inventories/<env>/hosts.yml playbooks/docker_swarm.yml --tags redeploy --limit swarm_workers
```

First-time bootstrap (and any idempotent re-converge) runs with no tags:

```bash
ansible-playbook -i inventories/<env>/hosts.yml playbooks/docker_swarm.yml
```

## Inventory groups

Exactly two groups are recognised:

| Group | Default name (overridable) | Hosts |
|---|---|---|
| Managers | `swarm_managers` | Manager nodes (Raft quorum) |
| Workers | `swarm_workers` | Worker nodes |

Override the group names if your inventory uses different ones:

```yaml
docker_swarm_managers_group: my_swarm_managers
docker_swarm_workers_group: my_swarm_workers
```

### Bootstrap host pick

There is **no** `swarm_bootstrap` group. The role discovers the bootstrap
host at run time:

1. If `docker_swarm_bootstrap_host` is set, that host is used.
2. Otherwise the role probes every manager via `docker info`. If any one
   already has an active swarm, it is reused (re-running against an
   existing cluster is idempotent — no node ever rejoins itself).
3. Otherwise the first host in `swarm_managers` is used.

In practice: list your preferred bootstrap manager first in the inventory
and you never need to touch the override.

## Tagging model — read this first

This role follows the **refinement** model, not enablement:

- **A no-tag run does the full, idempotent deploy.** "Deploy" is just
  running the role. There is no `deploy` or `bootstrap` tag you must
  remember, and **no tag is ever a prerequisite for another tag** (no
  `bootstrap`+`deploy` chaining).
- **Tags only narrow scope** for fast iteration, or opt into an action
  that should never run on a routine converge.
- **Opt-in actions are `never`-gated** — `redeploy`, `reconcile_nodes`,
  `destroy`. Skipped on a normal run, runnable standalone, never chained.

> **Do not add a play-level tag to the play that invokes this role.** A
> play-level tag propagates onto every role task and overrides `never`,
> which would let a plain `--tags swarm` re-trigger redeploy/reconcile/
> destroy. Use the role's own tags for partial runs (see the example play below).

### Converge phases (no tags runs all of these, in order)

| Tag | Phase | Notes |
|---|---|---|
| `prepare` | Kernel modules + sysctl + SELinux booleans | RHEL host prep for overlay networking |
| `install` | Docker CE + Python SDK (dnf **or** pip) + `daemon.json` | Also the umbrella tag — runs **every** converge phase below it |
| `access` | Align local `docker` group GID / add members (FreeIPA) | Opt-in via `docker_swarm_manage_docker_group` |
| `firewall` | Open Swarm ports + ESP protocol in the configured zone | |
| `gwbridge` | Pre-create `docker_gwbridge` with custom MTU + subnet | Only on hosts not yet in a swarm |
| `swarm` | Probe → init bootstrap → recreate encrypted ingress → join | Ingress recreation on bootstrap only, between init and joins |
| `overlays` | Create shared app overlays from `docker_swarm_overlay_networks` | On bootstrap; skips overlays that already exist |

Every converge phase also carries the `install` tag, so `--tags install`
runs the **whole cluster bring-up**; the per-phase tags narrow it further.

### Opt-in actions (`never`-gated — only run when explicitly tagged)

| Tag | Action | Gate |
|---|---|---|
| `redeploy` | Rolling, node-by-node restart of the **live** cluster (drain → restart Docker/reboot → active) | `--tags redeploy` (standalone) |
| `reconcile_nodes` | Remove `Down` nodes from the swarm | `--tags reconcile_nodes` |
| `destroy` | Tear down (workers + non-bootstrap managers leave first; bootstrap last) | `--tags destroy` **AND** `-e docker_swarm_destroy_confirm=true` |

Example playbook (`playbooks/docker_swarm.yml`) — note the **absence** of a
play-level tag:

```yaml
- name: Provision Docker Swarm cluster
  hosts: swarm_cluster        # = swarm_managers + swarm_workers
  become: true
  roles:
    - role: docker_swarm
```

```bash
# First-time bootstrap (or any re-converge — idempotent, no tags)
ansible-playbook -i inventories/hosts.yml playbooks/docker_swarm.yml

# Whole cluster bring-up only (skip the nfs/lb/baseline plays)
ansible-playbook ... --tags install

# Just open firewall ports
ansible-playbook ... --tags firewall

# Re-render daemon.json on workers only
ansible-playbook ... --tags install --limit swarm_workers

# Rolling restart of the live cluster — STANDALONE, no chaining. Pair with
# serial:1 in the play / --limit workers to avoid bouncing managers.
ansible-playbook ... --tags redeploy --limit swarm_workers

# Remove dead nodes
ansible-playbook ... --tags reconcile_nodes

# Tear down (Docker stays installed; data-root untouched). Both required.
ansible-playbook ... --tags destroy -e docker_swarm_destroy_confirm=true
```

## Block storage / data-root relocation

Workers running Splunk, Elasticsearch, Postgres, or anything else with a
heavy write path benefit from putting **Docker's data-root** on a separate
data disk:

- Named volumes (`docker volume create foo`, or `volumes: [foo:/data]` in
  a stack file) land at `<data-root>/volumes/foo/_data`.
- Image layers and container writable layers live under `<data-root>/overlay2/`.
- Swarm state, build cache, and network state also live under `<data-root>`.

This role does **not** provision the data disk itself — your OS-baseline
role should do that (LVM → xfs → `/opt`). What this role does is, on hosts
that flip `docker_swarm_use_data_disk=true`, render `daemon.json` with
`"data-root": "/opt/docker"` so every named volume + image layer lands on
that mount automatically.

Bind mounts that already target `/opt/<service>/data` from per-app stack
files follow the same convention with no further work.

Example pattern: enable on workers, leave off on managers and single-disk
lab hosts:

```yaml
# inventories/<env>/group_vars/swarm_workers.yml
docker_swarm_use_data_disk: true
docker_swarm_data_root: "/opt/docker"
```

## Network tuning

### Encrypted ingress overlay

Docker creates the default `ingress` overlay **unencrypted**. With
`docker_swarm_ingress_encrypted: true` (the default) the role recreates
it with `--opt encrypted` so published-port traffic between nodes is
wrapped in IPSec ESP. Recreation runs on the bootstrap manager between
`swarm init` and the first join — the freshest possible moment, with no
service tasks to disrupt.

```yaml
docker_swarm_ingress_subnet: "10.41.0.0/24"   # pick anything that doesn't collide
docker_swarm_ingress_gateway: "10.41.0.1"
docker_swarm_ingress_encrypted: true
```

### docker_gwbridge

`docker_gwbridge` is a per-node L2 bridge that gives swarm tasks egress
to the host network (and external networks). Docker auto-creates it with
defaults the first time a swarm overlay is needed; pre-creating it
(before the node joins a swarm) lets you set:

- **MTU** — drop to 1450/1400 when the underlay is < 1500 (nested
  VXLAN, VPN, vDS with VXLAN encap).
- **Subnet** — avoid collisions with corporate / existing VLAN ranges.

```yaml
docker_swarm_gwbridge_subnet: "10.42.0.0/24"
docker_swarm_gwbridge_gateway: "10.42.0.1"
docker_swarm_gwbridge_mtu: 1500
docker_swarm_gwbridge_enable_icc: false           # inter-container comms on bridge
docker_swarm_gwbridge_enable_ip_masquerade: true  # SNAT outbound
```

Pre-creation only fires on hosts not yet in a swarm and only if the
bridge does not already exist (replacing it on a live node requires
leaving the swarm + stopping Docker).

> **Why is `docker_gwbridge` `attachable: false`?** `attachable` is a
> property of swarm-scoped **overlay** networks — it lets standalone
> `docker run` containers join an overlay, not just swarm services.
> `docker_gwbridge` is a per-node **local bridge**, not an overlay, so
> `attachable` does not apply and always reports false in
> `docker network inspect`. There is nothing to set there; the `attachable`
> knob lives on the overlay networks (below).

### Application overlay networks

Create shared / estate-wide overlays at bootstrap (the "swarm init, then
create the encrypted app overlay" pattern). Empty by default — per-app
roles create their own. Each entry takes `name` (required) plus optional
`subnet`, `gateway`, `mtu`, `encrypted`, `attachable`:

```yaml
docker_swarm_overlay_networks:
  - name: app_net
    subnet: "10.43.0.0/24"
    encrypted: true
    attachable: true
# Defaults applied to omitted keys:
docker_swarm_overlay_default_mtu: 1450
docker_swarm_overlay_default_encrypted: true
docker_swarm_overlay_default_attachable: true
```

Created on the bootstrap manager; an overlay that already exists is left
untouched (never recreated). Runs in the `swarm`/`overlays` phase, or
standalone with `--tags overlays`.

## Host preparation

The `prepare` phase loads kernel modules, applies sysctls, and enables
SELinux booleans needed for overlay networking. All three are loop inputs:

```yaml
docker_swarm_kernel_modules: [overlay, br_netfilter]
docker_swarm_sysctl:
  net.ipv4.ip_forward: "1"
  net.bridge.bridge-nf-call-iptables: "1"
  net.bridge.bridge-nf-call-ip6tables: "1"
docker_swarm_selinux_booleans: []   # e.g. [httpd_can_network_connect]
```

The `bridge-nf-call-*` sysctls only exist once `br_netfilter` is loaded —
keep the module if you keep those sysctls.

## Docker Python SDK install

`community.docker.*` needs the Docker SDK. Two methods:

```yaml
docker_swarm_sdk_install_method: dnf      # dnf (distro pkgs) | pip
# pip method — version floor + optional internal index (Artifactory):
docker_swarm_sdk_pip_packages: ["docker>=5.0.3", "jsondiff", "requests"]
docker_swarm_pip_conf_enabled: true
docker_swarm_pip_index_url: "https://registry.example.com/api/pypi/pypi/simple"
docker_swarm_pip_trusted_hosts: ["registry.example.com"]
```

Use `pip` when the distro `python3-docker` lags what the collection needs
(community.docker 4.x wants `docker>=5`).

## Docker socket access (FreeIPA / central directory)

Align the local `docker` group's GID to a directory group's GID so
SSSD-resolved members get socket access without a local account:

```yaml
docker_swarm_manage_docker_group: true
docker_swarm_docker_group_gid: "1500001234"   # the FreeIPA docker group GID
docker_swarm_docker_group_members: []          # extra LOCAL users (optional)
```

## Centralised logging

Point the whole swarm at a log collector via the daemon log driver +
free-form `log-opts` (the `max-size`/`max-file` keys are emitted only for
the `json-file`/`local` drivers):

```yaml
docker_swarm_log_driver: syslog          # or gelf / fluentd / loki / splunk
docker_swarm_log_opts:
  syslog-address: "udp://logs.corp:514"
  tag: "{{ '{{.Name}}' }}"
```

## Firewall

Ports are grouped by **traffic plane** so multi-NIC hosts can land each plane
in the right zone. Single-NIC: both plane zones inherit
`docker_swarm_firewall_zone`, so everything lands in one zone.

```yaml
docker_swarm_firewall_zone: "public"               # single-NIC fallback zone
docker_swarm_control_plane_zone: "{{ docker_swarm_firewall_zone }}"
docker_swarm_data_plane_zone: "{{ docker_swarm_firewall_zone }}"

docker_swarm_control_plane_ports:                  # → advertise_addr NIC
  - 2377/tcp     # cluster management (manager API)
  - 7946/tcp     # gossip
  - 7946/udp     # gossip
docker_swarm_data_plane_ports:                     # → data_path_addr NIC
  - 4789/udp     # overlay VXLAN data plane
  - 4500/udp     # IPSec NAT-T (encrypted overlays)
docker_swarm_data_plane_protocols:
  - esp          # IPSec ESP (encrypted overlay data plane)
```

The role only **adds rules to named zones** — it does not bind interfaces to
zones (the baseline/network role owns that). Published "access" ports are
opened by the per-app roles in the access zone; the cluster role manages only
the control + data planes.

## Multi-NIC (management / access / data vNICs)

Single-NIC is the default. For a segmented layout — e.g. a management vNIC
(`ansible_host`), an access vNIC (client ingress), and a data vNIC (NFS +
overlay east-west) — Swarm's control/data split maps like this:

| vNIC | Carries | Knob | firewalld zone |
|---|---|---|---|
| management | SSH + control plane (2377, 7946) | `docker_swarm_advertise_addr` | `docker_swarm_control_plane_zone` |
| access | client-facing **published ports** (north-south) | *(firewall only — see note)* | per-app roles, access zone |
| data | overlay east-west (4789 + ESP) **+ NFS** | `docker_swarm_data_path_addr` | `docker_swarm_data_plane_zone` |

Per-host config (host_vars), e.g. `swarm-wkr-01`:

```yaml
docker_swarm_advertise_addr: 10.10.0.31        # management vNIC
docker_swarm_data_path_addr: 10.30.0.31        # data vNIC (overlay east-west)
docker_swarm_control_plane_zone: management
docker_swarm_data_plane_zone: data
```

**Things to know:**

- **Published ports do not bind to one NIC.** The ingress routing mesh
  answers on the published port on *all* interfaces (0.0.0.0). "Access NIC
  only" is enforced by **firewalld** (open the port in the access zone only)
  or by `mode: host` publishing — not by a Docker bind.
- **NFS is not a Docker concern.** The `nfs` volume driver targets the NFS
  server IP; the host route to that subnet decides it exits the data NIC.
- **`data_path_addr` is fixed at join time** — changing it later needs a
  `leave --force` + rejoin. Get the NIC→plane mapping right up front.
- **Asymmetric routing is the real risk.** A 3-NIC VM has one default
  gateway; return traffic on the access/data NICs needs **source-based
  policy routing** (`ip rule` + per-NIC routing tables) or stateful firewalls
  drop the mismatched flows. That is a baseline/network-role responsibility,
  not this role's.
- **MTU**: keep the overlay MTU consistent with the data NIC (encrypted
  overlay costs ~58 bytes); see `docker_swarm_ingress_mtu` /
  `docker_swarm_overlay_default_mtu`.

## Toggles

| Toggle | Default | When to flip |
|---|---|---|
| `docker_swarm_enabled` | `true` | Master kill switch |
| `docker_swarm_install_docker_enabled` | `true` | Set false when Docker is managed by another role or a golden image |
| `docker_swarm_manage_firewall` | `true` | Set false if firewalld is managed by another role |
| `docker_swarm_manage_gwbridge` | `true` | Set false to accept Docker's auto-created bridge |
| `docker_swarm_manage_ingress` | `true` | Set false to accept the default unencrypted ingress |
| `docker_swarm_use_data_disk` | `false` | Set true on hosts with `/opt` on a data disk |
| `docker_swarm_autolock` | `false` | Encrypts Raft logs at rest — capture unlock key before enabling |

## daemon.json customisation

First-class keys (each is omitted from the file when its input is empty):

```yaml
docker_swarm_log_driver: "json-file"
docker_swarm_log_max_size: "10m"
docker_swarm_log_max_file: 5
docker_swarm_log_opts: {}                 # free-form (central logging — see above)
docker_swarm_dns: []                      # upstream container DNS, e.g. ["10.0.0.1"]
docker_swarm_dns_search: []
docker_swarm_insecure_registries: []      # e.g. ["registry.example.com:8081"]
docker_swarm_registry_mirrors: []
docker_swarm_default_address_pools: []    # LOCAL bridge IPAM (see note below)
docker_swarm_metrics_enabled: false
docker_swarm_metrics_addr: "0.0.0.0:9323"
docker_swarm_experimental: false
```

> **Two address pools, different scopes.**
> `docker_swarm_default_address_pools` (daemon.json) governs **local bridge**
> networks the daemon creates. `docker_swarm_default_addr_pool` /
> `..._subnet_size` (passed to `swarm init`) governs **swarm-scoped
> overlays**. A fleet that carves a `/16` into `/24`s usually sets both.

For anything not exposed as a first-class key, merge it via
`docker_swarm_daemon_extra` (highest precedence — wins over every var
above):

```yaml
docker_swarm_daemon_extra:
  features:
    buildkit: true
```

`live-restore` is never emitted — it is incompatible with swarm mode.

## Destroy

Tearing down the swarm requires BOTH:
- `--tags destroy` on the command line
- `-e docker_swarm_destroy_confirm=true` on the command line

Either alone is a no-op. This double-lock exists because Ansible's
play-level tag inheritance defeats `[never]` — if the calling play has
its own tags (e.g. `tags: [swarm, docker_swarm]`), those propagate to
every task in the role and override `never`. The extra-var gate is
unambiguous: destroy only runs when the operator explicitly opts in.

Phase order:

1. Workers leave first (`docker swarm leave`).
2. Non-bootstrap managers leave (`docker swarm leave --force`).
3. Bootstrap manager leaves last — this dissolves the cluster.

What the destroy phase does NOT do:
- Uninstall Docker
- Remove firewall rules
- Wipe `/opt/docker` (or wherever the data-root points)
- Remove the docker_gwbridge

Data preservation across a destroy/rebuild cycle is intentional. A
follow-up run without `--tags destroy` re-installs the swarm on top of
the same data-root and named volumes survive.

## Caveats

- **RHEL family only.** The Docker CE install uses `dnf` and
  `yum_repository`. For Debian/Ubuntu, set
  `docker_swarm_install_docker_enabled=false` and install Docker via a
  distro-appropriate role first.
- **`live-restore` is incompatible with Swarm mode.** The role
  intentionally omits it from `daemon.json`. dockerd refuses
  `swarm init` while live-restore is enabled.
- **Encrypted overlays need IPSec ESP.** The firewall phase opens it.
  Without ESP, encrypted overlay east-west traffic times out at the
  TCP layer.
- **docker_gwbridge can only be customised before swarm join.** If a
  host is already in a swarm, the gwbridge phase skips. Re-tuning
  requires `--tags destroy` first.

## Inputs reference

Full schema in `defaults/main.yml` and `meta/argument_specs.yml`.
