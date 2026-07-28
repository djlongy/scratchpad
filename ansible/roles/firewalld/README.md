# firewalld

## TL;DR

Manages **firewalld** on EL- and Debian-family hosts: custom service XML
under `/etc/firewalld/services/`, custom zone XML under `/etc/firewalld/zones/`,
and source/interface bindings. Env-agnostic — every value lives in
`inventories/<env>/group_vars/` or `host_vars/`; `defaults/main.yml` ships
empty lists for all configurable surfaces.

**Permanent model (lockout-safe):** source CIDRs and interfaces from
`firewalld_*_zone_bindings` are **inlined into zone XML** at template time.
Runtime `firewall-cmd --permanent --add-source` alone is not enough — a later
zone re-template + `--reload` would wipe those binds and, with
`firewalld_default_zone: drop`, lock out SSH. Preferred leaf pattern:

```yaml
firewalld_default_zone: drop
firewalld_zones:
  - name: mgmt
    services: [ssh, http]   # ssh + this host's apps for admin CIDR
    protocols: [icmp]
  - name: app
    services: [http]        # peers only — no ssh
    protocols: [icmp]
firewalld_source_zone_bindings:
  - { zone: mgmt, source: "{{ networks.mgt.subnet }}" }   # exclusive
  - { zone: app,  source: "{{ networks.infra.subnet }}" }
# NEVER bind the same CIDR to two zones.
```

Prove on throwaway: `playbooks/test_firewalld_source_drop.yml`.

**Multi-zone / multi-NIC design research:** [REFERENCE.md](REFERENCE.md)
(vendor + community patterns; estate mapping; merge pad for follow-up research).

**Most common: apply updated firewall rules.** Edit `firewalld_services` /
`firewalld_zones` / `firewalld_source_zone_bindings` in group_vars, then
re-run — a no-tag run re-renders the XML and applies the bindings.

```bash
ansible-playbook -i inventories/<env>/hosts.yml playbooks/30_plat_baseline.yml [--tags services,bindings]
```

## Requirements

Install collections before running (repo `requirements.yml`, or ad-hoc):

    ansible-galaxy collection install -r requirements.yml

| Collection | When | Used for |
|---|---|---|
| `ansible.posix` | always | `firewalld` module — bindings, legacy rules |

## Key variables

Full list: `defaults/main.yml`. Contract: `meta/argument_specs.yml`.

**Required** = value must be correct for a successful run (defaults often work).
**Optional** = safe to leave default / empty; phase stays off or uses built-ins.

| Req | Variable | Default | Purpose |
|---|---|---|---|
| Optional | `firewalld_enabled` | `true` | Master toggle |
| Optional | `firewalld_default_zone` | `""` | Default zone (empty = unchanged) |
| Optional | `firewalld_services` | `[]` | Custom service XML definitions |
| Optional | `firewalld_zones` | `[]` | Custom zone XML definitions |
| Optional | `firewalld_source_zone_bindings` | `[]` | `[{zone, source}]` — preferred L3 pattern |
| Optional | `firewalld_interface_zone_bindings` | `[]` | `[{zone, interface}]` — L2, use sparingly (shared vSwitch uplink gives no real isolation) |
| Optional | `firewalld_services_remove` | `[]` | Service short names to delete |
| Optional | `firewalld_zones_remove` | `[]` | Zone short names to delete |
| Optional | `firewalld_reload` | `true` | Reload after XML changes |
| Optional | `firewall_rules` | `[]` | Legacy back-compat — accepts `"22/tcp/ssh"` or `{port,protocol,service}` |
| Optional | `firewalld_allow_no_ssh` | `false` | Permit a drop/block default with no ssh-opening activated zone. Dangerous — out-of-band-managed hosts only |
| Optional | `firewalld_skip_controller_check` | `false` | Skip the pre-flight assert that THIS controller's IP is inside an ssh source CIDR |
| Optional | `firewalld_strict_interface_audit` | `false` | Turn interface-audit warnings (NM-owned binds, unset NM zone) into hard failures |
| Optional | `firewalld_lockout_guard` | `true` | Arm a transient systemd revert timer (restores an `/etc/firewalld` snapshot) around the reload + restrictive default-zone switch |
| Optional | `firewalld_lockout_guard_seconds` | `180` | Seconds the revert timer waits before restoring the snapshot + pre-change default zone |
| Optional | `firewalld_lockout_guard_snapshot` | `/run/firewalld-lockout-guard.tar.gz` | Where the guard stores its pre-change `/etc/firewalld` snapshot (tmpfs) |

## Usage

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

firewalld_zones:
  - name: trusted-mgmt
    short: Management
    description: Internal management network — admin services
    target: default
    services: [ssh, harbor]

firewalld_source_zone_bindings:
  - { zone: trusted-mgmt, source: 192.168.10.0/24 }
```

```yaml
- hosts: docker_hosts
  become: true
  roles:
    - firewalld
