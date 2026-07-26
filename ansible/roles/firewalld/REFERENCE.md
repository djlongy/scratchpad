# firewalld reference — multi-zone & multi-NIC

Ground truth for zone behaviour, multi-NIC/NetworkManager interaction, remote-change
safety, and Ansible management approaches. Operational usage: [README.md](README.md);
this role's invariants: `tasks/main.yml` comments. Sources: [§9](#9-sources).

Version axis: firewalld 0.8 → 0.9 → 1.x; EL7/8 (iptables backend) → EL9/10
(nftables). Zone drifting removal and policy objects pivot on that line.

---

## 1. Classification

Every packet ingresses **exactly one zone**. Evaluation order:

1. **Source zone** (CIDR / host / MAC / ipset) — if it handles the packet
   (service allow, rich rule, or non-`default` target), stop. Ties between
   source zones break alphabetically by zone name.
2. **Interface zone** of the ingress NIC.
3. **Default zone** — everything unclassified.

Rules and gotchas:

- **Source beats interface beats default** — by design, not a quirk.
- **One source → one zone; one interface → one zone.** firewalld rejects a
  second binding. Dual-binding the same CIDR is the classic lockout.
- **Overlapping CIDRs are NOT longest-prefix matched** — resolution is the
  alphabetical tie-break, order-dependent. Keep source sets disjoint. Subset
  privilege = rich rules inside the parent zone or non-overlapping
  more-specific sources/ipsets. `0.0.0.0/0` as a source shadows everything.
- **ipsets** (`--add-source=ipset:name`) for large/dynamic source lists.
- **Zone priorities** (0.9+/EL9): lower number wins; legacy order on ties.
  `--set-priority`, `--set-ingress-priority`/`--set-egress-priority`.
- **Zone drifting**: pre-0.9, packets in a `target=default` source zone could
  *also* ingress the interface zone, leaking around a restrictive default.
  Off by default since 0.9/EL9 — **assert `AllowZoneDrifting=no` on EL8**.

## 2. Zone targets

| Target | Meaning |
|--------|---------|
| `default` | Only listed services/ports/protocols/rich rules pass. The whitelist. Use for source-role zones. |
| `ACCEPT` | Zone membership = broad accept. Wrong for "source AND service"; only `trusted`-style "this CIDR may talk to everything". |
| `DROP` / `%%REJECT%%` | Catch-all for unmatched traffic in that zone (stealth vs polite). A source zone with `target=DROP` is terminal — denies before interface zones. |

Ground truth on a host when semantics are unclear: `nft list ruleset`.

## 3. Single-NIC multi-zone patterns

- **Canonical:** restrictive interface/default zone (world-facing, minimal
  services) + privileged **source zones** for admin/org CIDRs (ssh, mgmt).
  Admin allowlisting never requires binding the primary NIC to a mgmt zone —
  the CIDR selects the traffic.
- **Ops variant:** custom zone per role/VLAN, `--add-source` +
  `--add-service`, `target=default`, drifting off, ipsets at scale.
- **Anti-pattern (RH-documented):** source zone with `target=ACCEPT` — accepts
  *all* traffic from the source, not just listed services.
