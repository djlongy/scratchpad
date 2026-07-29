# firewalld

## TL;DR

Manages firewalld on EL- and Debian-family hosts: custom service XML under
`/etc/firewalld/services/`, custom zone XML under `/etc/firewalld/zones/`, and
the source/interface bindings that classify traffic into those zones. Permanent
XML is the single source of truth, and a restrictive default zone is switched on
only after the running firewall proves an SSH path survives it.

```bash
ansible-playbook -i inventories/<env>/hosts.yml playbooks/<playbook>.yml --tags firewalld
```

## Requirements

Install collections before running (repo `requirements.yml`, or ad-hoc):

```bash
ansible-galaxy collection install -r requirements.yml
```

| Collection | When | Used for |
|---|---|---|
| `ansible.posix` | always | `firewalld` module — runtime source/interface binds, ICMP on the default zone, legacy rules |
| `ansible.utils` | always | `in_any_network` test — the controller-containment pre-flight check |
| `community.general` | When `firewalld_services_remove` or `firewalld_zones_remove` is set | `dict_kv` filter in the cleanup phase |

## Key variables

Full list: `defaults/main.yml`. Contract, including the per-entry field schema
for zones, services, and bindings: `meta/argument_specs.yml`.

**Required** = value must be correct for a successful run (defaults often exist).
**Optional** = safe to leave default / empty.
**When X** = required only if that feature is on.

| Req | Variable | Default | Purpose |
|---|---|---|---|
| **Required** | `firewalld_default_zone` | `""` | Zone for unclassified traffic. Empty leaves the host's current default in place — the lockout gates then judge that live value, not the empty one |
| **Required** | `firewalld_zones` | `[]` | Custom zone XML: services, ports, protocols, target, inline sources/interfaces |
| **Required** | `firewalld_source_zone_bindings` | `[]` | `[{zone, source}]` — L3 classification. Each source CIDR must bind to exactly one zone |
| Optional | `firewalld_enabled` | `true` | Master toggle; `false` skips the role entirely |
| Optional | `firewalld_service_state` | `started` | `stopped` is config-only mode — XML renders, nothing talks to the daemon |
| Optional | `firewalld_services` | `[]` | Custom service XML definitions |
| Optional | `firewalld_interface_zone_bindings` | `[]` | `[{zone, interface}]` — L2 classification, for genuine plane isolation only |
| Optional | `firewalld_services_remove` | `[]` | Service short names whose XML is deleted |
| Optional | `firewalld_zones_remove` | `[]` | Zone short names whose XML is deleted |
| Optional | `firewalld_reload` | `true` | Reload firewalld after XML changes |
| Optional | `firewalld_allow_icmp` | `true` | Keep ping answerable on the default zone, including under `drop` |
| Optional | `firewalld_lockout_guard` | `true` | Arm a transient systemd revert timer around the reload and the restrictive default-zone switch |
| Optional | `firewalld_lockout_guard_seconds` | `180` | Seconds the revert timer waits before restoring the snapshot. Must be `>= 120` |
| Optional | `firewalld_lockout_guard_snapshot` | `/run/firewalld-lockout-guard.tar.gz` | Where the guard stores its pre-change `/etc/firewalld` snapshot (tmpfs) |
| Optional | `firewalld_strict_interface_audit` | `false` | Turn interface-audit warnings (NetworkManager-owned binds, unset NM zone) into hard failures |
| Optional | `firewalld_allow_no_ssh` | `false` | Permit a `drop`/`block` default with no ssh-opening activated zone. Dangerous — out-of-band-managed hosts only |
| Optional | `firewalld_skip_controller_check` | `false` | Skip the pre-flight assert that this controller's own IP falls inside an ssh source CIDR |
| Optional | `firewall_rules` | `[]` | Legacy back-compat list; refused outright on a restrictive default zone |

## Minimum configuration

```yaml
# group_vars/<inventory_group>.yml
---
# Required — unclassified traffic is silently discarded
firewalld_default_zone: drop

# Required — the zones the bindings below classify traffic into
firewalld_zones:
  - name: mgmt
    short: Management
    description: Administrative sources — SSH plus this host's admin services
    target: default
    services: [ssh]
    protocols: [icmp]

  - name: app
    short: Application
    description: Peer sources — application traffic only, never SSH
    target: default
    services: [https]
    protocols: [icmp]

# Required — one source CIDR binds to exactly one zone
firewalld_source_zone_bindings:
  - { zone: mgmt, source: 192.0.2.0/24 }
  - { zone: app, source: 198.51.100.0/24 }
```

The controller must connect from inside an ssh-opening zone's CIDR
(`192.0.2.0/24` here), or the pre-flight refuses the run.

## Usage

```yaml
- name: Configure the host firewall
  hosts: <group>
  roles:
    - role: firewalld
      tags: [firewalld]
```

Run:

```bash
export ANSIBLE_VAULT_PASSWORD=$(cat ~/secrets/vault-password.txt)
ansible-playbook -i inventories/<env>/hosts.yml playbooks/<playbook>.yml --tags firewalld
```

## Preconditions