```

Run it:

```bash
ansible-playbook -i inventories/mgt/hosts.yml playbooks/30_plat_baseline.yml --tags bindings
```

## Behaviour

`firewall_rules` (role only — inventory has migrated off it) remains accepted
for transitional callers; entries open in `firewalld_default_zone` (or the
firewall's actual current default, queried, if unset). Do not reintroduce it in
group_vars/host_vars — use `firewalld_services` / `firewalld_zones` /
`firewalld_source_zone_bindings`.

## Lockout safety

The role is fail-closed by construction — an SSH lockout during convergence is
meant to be impossible for a well-formed inventory and loudly rejected for a
dangerous one:

- **Pre-flight (`validate.yml`, runs `[always]`)** — a `drop`/`block` default is
  refused unless a zone both opens ssh (service `ssh`, tcp/22 port, or a **bound**
  built-in that ships ssh — `internal`/`home`) **and** has an activation (source
  bind, interface bind, inline sources/interfaces). "Restrictive default" now means
  the inventory value **or** the live one read off the host, so a host already at
  `drop` with `firewalld_default_zone` unset can no longer slip past these gates.
- **Interface audit (`interface_audit.yml`)** — SSH "falls to default" is judged
  against the **restrictive** default only (inventory `drop`/`block` target, else
  live `drop`/`block`). A non-restrictive inventory value (e.g. `public`) is never
  the compare operand, so inventory=`public` + live=`drop` + NIC in `public` is
  not a false fail.
  Zone `rich_rules` are rejected outright (they are not renderable into zone XML —
  see `templates/zone.xml.j2`), so they are no longer an ssh path; every binding
  must target a real zone;
  `firewalld_zones_remove` may not strand a bound or default zone; and THIS
  controller's client IP (from `SSH_CONNECTION`) must fall inside a source CIDR
  bound to an ssh-opening zone. Because it is tagged `[always]` it runs under any
  partial tag selection, so `--tags zones,reload` cannot re-template zone XML
  behind the contract's back. Escapes: `firewalld_allow_no_ssh`,
  `firewalld_skip_controller_check`.
- **Runtime sync (`main.yml`)** — after the module reconciles bindings, an
  explicit `firewall-cmd --reload` syncs runtime with the now-complete permanent
  state, so a binding the module treated as a permanent/runtime no-op (#451)
  still reaches the running firewall before the live gate reads it.
- **Live gate (`default_zone.yml`)** — before switching to `drop`/`block` the
  role reads the RUNNING firewall (`--get-active-zones`, `--list-services`,
  `--list-ports`, `--list-rich-rules`) and refuses unless a currently-active zone
  allows ssh. Intent in inventory cannot satisfy this — only live state can. If
  runtime shows no ssh path but PERMANENT holds an activated ssh zone, it
  self-heals (reload once, re-probe) before failing closed. It also refuses a
  restrictive switch when `firewalld_reload` is `false`, and — when the revert
  guard is enabled+applicable — refuses unless the guard timer is proven pending.
- **Interface audit (`interface_audit.yml`)** — reports each NIC's runtime zone,
  fails if the operator's own SSH NIC falls to the default zone with no covering
  source path, warns on NM-owned interface binds and unset NM zones
  (firewalld#195), and asserts `AllowZoneDrifting` is off. The SSH ingress NIC is
  taken from **`SSH_CONNECTION`'s local IP first**, with the default route only as
  a fallback for when that is unreadable. The precedence used to be inverted, so
  on a multi-homed host the audit judged the default-route NIC and could pass
  while the operator's real NIC fell to `drop`.
- **Revert guard (`guard_arm.yml` / `guard_disarm.yml`)** — when a reload will
  fire and the default is/becomes `drop`/`block`, a transient `systemd-run` timer
  is armed that restores a **snapshot of `/etc/firewalld`** taken before the
  risky window, puts the pre-change default zone back, and reloads.

  The snapshot is the point: reverting only the default zone recovered *nothing*
  on a host whose default was already `drop`/`block` — the timer set `drop` back
  to `drop` while the rewritten zone XML (and the SSH source in it) stayed
  broken. Restoring the config is what actually undoes the risky window.

  It is armed **twice**: once before the handler flush, so the reload that
  re-templates zone XML is covered at all, and again immediately before the
  default-zone switch so a long reload/reconcile cannot burn the timer budget and
  leave the switch unguarded. The snapshot and the captured pre-change default are
  taken on the **first** arm only — re-snapshotting would capture post-reload
  state, i.e. the very change the guard exists to undo.

  The timer pins `--timer-property=AccuracySec=1s`: systemd's default 1-minute
  timer accuracy would otherwise let the revert fire up to ~60s late, overshooting
  `firewalld_lockout_guard_seconds` and desyncing the reconnect probe (observed
  live — a `--on-active=70` timer fired at +104s without the pin). After the
  switch the role forces a fresh SSH connection (`reset_connection` +
  `wait_for_connection`) and only then disarms and deletes the snapshot; a real
  lockout is auto-reverted within `firewalld_lockout_guard_seconds` (validated
  `>= 120` so the probe never races its own revert). The timer is transient,
  leaving no unit behind. The role's only persistent change to a host is the
  firewall ruleset itself.

The guard never uses a full state-dropping reload, a service restart, or panic
mode, and never runs `--runtime-to-permanent` — permanent zone XML stays the
single source of truth (a runtime-to-permanent copy would let firewalld rewrite
the templated zone files and wipe an inlined SSH source on the next render).

**Live-validated (throwaway canary):** a real intentional-lockout test — default
switched to `drop` with the admin sources removed, inbound SSH watched from an
out-of-band bastion probe — confirmed the transient timer survives the lockout and
auto-reverts to a working state. A full drop-switch run with a correct inventory
recorded **zero** inbound-SSH interruption (the operator's path stays classified by
its source-bound zone throughout the switch).

**Live A/B of the snapshot revert (2026-07-29, `fwtest-01`, default already
`drop`):** with all three admin sources removed from the `mgmt` zone, the host's own
journal shows the old-style revert firing first and achieving nothing —

```
01:05:45 Started /bin/sh -c firewall-cmd --set-default-zone=drop && firewall-cmd --reload
01:05:45 sh[48077]: Warning: ZONE_ALREADY_SET: drop
01:07:30 Started /bin/sh -c tar xzf /run/fwguard-e2e.tar.gz -C /etc/firewalld && ...
01:07:31 sh[48081]: success
```

— firewalld itself reporting `ZONE_ALREADY_SET: drop` for the old revert, while the
snapshot revert restored `mgmt` to `192.168.10.0/24 192.168.0.0/24 192.168.1.0/24`
in both permanent and runtime state.

**ICMP churn (L4):** when a custom default zone omits `icmp` from its
`protocols` and `firewalld_allow_icmp` is true, the role adds ICMP to that zone
every run — list `icmp` in the zone's `protocols` to make the run idempotent.

**IPv6 controllers (L5):** the controller-containment check fails closed if you
connect from an IPv6 address while the ssh zone binds only IPv4 CIDRs — add the
v6 CIDR to the ssh zone binding, or set `firewalld_skip_controller_check=true`.

## Tag safety

**Read this before reasoning about tags here.** Tags on a **dynamic**
`include_tasks` apply to the include statement only — the include expands and
then every untagged task inside it is filtered out. Tags on a **static**
`import_tasks` propagate to all children. Verified on ansible-core 2.18.

Consequences for this role, all confirmed live:

- **`validate.yml` is `import_tasks` with `tags: [always]`** — and it must stay
  an import. As an `include_tasks` the `[always]` was a lie: under
  `--tags default_zone` the contract expanded and ran **zero** tasks while the
  directly-tagged ICMP task further down still modified the firewall. As an
  import it runs under every selection (measured: 0 → 13 contract tasks), so
  drift cannot wipe an SSH source with the contract skipped.
- **Narrow phase tags make the role inert, not dangerous.** Because the phase
  files are dynamic includes whose children carry only the role-level `firewalld`
  tag, `--tags default_zone` / `bindings` / `zones` expand their include and run
  nothing. The default-zone switch does **not** happen under `--tags
  default_zone`. If you want a phase to actually run, use `--tags firewalld`.
  (Making every phase individually tag-selectable would mean converting the
  phase includes to imports — see `.agent/PENDING.md`. Some must stay dynamic:
  `bindings_reload.yml`, `guard_disarm.yml` and
  `default_zone_selfheal_reload.yml` end in `meta: reset_connection`, which
  ignores `when:`, so the conditional has to live on the include.)
- **`--skip-tags guard` fails closed.** That is the reachable way to reach the
  switch with no guard: the arm is filtered out while `--tags firewalld` still
  runs the switch. `default_zone.yml` compares `_firewalld_guard_expected` (from
  the always-run contract) against `_firewalld_guard_active` and refuses, rather
  than switching to `drop` with no revert timer and no fresh-connection proof.
  Arm, re-arm and disarm share one tag list via a YAML anchor, so no selection
  can arm without disarming.
- **`--tags interface_audit`** — the audit recomputes the restrictive-default
  flag locally; the controller-containment fact comes from the always-run
  `validate`, and if a hand-rolled skip removes it the NIC check degrades to a
  loud warn rather than a false failure.

## Config-only mode (`firewalld_service_state: stopped`)

`stopped` is an advertised value and now behaves: permanent XML still renders,
and every runtime interaction — readiness probe, interface audit, reload
handler, binding reconcile, default-zone switch, lockout guard, verification —
is gated on `firewalld_daemon_expected_up` (`vars/main.yml`). Previously the
unconditional readiness probe (`until: rc == 0`, and `firewall-cmd --state`
exits 252 on a stopped daemon) aborted the role every time.

To skip the role entirely instead, use `firewalld_enabled: false`.