- **Default = drop is upstream-endorsed** ("drop is worth considering …
  avoid diluting it with allowed services"). `drop` = silent discard,
  `block` = ICMP reject. Safe only when every management path is bound to an
  accepting zone **in the same converge** — the default zone catches all
  unclassified traffic, including a NIC the NM boot race (§4) dumped there.
  **Bind before you drop.** The 2026-07 estate lockout was drop-before-bind.

## 4. Multi-NIC & NetworkManager

Interface→zone is for real plane isolation (mgmt / access / storage NICs),
not admin IP allowlisting.

**NM owns interfaces it manages.** Zone comes from `connection.zone`
(nmcli/keyfile); unset → default zone. `firewall-cmd --change-interface` and
templated `<interface>` XML fight NM and may not survive reboot/NM restart.

**Boot race ([firewalld #195](https://github.com/firewalld/firewalld/issues/195)):**
firewalld starts `Before=NetworkManager.service`, can assume interfaces are
unmanaged and park them in the **default zone** at boot (may also clear
`ZONE=` in the profile). With `default=drop`: reboot-time lockout vector for
interface-zoned hosts.

Interface→zone assignment, by reliability:

1. `nmcli connection modify <con> connection.zone <zone>` — NM re-asserts on
   every up. The durable answer.
2. **Source-based zoning** — immune to the race; sources classify traffic
   regardless of which zone the NIC sits in.
3. Templated `<interface>` in zone XML — least reliable under NM; upstream
   limitation, not an Ansible bug
   ([ansible.posix #75](https://github.com/ansible-collections/ansible.posix/issues/75)).

Non-NM interfaces: nothing feeds firewalld, so firewalld's own binding is all
there is. Docker/bridge zones: leave alone; never reuse for host policy.

Hybrid layout (matches this role's `fwtest` inventory): primary NIC on
default `drop`; admin CIDRs → source `mgmt` (ssh + host apps); peer CIDRs →
source app zone; plane NICs → interface zones via NM `connection.zone`.

## 5. Reload semantics & lockout prevention

- **`--reload` preserves established connections** (prunes conntrack only for
  removed rules). **`--complete-reload` drops all state** — severe breakage
  only. An EL8-era bug killed SSH on *restart*
  ([RHBZ 1668450](https://bugzilla.redhat.com/show_bug.cgi?id=1668450)) —
  prefer reload. This role resets the Ansible connection after reload anyway
  (observed live here); harmless.
- **Runtime-first safety net:** apply runtime-only → verify a **new** SSH
  connection → `--runtime-to-permanent`. Broken? `--reload` reverts to
  last-good permanent. NOT usable on this role's templated zones (§6).
- **`--timeout=<n>`** runtime rules auto-expire — good for trials. Gotcha: a
  live timed rule gets baked in by `--runtime-to-permanent`
  ([firewalld #271](https://github.com/firewalld/firewalld/issues/271)).
- **Transient revert timer:** before a risky permanent change, schedule
  rollback via `systemd-run --on-active=<sec>` (reset default zone / restore
  backed-up XML + reload); cancel only after a fresh-connection probe.
  Catches unknown-unknowns (wrong CIDR, NAT, routing) that validation can't.
  **Gotcha (live-proven here):** `--on-active` inherits systemd's default
  `AccuracySec=1min`, so the revert can fire up to ~60s late (a `--on-active=70`
  timer fired at +104s). Pin `--timer-property=AccuracySec=1s` for a time-critical
  revert, or the guard window silently overshoots.
- **An established session proves nothing** — conntrack keeps it alive even
  under `drop`. Verify with a fresh handshake: `meta: reset_connection` +
  `wait_for_connection`.
- **Panic mode** drops everything including established sessions — console
  access only, incident response, never convergence.
- Keep OOB access (vSphere console here) as last resort.

## 6. Ansible management

| Approach | Use | Sharp edges |
|----------|-----|-------------|
| [`fedora.linux_system_roles.firewall`](https://github.com/linux-system-roles/firewall) (official; obsoletes old firewalld role) | Declarative fleet convergence via D-Bus; `previous: replaced` wipes + reapplies; check/diff mode emits `{permanent, runtime}: {added, removed}` — usable as an external drift oracle | Brief reject window during replaced-reset — ssh rule must be in the same batch |
| `ansible.posix.firewalld` module | Incremental rules | Zone creation needs `permanent: true` + manual reload before use; can't reliably persist interface binds under NM ([#75](https://github.com/ansible-collections/ansible.posix/issues/75)); permanent/runtime skew bugs ([#451](https://github.com/ansible-collections/ansible.posix/issues/451)) — always pair `permanent: true` + `immediate: true` |
| Templating `/etc/firewalld/zones/*.xml` (this role) | Full control; diffable SoT; no module skew | **firewalld rewrites zone XML** when runtime state is persisted (`--runtime-to-permanent` or any out-of-band `--permanent` change); next template push wipes those additions — with default=drop this wiped an SSH source (2026-07 incident) |

**Template-as-truth discipline (this role):** nothing else writes templated
zone files — no `--runtime-to-permanent`, no out-of-band `--permanent` on
them; trials end with `--reload` (revert), never persist; **all** sources and
interfaces are inlined into the XML at template time so a re-render is always
complete. Role order: validate → render complete XML → reload → reconcile →
default zone last → verify. Prove playbook:
`playbooks/test_firewalld_source_drop.yml` (throwaway).

Debug: `firewall-cmd --get-active-zones`, `--list-all`,
`--permanent --zone=X --list-sources`, `nft list ruleset`.

## 7. Policy objects (0.9/1.0+, EL9/10)

Rulesets for traffic **between zones** (forwarded/output) — stateful,
unidirectional (`zoneA→zoneB`), with ingress/egress zone sets plus symbolic
`HOST`/`ANY`. Only relevant for hosts that **route between NICs**; leaf
drop-default hosts need zones + sources only. Gateways use policies, not
`target=ACCEPT`/masquerade hacks. Not on EL7; partial early EL8.

## 8. Estate contract (this repo)

| Rule | Setting |
|------|---------|
| Admin SSH | source `networks.mgt.subnet` → `mgmt` zone |
| Default zone | `drop` (+ role-managed ICMP) |
| Custom zone target | `default` (whitelist) |
| CIDR exclusivity | one CIDR → one zone, disjoint sets |
| Primary NIC | never interface-bound to mgmt on leaf hosts |
| Plane NICs | interface zones via NM `connection.zone`, not zone XML |
| Drifting | assert `AllowZoneDrifting=no` on EL8 |

```yaml
firewalld_default_zone: drop
firewalld_zones:
  - name: mgmt
    services: [ssh, http]         # ssh + this host's apps, for admin CIDR
    protocols: [icmp]
    target: default
  - name: app                     # or lgtm, gitlab, …
    services: [http]              # apps only — no ssh
    protocols: [icmp]
    target: default
firewalld_source_zone_bindings:
  - { zone: mgmt, source: "{{ networks.mgt.subnet }}" }   # exclusive
  - { zone: app,  source: "{{ networks.infra.subnet }}" }
# NEVER bind the same CIDR to two zones.
# NEVER interface-bind the primary NIC to mgmt in this pattern.
```

Dual-group hosts (e.g. `lgtm` + `observability`) own the **complete**
firewalld lists in host_vars — Ansible list vars replace, they do not merge.

## 9. Sources

- **firewalld.org** —
  [zone concepts / NM](https://firewalld.org/documentation/zone/connections-interfaces-and-sources.html) ·
  [default zone](https://firewalld.org/documentation/zone/default-zone.html) ·
  [zone options](https://firewalld.org/documentation/zone/options.html) ·
  [zone priorities](https://firewalld.org/2023/04/zone-priorities) ·
  [drifting removal](https://firewalld.org/2020/01/allowzonedrifting) ·
  [policy objects](https://firewalld.org/2020/09/policy-objects-introduction) ·
  [firewall-cmd man](https://firewalld.org/documentation/man-pages/firewall-cmd.html) ·
  [firewalld.policies man](https://firewalld.org/documentation/man-pages/firewalld.policies.html)
- **GitHub** —
  [firewalld #195 NM boot race](https://github.com/firewalld/firewalld/issues/195) ·
  [firewalld #271 timeout persist](https://github.com/firewalld/firewalld/issues/271) ·
  [ansible.posix #75](https://github.com/ansible-collections/ansible.posix/issues/75) ·
  [ansible.posix #451](https://github.com/ansible-collections/ansible.posix/issues/451) ·
  [linux-system-roles/firewall](https://github.com/linux-system-roles/firewall)
- **Red Hat** —
  [zone precedence](https://access.redhat.com/solutions/7076042) ·
  [source-based zones](https://docs.redhat.com/en/documentation/red_hat_enterprise_linux/7/html/security_guide/sec-using_zones_to_manage_incoming_traffic_depending_on_source) ·
  [system-roles firewall blog](https://www.redhat.com/en/blog/automating-firewall-configuration-rhel-system-roles) ·
  [RHEL 9 firewalld/policies](https://docs.redhat.com/en/documentation/red_hat_enterprise_linux/9/html/configuring_firewalls_and_packet_filters/using-and-configuring-firewalld_firewall-packet-filters) ·
  [RHBZ 1668450](https://bugzilla.redhat.com/show_bug.cgi?id=1668450)
- **Community** —
  [Fedora FirewallD wiki](https://fedoraproject.org/wiki/FirewallD) ·
  [Linux Journal multi-zone](https://www.linuxjournal.com/content/understanding-firewalld-multi-zone-configurations) ·
  [Rocky forums practices](https://forums.rockylinux.org/t/firewalld-best-practices-for-zone-based-configuration/19079) ·
  [ServerFault drop+allowlist](https://serverfault.com/questions/680780/block-all-but-a-few-ips-with-firewalld) ·
  [oneuptime runtime vs permanent](https://oneuptime.com/blog/post/2026-03-04-manage-runtime-permanent-firewall-rules-rhel-9/view)

## 10. Open areas

- Docker / Podman / K3s CNI coexistence with host default=`drop` — unresearched.
- Rich-rule-only zones (no source bind) as an alternative — open.
- Pure nftables service vs firewalld for static multi-plane hosts — open.

*Last consolidated: 2026-07-24 (Grok research pass + Fable/Opus merge + condense).*