- The host runs systemd; the revert guard schedules its rollback with
  `systemd-run`.
- A package source providing `firewalld` is reachable.
- The play reaches the host over SSH. The pre-flight reads `SSH_CONNECTION` to
  place the controller and the session NIC; a local or non-SSH connection skips
  those two checks rather than failing.
- Out-of-band console access exists before a `drop`/`block` default is applied.
  The guard is the safety net, not a substitute for one.
- NetworkManager connection profiles already carry the intended
  `connection.zone` for any interface-zoned NIC. The role audits that; it does
  not set it.

## Behaviour

Order: validate → install → interface audit → arm the revert guard → render
service and zone XML and prune orphans → reload → reconcile runtime bindings →
re-arm the guard → switch the default zone → prove a fresh SSH connection →
disarm → verify.

**Permanent XML is the single source of truth.** Sources and interfaces from
`firewalld_source_zone_bindings` and `firewalld_interface_zone_bindings` are
inlined into the zone XML at template time. A runtime `--permanent --add-source`
alone is not enough: the next zone re-template plus `--reload` would wipe it and,
under a `drop` default, strand SSH. The role therefore never runs
`--runtime-to-permanent`, never restarts the daemon in place of a reload, and
never uses panic mode — any of those would let firewalld rewrite the templated
zone files. The runtime `ansible.posix.firewalld` binds are reconcile-only, and
repair drift from a hand-edited permanent state. Out-of-band additions to a
managed zone are never adopted: verification fails the run on any attribute the
zone carries that inventory does not declare.

**Each source CIDR must bind to exactly one zone.** Inline `sources` on a zone
and entries in `firewalld_source_zone_bindings` are counted together, because
the zone template merges both into one `<source>` list. Inline on zone A plus a
binding to zone B is a dual bind and is refused.

**Zone `rich_rules` are refused.** Zone XML takes structured `<rule>` children,
not rich-language strings, so a rich rule cannot be rendered. Scope access by
CIDR instead: put the services and ports on a zone and bind the sources. For a
rule with no equivalent, apply it out of band with `ansible.posix.firewalld`
(`rich_rule:`, `permanent: true`) and keep that zone out of `firewalld_zones` —
re-templating a managed zone rewrites the file and would wipe it.

**Reloads drop the SSH session.** `firewall-cmd --reload` flushes connection
tracking, so the reload handler chains a connection reset; the play reconnects
rather than dying later with an unrelated unreachable error.

### Lockout safety

The role is fail-closed by construction: an SSH lockout during convergence is
meant to be impossible for a well-formed inventory and loudly rejected for a
dangerous one. Four gates and one timer:

- **Pre-flight contract** (`validate.yml`, imported with `tags: [always]`, so it
  runs under every tag selection). A `drop`/`block` default is refused unless
  some zone both opens ssh — service `ssh`, a tcp/22 port, or a *bound* built-in
  that ships ssh (`internal`, `home`) — **and** has an activation: a source
  bind, an interface bind, or non-empty inline `sources`/`interfaces`. Empty
  lists do not count as activation. "Restrictive default" means the inventory
  value **or** the live one read off the host, so a host already running `drop`
  with `firewalld_default_zone` unset cannot slip past. It also refuses a
  binding to an undefined non-built-in zone, a `firewalld_zones_remove` that
  strands a bound or default zone, a zone that is both defined and removed, a
  guard delay below 120 seconds, and a run whose controller client IP (from
  `SSH_CONNECTION`) falls outside every source CIDR bound to an ssh-opening
  zone. Escape hatches: `firewalld_allow_no_ssh`,
  `firewalld_skip_controller_check`.
- **Interface audit**, before the risky window. Reports each NIC's runtime zone;
  fails when the operator's own SSH NIC falls to a restrictive default with no
  covering source path; warns on NetworkManager-owned interface binds and unset
  NM zones; asserts `AllowZoneDrifting` is off. The SSH ingress NIC comes from
  `SSH_CONNECTION`'s local IP, with the default route only as a fallback when
  that is unreadable. "Falls to default" is judged against a *restrictive*
  default only — a permissive inventory value is never the compare operand.
- **Runtime sync.** After the module reconciles bindings, an explicit
  `firewall-cmd --reload` syncs runtime with the now-complete permanent state,
  so a binding the module treated as a permanent-only no-op still reaches the
  running firewall before the live gate reads it.
- **Live gate**, immediately before the switch. The role reads the *running*
  firewall (`--get-active-zones`, `--list-services`, `--list-ports`,
  `--list-rich-rules`) and refuses unless a currently-active zone allows ssh.
  Intent in inventory cannot satisfy this — only live state can. If runtime
  shows no ssh path while permanent holds an activated ssh zone, it self-heals
  (reload once, re-probe) before failing closed. It also refuses a restrictive
  switch when `firewalld_reload` is `false`, and — when the guard is enabled and
  applicable — unless the guard timer is proven pending.
- **Revert guard.** When a reload will fire and the default is or becomes
  `drop`/`block`, a transient `systemd-run` timer is armed that restores a
  **snapshot of `/etc/firewalld`** taken before the risky window, puts the
  pre-change default zone back, and reloads. Restoring the config is the point:
  on a host whose default is *already* `drop`, rolling only the default zone
  back sets `drop` to `drop` and recovers nothing while the rewritten zone XML —
  and the SSH source in it — stays broken.

  The guard arms twice: once before the **first permanent write**, so the
  snapshot captures zone XML as it stood before this run re-templates it, and
  again immediately before the switch so a long reload cannot burn the timer
  budget. The snapshot and the captured pre-change default are taken on the
  **first** arm only — re-snapshotting would capture the new state, the very
  change the guard exists to undo. The timer pins
  `--timer-property=AccuracySec=1s`, because
  systemd's default one-minute accuracy lets the revert fire up to ~60s late and
  desync the reconnect probe. After the switch the role forces a fresh SSH
  connection (`reset_connection` + `wait_for_connection`) and only then disarms
  and deletes the snapshot. A real lockout auto-reverts within
  `firewalld_lockout_guard_seconds`. The timer is transient, so nothing is left
  behind; the role's only persistent change to a host is the firewall ruleset.

### Config-only mode

`firewalld_service_state: stopped` renders permanent XML and touches nothing
runtime. Every runtime interaction — readiness probe, interface audit, reload
handler, binding reconcile, default-zone switch, lockout guard, verification —
is gated on `firewalld_daemon_expected_up` (`vars/main.yml`). To skip the role
entirely instead, set `firewalld_enabled: false`.

### Legacy `firewall_rules`

`firewall_rules` is accepted for transitional callers only; entries open in
`firewalld_default_zone`, falling back to `public` when it is unset. It is
**refused outright on a restrictive default**: the default zone is where every
unclassified NIC lands, so the rule becomes a broadly reachable hole on exactly
the hosts meant to be most closed, and a built-in default zone is never
re-templated, verified, or cleaned up by this role. Declare the ports on a zone
and bind the sources allowed to reach them instead.

### Idempotency notes

- When a *custom* zone is also the default zone and omits `icmp` from its
  `protocols`, `firewalld_allow_icmp` re-adds ICMP to it every run. List `icmp`
  in that zone's `protocols` to settle it.
- The controller-containment check fails closed for an IPv6 controller when the
  ssh zone binds only IPv4 CIDRs. Add the v6 CIDR to the binding, or set
  `firewalld_skip_controller_check: true`.

## Out of scope

- Does not manage NetworkManager connection profiles. Interface-to-zone
  assignment that must survive reboot belongs in `connection.zone`; the role
  only audits it.
- Does not manage rules published by container runtimes. A container engine's
  DNAT rules bypass zone and source filtering entirely.
- Does not manage upstream or perimeter firewalls, routing, NAT, or firewalld
  policy objects for traffic forwarded between zones.
- Does not manage rich rules, ipsets, or built-in zones beyond binding to them.

## Expected result

- `firewalld` installed; the unit in the requested state and enabled at boot.
- One XML file per entry under `/etc/firewalld/services/` and
  `/etc/firewalld/zones/`, each already carrying its inlined sources and
  interfaces.
- Every declared source is present on its zone in **both** permanent and runtime
  state, and every declared interface in permanent state — asserted by the role.
- Each zone in `firewalld_zones` exists in the permanent configuration and its
  attributes — services, ports, protocols, source ports, ICMP blocks, target,
  masquerade, forward — match inventory exactly, with nothing extra. Built-in
  zones the role does not manage are not compared.
- The running default zone equals `firewalld_default_zone` when it is set.
- The SSH ingress NIC is not sitting on a restrictive default zone without a
  covering source path.
- No `firewalld-lockout-guard` timer or snapshot remains on a successful run.

Verify by hand: `firewall-cmd --list-all-zones` for the full permanent picture,
`firewall-cmd --get-active-zones` for what is actually classifying traffic.

## Tag safety

The phase includes stamp their children with `apply.tags`, so a narrow selection
such as `--tags zones` or `--tags bindings` really does run that phase. Three
exceptions:

- **`validate.yml` is `import_tasks` with `tags: [always]`, and must stay an
  import.** Tags on a *dynamic* include apply to the include statement only —
  the include expands and every untagged child is then filtered out. As an
  import, `always` propagates to all children, so the contract runs under every
  selection and drift cannot wipe an SSH source with the gates skipped.
- **`--tags default_zone` alone refuses to switch.** The `guard` tag is not in
  that selection, so the revert timer is never armed; the switch compares
  `_firewalld_guard_expected` (from the always-run contract) against
  `_firewalld_guard_active` and fails closed rather than dropping the default
  with no net. Use `--tags default_zone,guard`, or `--tags firewalld`.
- **`--skip-tags guard` fails closed the same way** when the remaining selection
  still reaches the switch. Arm, re-arm, and disarm share one tag list via a
  YAML anchor, so no selection can arm without disarming.

Three phase files must stay dynamic includes because they end in
`meta: reset_connection`, which ignores `when:` — the conditional has to live on
the include: `bindings_reload.yml`, `guard_disarm.yml`, and
`default_zone_selfheal_reload.yml`.
