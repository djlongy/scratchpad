# network_catalog — operator guide & reference

Documentation for the **`network_catalog` Jinja filter**
(`plugins/filter/network_catalog.py`). It turns **one dictionary of network
segments** into every per-platform list your modules loop over — hypervisor host port
groups, the switch fabric VLANs, edge subinterfaces, the firewall SVIs, whatever you declare.

The document has two halves for two audiences:

- **[Part 1 — Operating the catalog in this repo](#part-1--operating-the-catalog-in-this-repo)**
  — which file to edit, how to add a segment safely, how to verify, how to
  debug. Start here if you have a task.
- **[Part 2 — Engine reference](#part-2--engine-reference)** — every config
  option, its type, default and edge cases. Nothing in the engine is specific
  to this estate — it knows no site, role, zone, platform or naming
  convention — so this half (plus the plugin and contract file) can be copied
  into any other Ansible repo and used as-is.

---

# Part 1 — Operating the catalog in this repo

## What problem this solves

Before the catalog, every platform kept its own hand-maintained list of the
same networks: a distributed switch port-group list, the switch fabric VLAN list, the firewall VLAN list, a
the wifi controller list. Adding one VLAN meant editing five files with five vocabularies,
and they drifted.

Now there is **one dictionary of segments** — the single source of truth
(often shortened to "SSOT"): the one place this data is defined, which
everything else is built from. Each segment says which
[platforms](#membership-vs-namespace-the-three-meanings-of-platform) it applies
to, and declared [views](#views) reshape it into exactly the row each module
expects. Add a segment once; every list rebuilds itself.

A complete, tiny example — two segments in, one platform list out:

```yaml
network_underlays:                    # the single source of truth (input)
  web:  {vlan_id: 1101, subnet: "10.11.1.0/24", platforms: [alpha], purpose: web}
  data: {vlan_id: 1102, subnet: "10.11.2.0/24", platforms: [alpha], purpose: data}

network_platform_lists:               # a view (output shape)
  alpha:
    segments:
      platform: alpha
      fields: {display_name: name, vlan_ids: vlan_id}
```

```yaml
_net.alpha.segments:                    # what a consumer reads
  - {display_name: seg1101_web,  vlan_ids: 1101}
  - {display_name: seg1102_data, vlan_ids: 1102}
```

Names like `seg1101_web` are never typed — they are built from
[tokens](#tokens) by [name recipes](#name-recipes). The full pipeline:

```text
networks.yml               networks_config.yml         networks_platforms.yml
  network_underlays          platforms, defaults,         views (output shapes),
  (the segments)             name recipes, required       append lists, device
        |                    fields, partitions           metadata
        |                          |                          |
        +----------+---------------+--------------------------+
                   v
        plugins/filter/network_catalog.py
          pools -> merge -> enrich (defaults, names, computed fields)
          -> partitions -> views -> validation
                   |
                   v
        _networks_derived.yml   (the contract: _net, _segments, _segments_by,
                                 lookup maps, validation vars)
                   |
                   v
        roles, playbooks, host_vars   (consumers read plain data)
```

Ansible templates lazily: the filter call is *declared* once in group_vars, but
*evaluates* wherever a derived var is referenced. See
[Two traps: recalculation and recursion](#two-traps-recalculation-and-recursion).

## The six files — who owns what

All the YAML lives in `playbooks/group_vars/all/`, so it loads for every play
under `playbooks/`. A **leading underscore** — on a var *or* a filename — means
machine-owned: derived by the engine, never edited by hand.

| File | You edit it when… | Never use it for |
|---|---|---|
| `networks.yml` | **Day to day** — adding or changing a network segment | Output shapes, engine rules |
| `networks_config.yml` | The **rules** change: allowed platforms, name recipes, required fields, defaults, partitions | Adding an ordinary segment |
| `networks_platforms.yml` | A **module's row shape** changes, a static `*_extra` row is needed, or device metadata (the switch fabric trunks, edge device config) changes | Declaring ordinary segments |
| `_networks_derived.yml` | Effectively never — it is the stable contract you **read** (`_net.*` and friends) | Estate-specific values |
| `plugins/filter/network_catalog.py` | Reusable engine behaviour changes (rare; unit-tested) | Anything estate-specific — a missing knob belongs in config, not the engine |
| `playbooks/ops_net_facts.yml` | Extending the read-only browser | Applying configuration — it changes nothing |

`_networks_derived.yml` is machine-owned **by convention**, not generated on
disk: it is an ordinary group_vars file whose contents only slice the filter's
result. The underscore is a repo convention, not an Ansible mechanism.

## Terminology

| Term | Meaning |
|---|---|
| **Segment** | One logical L2/L3 network: a key under `network_underlays` with `vlan_id`, subnet, membership and identity fields. |
| **Platform** (membership) | A label in a segment's `platforms: [...]` saying which systems the segment applies to. Allow-list: `network_platforms`. See [the three meanings](#membership-vs-namespace-the-three-meanings-of-platform). |
| **Namespace** | The outer key under `network_platform_lists` / `_net` — a free output label that need **not** match any platform. |
| **View** | A recipe for reshaping segments (or an earlier view's rows) into the exact rows a module wants. See [Views](#views). |
| **Recipe** | A token-based name builder. See [Name recipes](#name-recipes). |
| **Token** | A field name on the segment (plus a few engine-added ones) used inside a recipe's `parts`. See [Tokens](#tokens). |
| **Enriched row** | A segment plus the extra fields the engine works out for you (`name`, `tagged`, `gateway_cidr`, `on_<platform>`, …). [Full list](#enriched-row-fields). |
| **Partition** | A prebuilt grouping of segments by a field value: `_segments_by.<field>.<value>`. See [Partition and lookup semantics](#partition-and-lookup-semantics). |
| **Pool** | A generator emitting `instances × roles` segments from one spec. See [Pools](#pools). |
| **Appended row** | A hand-maintained, module-shaped row bolted onto a view's output via `append:`. See [Generated vs appended vs static](#generated-vs-appended-vs-static-data). |
| **Grouped view** | A view with `group_by`: a dict of lists keyed by a field's values, instead of one list. |
| **Chained view** | A view with `source:`, built from an earlier view's **output** instead of the segments. See [Chained views](#chained-views-source). |
| **Contract file** | `_networks_derived.yml` — the one place the filter is called; it names every result slice. See [Variable name map](#variable-name-map). |
| **Pinned name** | A name declared literally on a segment, overriding its recipe. See [Pinning a name](#pinning-a-name). |

## Task navigator

| I want to… | Go to |
|---|---|
| Add / change / remove a network | [Add a segment](#workflow-add-a-segment) |
| Put an existing network on another platform | [Add a platform membership](#workflow-add-a-platform-membership) |
| Change what a module receives (row shape) | [Change a platform row shape](#workflow-change-a-platform-row-shape) |
| Add a one-off row no segment produces | [Add a static exception row](#workflow-add-a-static-exception-row) |
| Keep a legacy name the tokens can't build | [Pin a legacy name](#workflow-pin-a-legacy-name) |
| See what a platform/module receives | [Browsing and validating](#browsing-and-validating-ops_net_showyml) |
| Find who owns a VLAN / IP / name | `netshow --tags where` — [runbook](#browsing-and-validating-ops_net_showyml) |
| Understand why a row/name/list is wrong or missing | [Troubleshooting](#troubleshooting) |
| Wire a role or host to the catalog | [Consumer wiring](#consumer-wiring) |
| Filter segments myself (`platforms[]`, one platform, a tenant) | [Selecting segments yourself](#selecting-segments-yourself) |
| Understand an engine option in depth | [Part 2](#part-2--engine-reference) |

## Membership vs namespace: the three meanings of "platform"

The single most common confusion in this system. The word "platform" appears in
three distinct roles:

1. **The allow-list** — `network_platforms: [hypervisor, firewall, switches, wifi, edge]`
   in `networks_config.yml`. The only labels a segment may use. Anything else
   is a [validation error](#what-errors-catches).
2. **Segment membership** — `platforms: [hypervisor, firewall, …]` on a segment:
   *which systems this network applies to*. Views filter on it; it also drives
   the `on_<platform>` booleans and the always-built `platform`
   [partition](#partition-and-lookup-semantics).
3. **The output namespace** — the outer key under `network_platform_lists`,
   which becomes `_net.<namespace>`. This is a **free label**. It does not
   have to be (and sometimes is not) a member of the allow-list.

They only *look* like the same thing because `hypervisor`, `edge` and `wifi` happen
to use the same word for both. `switches` and `firewall` do not:

```yaml
network_platform_lists:
  switches:                     # NAMESPACE  -> read as _net.switches.vlans
    vlans:
      platform: switches    # MEMBERSHIP -> selects segments listing 'switches'
      fields: {id: vlan_id, name: short_name, key: key, site: site}
```

| `_net` namespace | Filters on membership | Lists |
|---|---|---|
| `hypervisor` | `hypervisor` | `port_groups`, `hv_mgr_portgroups` |
| `switches` | `switches` | `vlans` |
| `edge` | `edge` | `interfaces`, `zones` |
| `firewall` | `firewall` | `vlans.<site>`, `interfaces.<site>` |
| `wifi` | `wifi` | `vlans.<site>` |

Consequences:

- A segment declares `platforms: [switches]`, **never** `platforms: [switches]` —
  `switches` is not in the allow-list and would be a validation error.
- Validation checks a view's `platform:` filter against the allow-list; the
  namespace key is deliberately **not** checked — it is yours to name.
- A bare `source:` reference resolves within the **namespace**
  (`source: port_groups` under `hypervisor` means `hypervisor.port_groups`), not within
  the membership label.

## Variable name map

The same data goes by four different names as it flows through the pipeline.
This table translates between them; note the filter's own return keys appear
**nowhere** outside `_networks_derived.yml`.

| You declare (group_vars) | Filter receives (config key) | Filter returns (dict key) | You read (contract var) |
|---|---|---|---|
| `network_underlays` | *(filter input)* | `segments` | `_segments` |
| `network_platform_lists` | `views` | `views` | **`_net`** |
| `network_name_recipes` | `names` | *(via rows)* | `row.names`, `row.name` |
| `network_primary_name_recipe` | `name_default` | *(via rows)* | `row.name`, `_segment_names` |
| `network_platforms` | `platforms` | *(via rows)* | `row.on_<platform>`, `_segments_by.platform` |
| `network_segment_pools` | `pools` | *(merged into segments)* | — |
| `network_required_fields` | `required` | `missing` | `_missing_required_fields` |
| `network_segment_defaults` | `defaults` | *(merged into rows)* | — |
| `network_partition_fields` | `partition_fields` | `by` / `cidrs_by` | `_segments_by` / `_cidrs_by` |
| `network_description_template` | `desc_template` | *(via rows)* | `row.description` |
| — | — | `vlan_ids_by_platform` | `_vlan_ids_by_platform` |
| — | — | `vlan_ranges_by_platform` | `_vlan_ranges_by_platform` |
| — | — | `by_key` | `_segment_by_key` |
| — | — | `keys` / `names` | `_segment_keys` / `_segment_names` |
| — | — | `vlan_ids` / `tagged_vlan_ids` | `_vlan_ids` / `_tagged_vlan_ids` |
| — | — | `vlan_by_key`, `name_by_key`, `subnet_by_key`, `gateway_by_key`, `key_by_vlan` | same names |
| — | — | `operator_cidrs` | `_operator_cidrs` |
| — | — | `derived_names` | `_names_from_recipes` |
| — | — | `name_overrides` | `_names_pinned` |
| — | — | `errors` | `_config_errors` |
| — | — | `duplicate_names` / `duplicate_vlans` | `_duplicate_names` / `_duplicate_vlans` |

Semantics of every return key: [Return value](#return-value). Older comments
elsewhere may still say `targets[]`, `n`/`nn`, `_by`, `_vlan_ids_by_target` or
`_name_overrides` — those are pre-rename terms; the live names are
`platforms[]`, `instance`/`instance_nn`, `_segments_by`,
`_vlan_ids_by_platform` and `_names_pinned`. This document uses only live
names.

## This estate's segment contract

The engine itself requires almost nothing
([engine-read fields](#segment-fields-the-filter-reads-directly)); *this repo's
policy* (`networks_config.yml`) requires much more. A segment here must carry:

| Scope | Required fields (empty counts as missing) |
|---|---|
| **Every segment** | `vlan_id`, `platforms`, `site`, `zone`, `role` |
| **When on `firewall`** | `subnet`, `netmask`, `gateway`, `dns`, `fw_parent`, `fw_descr` |
| **When on `edge`** | `subnet`, `gateway` |
| **When on `wifi`** | `wifi_net_name`, `wifi_site` |

L3 fields are required only where a device **routes** the segment — an
L2-only isolation VLAN (e.g. a [pool](#pools)-generated tenant segment on
`hypervisor`/`switches` only) legitimately has no subnet or gateway, and the
policy no longer forces one.

> **Defaults do not satisfy required fields.** The check runs on the segment
> *as written*, before `network_segment_defaults` merge — see
> [the warning under `required`](#parameters--config-dict). Every required
> field must be on the segment itself.

Anatomy of a segment, grouped by what consumes each field:

| Group | Fields | Consumed by |
|---|---|---|
| **Fabric** | `vlan_id`, `subnet`, `netmask`, `gateway`, `dns` | Engine (`tagged`, `prefixlen`, `gateway_cidr`) + views + VM provisioning |
| **Membership** | `platforms` | View filters, `on_<platform>`, platform partition |
| **Identity (name tokens)** | `env`, `role`, `tenancy`, `site`, `zone`, `instance` | [Name recipes](#name-recipes), [partitions](#partition-and-lookup-semantics), grouped views |
| **Platform extras** | `fw_parent`, `fw_descr`, `fw_priority`, `wifi_net_name`, `wifi_site`, `hv_num_ports`, `hv_port_binding`, `hv_allow_promiscuous`, `hv_allow_forged_transmits`, `hv_allow_mac_change`, `hv_vswitch` | The matching platform's views (pass-through — the engine never reads them) |
| **Naming overrides** | `name_parts`, `name_case`, `name_sep`, `name_prefix`, `name_suffix`, `vlan_prefix`, `vlan_pad`, `names:`, a recipe-named field | [Recipe resolution](#how-one-recipe-is-resolved-for-one-segment) / [pinning](#pinning-a-name) |
| **Ops** | `description`, `operator_source` | `row.description`, `_operator_cidrs` (bastion/proxy allow-lists) |

This estate's naming policy: primary recipe `name` =
`[vlan, env, role]`, upper-case, `-`, prefix `VLAN`, pad 2 (so VLAN 0 renders
`VLAN00`, matching the live distributed switch); `short_name` = the same minus the VLAN part
(the switch fabric wants the label without `VLANnn`). Defaults stamp `tenancy: platform` and
the hypervisor host security/binding knobs (all conservative).

A copy-paste template with every required field:

```yaml
network_underlays:
  example:                            # key: short, lowercase, stable
    vlan_id: 99
    subnet: "192.168.99.0/24"
    netmask: "255.255.255.0"
    gateway: "192.168.99.1"
    dns: ["192.168.99.1"]
    platforms: [hypervisor, firewall, switches, wifi, edge]
    env: dev                          # name token + partition
    role: svc                         # name token + partition
    site: site-a                    # partition + the firewall grouping
    zone: dev                         # partition + edge zone
    description: "example service network"
    fw_parent: uplink0              # required by 'firewall'
    fw_descr: "V99_EXAMPLE"      # required by 'firewall'
    wifi_net_name: "V99_EXAMPLE"         # required by 'wifi'
    wifi_site: lid                   # required by 'wifi' + the wifi controller grouping
```

What that yields (names from the estate recipes, rows per
[Platform output map](#platform-output-map)): `name: VLAN99-DEV-SVC`,
`short_name: DEV-SVC`, a distributed switch port group, the switch fabric VLAN, the firewall VLAN + SVI
under `.site-a`, the wifi controller network under `.sa`, and a edge subinterface
`ethernet1/1.99` in zone `z-dev`.

Related variables that are **not** part of the engine:

- `network_underlay` and `networks` are YAML-anchor **aliases** of
  `network_underlays` (~180 legacy uses; kept deliberately). New code uses
  `network_underlays`.
- `network_underlay_env_primaries` / `network_underlay_env_clusters` are
  **hand-picked selections** (which segment is env X's primary) — a choice,
  not a derivation.
- See [What is not this catalog](#what-is-not-this-catalog).

## Platform output map

Every list the estate currently derives, and what is special about each:

| Output | Selects (membership) | Row shape for | Special behaviour |
|---|---|---|---|
| `_net.hypervisor.port_groups` | `hypervisor` | distributed switch port groups | Appends `hv_port_groups_extra` (the TRUNK row) |
| `_net.hypervisor.hv_mgr_portgroups` | *(chained from `port_groups`)* | `community.portgroup module.portgroup module` | `security:` emitted only when a row relaxes something; `vlan_trunk` left out when empty/false |
| `_net.switches.vlans` | `switches` | the switch fabric VLANs | Uses `short_name` (no `VLANnn` in the label) |
| `_net.edge.interfaces` | `edge`, tagged only | edge L3 subinterfaces | `consts` inject parent IF + VR; `ip` is the segment **gateway** (the subinterface *is* the gateway) |
| `_net.edge.zones` | *(chained from `interfaces`)* | edge zones | `unique_by: name` dedupes to distinct zones |
| `_net.firewall.vlans.<site>` | `firewall`, tagged only | the firewall 802.1Q VLANs | `group_by: site` — one firewall consumes one site |
| `_net.firewall.interfaces.<site>` | `firewall`, tagged only | the firewall SVIs | `group_by: site` |
| `_net.wifi.vlans.<site>` | `wifi` | the wifi controller vlan-only networks | `group_by: wifi_site`; appends `wifi_vlans_extra` (legacy rows driven to `state: absent`) |

## Generated vs appended vs static data

Three kinds of platform data, three homes. Decision rules:

| The row… | Belongs in | Example |
|---|---|---|
| Follows the segment matrix pattern | `network_underlays` (a segment) | Any ordinary VLAN |
| Is an exception, but has the same final module row shape | The view's `append:` list (`*_extra` vars in `networks_platforms.yml`) | The hypervisor host TRUNK port group; the wifi controller legacy VLANs driven absent |
| Describes physical wiring or device-global config | A plain variable outside the views | the switch fabric trunk ports, `edge_parent_interface` |

Rules for `append:` rows (full semantics: [Views → append](#how-a-result-is-assembled)):

- They use the view's **output keys** (`name`/`switch`/`vlan`), never segment
  field names (`hv_vswitch`) — they are added to the *finished* list.
- Keep their names/VLANs **outside** the matrix: generated and appended rows
  are never jointly deduplicated.
- For a grouped view the append must be a **dict** keyed like the groups; an
  append-only key gets its own bucket.
- Appended rows skip that view's reshaping — but **do** get reshaped by any
  later [chained view](#chained-views-source) (that is how the TRUNK row's
  `trunk: true` becomes `vlan_trunk` in `hv_mgr_portgroups`).

## Day-to-day workflows

All verification uses [`ops_net_facts.yml`](#browsing-and-validating-ops_net_showyml);
the `netshow` alias below is assumed.

### Workflow: add a segment

1. **Pre-flight the allocation** — is the VLAN/subnet/name free?
   `netshow --tags check -e vlan=99 -e subnet=192.168.99.0/24`
   (`--tags free` lists free VLAN blocks).
2. **Edit `networks.yml`** — copy the
   [template](#this-estates-segment-contract); include every required field
   for every platform you list.
3. **Validate**: `netshow --tags validate` — must pass; all four
   [validation vars](#validation-and-failure-model) want to be empty.
4. **Check the names**: `netshow --tags names` — is the token-built name what
   you expected? (A typo'd token drops silently —
   [why](#validation-and-failure-model).)
5. **Check the segment**: `netshow --tags where -e key=example`.
6. **Check each platform's rows**: `netshow --tags view -e view=firewall.vlans`
   etc. — the shape the module will actually loop over.
7. **Apply** through the owning playbooks (the hypervisor manager, the switch fabric, the firewall, the wifi controller —
   check-mode first where supported). The catalog itself changes nothing.

### Workflow: add a platform membership

1. Confirm the label in the allow-list (`network_platforms`) and find which
   views filter on it — [Platform output map](#platform-output-map).
2. Add it to the segment's `platforms: [...]`.
3. Add that platform's conditional fields (`firewall` → `fw_parent` +
   `fw_descr`; `wifi` → `wifi_net_name` + `wifi_site`) — validation will
   name anything missing.
4. `netshow --tags validate`, then `--tags view -e view=<ns>.<list>` for each
   list the platform gains.

### Workflow: change a platform row shape

1. Edit the view in `networks_platforms.yml` (never `networks.yml`).
2. Know your source: segment-built views use **enriched segment fields**;
   [chained views](#chained-views-source) use the **previous view's output
   keys**.
3. Name output keys exactly as the consuming module expects
   ([Field specs](#field-specs)).
4. If the view has `append:` rows, update them too — they must match the new
   **output** shape by hand.
5. Re-check `source:`/`group_by`/`unique_by`/`sort_by`/`omit_if_falsy`
   interactions against [How a result is assembled](#how-a-result-is-assembled).
6. `netshow --tags view -e view=<ns>.<list>`, then the consumer in check mode.

### Workflow: add a static exception row

1. Confirm it truly has no matrix pattern (else it's a segment).
2. Add it to the relevant `*_extra` list in `networks_platforms.yml`, using
   the view's **output keys**.
3. Manually check name/VLAN collisions against the matrix (`netshow --tags
   check -e name=… -e vlan=…`) — appended rows bypass duplicate detection.
4. Grouped views: key the append dict by the group value.
5. `netshow --tags view -e view=<ns>.<list>` — the row appears after the
   generated ones.

### Workflow: pin a legacy name

1. On the segment, set a field named after the recipe:
   `name: "OLD-VDS-NAME"` pins the primary; `short_name: "OLDLABEL"` pins the
   short name. Details: [Pinning a name](#pinning-a-name).
2. `netshow --tags names` — pinned values are starred, and primary pins are
   counted in `_names_pinned` (want: shrinking).
3. Note the audit boundary: **only primary-name pins appear in
   `_names_pinned`** — a pinned `short_name` is visible only in the starred
   `names` table.

## Browsing and validating: ops_net_facts.yml

The read-only front door — browse the catalog without reading YAML. It changes
nothing. Setup once per shell:

```bash
export ANSIBLE_VAULT_PASSWORD=$(cat ~/secrets/vault-password.txt)
source /Users/longy/venvs/ansible312/bin/activate
cd ansible/
alias netshow='ansible-playbook -i inventories/example playbooks/ops_net_facts.yml'
```

Any inventory works — the catalog is estate-wide. The playbook needs the
`community.general` and `ansible.utils` collections (for `json_query` and
`ipaddr`); the engine itself needs neither.

| Command | Shows |
|---|---|
| `netshow` | Health line (always) + every section |
| `netshow --tags validate` | **The assert** — all four validation vars, verbatim on failure |
| `netshow --tags segments` | Every segment: key, VLAN, name, subnet, site, zone, role, platforms |
| `netshow --tags vlans` | VLAN allocation map (tagged + untagged) |
| `netshow --tags free` (`-e from=… -e to=…`) | Free VLAN ids and contiguous blocks |
| `netshow --tags matrix` | Segment × platform grid |
| `netshow --tags views` | **The index**: every namespace and its lists, with row counts and keys |
| `netshow --tags platform -e p=hypervisor` | Everything one namespace gets, full YAML |
| `netshow --tags view -e view=switches.vlans` | One list, every row, in declared field order |
| `netshow --tags find -e term=mgt` | Substring search across key/name/subnet/description/role/zone/site/vlan |
| `netshow --tags where -e vlan=21` / `-e ip=192.168.21.55` / `-e key=dev_cluster` | Resolve to the owning segment: every field, every recipe's name (pins starred), which views it feeds |
| `netshow --tags names` | What each recipe produces per segment; pins starred; `_names_pinned` summary |
| `netshow --tags cidrs` | `_cidrs_by` — subnet lists per role/zone/env/site/tenancy |
| `netshow --tags check -e vlan=99 -e subnet=… -e name=…` | Conflict pre-flight before adding a segment |

The health line runs on **every** invocation, so a broken catalog is never
silently browsed.

## Consumer wiring

Consumers never call the filter — they read the contract vars. Live examples:

```yaml
# networks_platforms.yml — device dicts wired to derived lists
hv_mgr:
  vds_name: "{{ hv_switch_name }}"
  portgroups: "{{ _net.hypervisor.hv_mgr_portgroups }}"

switch_fabric_config:
  vlans: "{{ _net.switches.vlans }}"        # trunks stay hand-curated (physical)

edge_config:
  zones: "{{ _net.edge.zones }}"

# host_vars/fw-site-a-01.yml — one firewall consumes one site of a grouped view
fw_network_vlans: "{{ _net.firewall.vlans.site-a }}"

# inventory — selecting a primary segment (a choice, not a derivation)
guest_vm_network:  "{{ _name_by_key.mgt }}"
guest_vm_gateway:  "{{ network_underlays.mgt.gateway }}"

# keep an existing hand-maintained list, without touching the view
loop: "{{ my_existing_rows + _net.hypervisor.port_groups }}"
```

Switch trunk lines from `_vlan_ranges_by_platform` — per-platform VLAN ids
pre-compressed to ranges, chunked into native CLI lines with built-in Jinja
(`batch` splits a list into groups). No range logic at the consumer:

```yaml
# 104 hypervisor vlans -> ['10','20','30','40','2000-2004','2010-2014',...] ->
{% for chunk in _vlan_ranges_by_platform.hypervisor | batch(6) %}
switchport trunk allowed vlan {{ 'add ' if not loop.first }}{{ chunk | join(',') }}
{% endfor %}
# switchport trunk allowed vlan 10,20,30,40,2000-2004,2010-2014
# switchport trunk allowed vlan add 2020-2024,2030-2034,...
```

Put the [preflight assert](#preflight-assert) first in any play that consumes
the catalog.

## Selecting segments yourself

When no view gives you the shape you want, select over `_segments` directly.
`platforms` is a **list** field, so the usual `selectattr('field', 'eq', x)`
does not apply — but you rarely need to touch the list at all.

### Four ways to say "the hypervisor segments"

All four return the same set. Counts are from this repo's demo catalog
(34 segments, 11 of them on hypervisor):

| # | Expression | Rows | Use when |
|---|---|---|---|
| 1 | `_net.hypervisor.port_groups` | 15 | **Default.** A [view](#views) — already shaped for the module, and includes `append:` static rows (hence 15, not 11) |
| 2 | `_segments_by.platform.hypervisor` | 11 | You want raw segments, not shaped rows. Free — the partition is pre-built |
| 3 | `_segments \| selectattr('on_hypervisor')` | 11 | Same, but composing with other `selectattr`s in one chain |
| 4 | `_segments \| selectattr('platforms', 'contains', 'hypervisor')` | 11 | Filtering on the raw list, e.g. the label is in a variable |

Every row carries one `on_<platform>` bool per **declared** platform
([enriched row fields](#enriched-row-fields)), which is why #3 needs no
`contains` test. Reach for #4 only when the platform label is dynamic:

```yaml
# the label is not known until runtime
loop: "{{ _segments | selectattr('platforms', 'contains', target_platform) }}"

# combining: hypervisor AND tagged AND in site-a
loop: >-
  {{ _segments | selectattr('on_hypervisor') | selectattr('tagged')
     | selectattr('site', 'eq', 'site-a') | list }}

# the inverse — everything NOT on hypervisor
loop: "{{ _segments | rejectattr('on_hypervisor') | list }}"
```

`contains` is an **Ansible** test, not a Jinja one — it comes from
`ansible.builtin`, so it works in `rejectattr` and in a `when:` in any playbook
(2.14–2.21+), but a bare Jinja environment fails with
`No test named 'contains'` (verified on Jinja 3.1.6). If anything renders these
templates outside Ansible, use the precomputed `on_<platform>` bool (#3)
instead — it needs nothing but ansible-core.

### Worked example: a hypervisor port-group list with a name built from tokens

**Read `row.name`. Do not build the name yourself, and do not read
`row.names.<recipe>` for something you are going to apply.**

`row.name` is the primary recipe's output *or the pinned value where one is
pinned* — it is the only name that is safe to push at a device. The primary
recipe here is `[vlan, env, role]`, which already gives you
`VLAN10-MGT-SVC` / `VLAN31-PROD-CLUSTER`:

```yaml
- name: Reconcile distributed switch port groups
  community.portgroup module.portgroup module:
    portgroup_name: "{{ item.name }}"          # honours pins
    vlan_id: "{{ item.vlan_id }}"
    switch_name: "{{ item.hv_vswitch }}"
    num_ports: "{{ item.hv_num_ports }}"
    port_binding: "{{ item.hv_port_binding }}"
  loop: "{{ _segments | selectattr('on_hypervisor') | list }}"
  loop_control:
    label: "{{ item.key }}"
```

Better still, declare it as a [view](#views) so the rows arrive pre-shaped and
`_net.hypervisor.<list>` stays the only thing consumers read.

**To change the token set**, change the recipe — per segment when it is an
exception, estate-wide when it is the rule:

```yaml
# one segment only — this is how tenant_acme already gets VLAN02-ACME
tenant_acme:
  name_parts: [vlan, tenancy]

# estate-wide — edit network_name_recipes.name.parts in networks_config.yml
network_name_recipes:
  name:
    parts: [vlan, tenancy, env, role]     # renames almost everything: see below
```

**A second recipe is for reporting, not for applying.** Adding a `portgroup:`
recipe alongside `name:` gives you `row.names.portgroup`, but
[`names` holds derived values and never pins](#pinning-a-name). Verified against
this catalog: with a `[vlan, tenancy, env, role]` recipe added,
`legacy_storage.names.portgroup` is `VLAN50-PLATFORM-STORAGE` while
`legacy_storage.name` is the pinned `VLAN50-LEGACY-STORAGE-DO-NOT-RENAME`.
Applying the former renames a port group that was pinned precisely so it would
not be renamed. If you want a different shape to be the one you apply, make it
`network_primary_name_recipe` — then it lands in `row.name` and pins win again.

If you really must build the name inline, join the tokens and drop the empty
ones — `select` with no test keeps only truthy values:

```yaml
name: >-
  {{ ['VLAN' ~ (seg.vlan_id | string),
      seg.tenancy | default(''), seg.env | default(''), seg.role | default('')]
     | select | join('-') | upper }}
```

Five of the eleven hypervisor segments have no `env`, so the `select` is not
optional — without it `infra` renders `VLAN0--INFRA`.

#### Three things hand-building gets wrong

Run against the demo catalog — the inline expression above, versus what
`row.name` gives you:

| Segment | Inline join | `row.name` | |
|---|---|---|---|
| `infra` | `VLAN0-PLATFORM-INFRA` | `VLAN00-INFRA` | pad + noise |
| `dmz` | `VLAN9-PLATFORM-DMZ` | `VLAN09-DMZ` | pad + noise |
| `mgt` | `VLAN10-PLATFORM-MGT-SVC` | `VLAN10-MGT-SVC` | noise |
| `legacy_storage` | `VLAN50-PLATFORM-STORAGE` | `VLAN50-LEGACY-STORAGE-DO-NOT-RENAME` | **pin lost** |

1. **`tenancy` is probably not the token you want.**
   `network_segment_defaults` sets `tenancy: platform`, so *every* segment
   carries it — 32 of 34 here. Putting it in a name stamps `PLATFORM` on
   almost everything. Only `tenant_acme` and `tenant_globex` override it, and
   they already handle their own naming with `name_parts: [vlan, tenancy]`.
   This one bites recipes too, not just inline joins: it is a property of the
   token, not of how you assemble it.
2. **`vlan_pad` is lost.** The recipe pads to two digits to match the names
   already on the distributed switch; `'VLAN' ~ vlan_id` gives `VLAN0` and `VLAN9`, which are
   different port groups. A recipe fixes this; an inline join has to
   re-implement it.
3. **Pinned names are lost — this one renames production.** `legacy_storage`
   pins its name. `row.name` honours the pin; neither an inline join nor
   `row.names.<recipe>` does. This is the reason to read `row.name` and let
   the engine assemble it.

### Trap: `tenancy` and `tenant` are different fields

The demo catalog uses both, and they do not line up:

| | Field | Segments carrying it | Values |
|---|---|---|---|
| Underlays | `tenancy` | 34 (all — it is a segment default) | `platform` ×32, `acme`, `globex` |
| Pools | `tenant` | 20 (the `acme_lab` pool) | `acme` |

A pool-generated row carries **both**: `tenant: acme` *and* the inherited
`tenancy: platform`. So:

```jinja
{{ _segments | selectattr('tenancy', 'eq', 'acme') | list | length }}   {# -> 1 #}

{# `tenant` exists only on pool rows, so guard with 'defined' first —
   without it selectattr raises on every underlay segment. #}
{{ _segments | selectattr('tenant', 'defined')
             | selectattr('tenant', 'eq', 'acme') | list | length }}    {# -> 20 #}
```

`network_partition_fields` lists `tenancy` and not `tenant`, so
`_segments_by.tenancy` is `{platform: 32, acme: 1, globex: 1}` — the twenty
ACME pool segments are filed under `platform`. If you are selecting a tenant's
networks, decide which field you mean and check both; `selectattr('tenant', …)`
also needs a `selectattr('tenant', 'defined')` in front of it, since only pool
rows have the field.

### Two traps: recalculation and recursion

Ansible doesn't calculate a variable's value until something actually reads
it — and it recalculates on every read. So the whole catalog is re-built
wherever it is consumed. Two consequences:

- Call the filter **once** in group_vars and slice the result; calling it
  inside a per-host loop re-runs the entire pipeline every iteration. A
  `where`-style lookup written as a nested loop over `_net` is the classic way
  to hang a play.
- A scalar default must not reach *into* a dict that contains derived output.
  `hv_vswitch` defaults to the standalone scalar `hv_switch_name` — if it
  read `hv_mgr.switch_name` instead, templating `hv_mgr` would template its
  `portgroups` key, which needs `_net`, which needs the defaults →
  recursive-loop error. Keep any value the derivation itself needs as a
  **plain scalar** beside the dict, and have the dict reference the scalar.

## Troubleshooting

First command is always `netshow` — the health line says which validation var
is non-empty. Then:

| Symptom | Check first | Likely cause |
|---|---|---|
| `_net.<ns>.<list>` is empty | `netshow --tags view -e view=<ns>.<list>`; `_config_errors` | View's `platform:` filter vs the segments' `platforms[]` — remember [membership ≠ namespace](#membership-vs-namespace-the-three-meanings-of-platform); or a `where:` type mismatch (`true` ≠ `"true"`); or `source:` points at a [grouped view](#chained-views-source) (→ `[]`) |
| A name is missing a part | `netshow --tags names` | Unknown token dropped silently ([Tokens](#tokens)); or the field is empty on that segment |
| A generated key is shorter than expected | [Key tokens](#key-tokens--not-the-same-as-name-tokens) | `key_parts` used a name-only token (`vlan`, `key`) — renders empty |
| `_missing_required_fields` non-empty | The message names key + field | Estate policy ([segment contract](#this-estates-segment-contract)); remember defaults don't count |
| Duplicate VLAN reported | `netshow --tags vlans` | Only **tagged** ids must be unique; untagged 0 may repeat per site |
| A field vanished from a row | [Field specs](#field-specs) | Source row lacks the field (omitted, not nulled); or `omit_if_falsy`; or a `{template}` referencing a missing name; or a `{const:}`/`{group:}` typo |
| An appended row is wrong / unfiltered | [append semantics](#how-a-result-is-assembled) | Appended rows skip the reshaping, dedupe and `omit_if_falsy` steps; they use **output** keys |
| Rows out of order / `TypeError` on sort | [`sort_by`](#views) | Sorting on a key some rows lack — the **one** thing that raises |
| Output alphabetised in debug | — | Use `to_nice_yaml(sort_keys=False)` (ops_net_show does) |
| Play hangs when reading the catalog | [Recalculation trap](#two-traps-recalculation-and-recursion) | Filter called per-host / per-loop-iteration |
| "recursive loop detected" | [The recursion trap](#two-traps-recalculation-and-recursion) | A default reaches into a dict containing derived output |
| A doc/comment contradicts behaviour | [Variable name map](#variable-name-map) | Pre-rename vocabulary in an older comment — trust this doc and `_networks_derived.yml` |

## What is not this catalog

Things that live near the catalog but are **not** derived from it — do not
force them into `network_underlays`:

- **`swarm_overlays.yml`** — Docker Swarm overlay networks (container-side,
  not underlay).
- **the switch fabric trunk definitions** (`switch_fabric_config.trunks`) — physical cabling,
  hand-curated on purpose.
- **edge/the hypervisor manager device metadata** (`edge_parent_interface`, `hv_switch_name`,
  device-group/template names) — device-global config, referenced by views via
  `consts` but not segment data.
- **Env primary/cluster selections** (`network_underlay_env_primaries`,
  `network_underlay_env_clusters`) — curated choices.
- **`networks-CHEATSHEET.md`** — the native-Jinja matrix→list technique the
  plugin replaced; still useful for a repo with no plugin path.

---

# Part 2 — Engine reference

The engine contains **nothing specific to this estate**: it knows no site,
role, zone, platform or naming convention. Everything specific to a site
arrives in the config dict. Copy the plugin and its contract file into any
Ansible repo and it works.

## Synopsis

- You maintain **one source of truth**: a dict of segments
  (`network_underlays`), plus an optional generator for repeating blocks
  ([pools](#pools)).
- Each segment declares `platforms: [...]` — which systems it applies to
  ([membership](#membership-vs-namespace-the-three-meanings-of-platform)).
  Every output list filters on that, so a segment appears in exactly the
  platform lists it belongs to and no others.
- Each segment's **names are built from [tokens](#tokens)**, never typed as
  literals. A token is just a field name, so any field the segment carries is
  usable.
- You declare **[views](#views)**: per-namespace output row shapes. A view
  names its own output keys, so the filter bends to whatever the target module
  expects rather than forcing a vocabulary on it.
- A view may **`append:`** an existing hand-maintained list
  ([semantics](#how-a-result-is-assembled)). Their rows pass through
  untouched, so a setup that already has hand-maintained lists keeps them and
  gains the generated rows in the same loop.
- Everything is validated **before** any consumer reads it: unknown platforms,
  malformed views, missing required fields, duplicate names and duplicate VLAN
  ids are reported with the offending key named
  ([failure model](#validation-and-failure-model)).
- Bad input is either reported in the validation variables or replaced with
  something safe — it does not crash the run, with **one** documented
  exception ([`sort_by`](#validation-and-failure-model)).

## Requirements

- ansible-core with `filter_plugins` on the plugin path (this repo sets
  `filter_plugins = ./filter_plugins` in `ansible.cfg`).
- No collections, no pip packages. Pure Python + stdlib.

## Wiring

Call it **once**, from group_vars, and give the results names. Consumers then
read plain data — they never call the filter
([why once matters](#two-traps-recalculation-and-recursion)):

```yaml
# playbooks/group_vars/all/_networks_derived.yml
__catalog: >-
  {{ network_underlays | default({}, true) | network_catalog({
       'pools':            network_segment_pools     | default({}, true),
       'names':            network_name_recipes      | default({}, true),
       'name_default':     network_primary_name_recipe | default('', true),
       'views':            network_platform_lists    | default({}, true),
       'platforms':        network_platforms         | default([], true),
       'required':         network_required_fields   | default({}, true),
       'defaults':         network_segment_defaults  | default({}, true),
       'partition_fields': network_partition_fields  | default([], true),
       'desc_template':    network_description_template | default('', true),
     }) }}

_net: "{{ __catalog.views }}"
_segments: "{{ __catalog.segments }}"
```

Signature: `<segments dict> | network_catalog(<config dict>)`. The
`default(…, true)` guards let any config root be absent or empty-string
without breaking the call; the `>-` marker just lets the long expression span
multiple lines. The
double underscore on `__catalog` marks it internal — consume the named slices,
never `__catalog` itself ([full slice list](#variable-name-map)).

### When variables get filled in

Before the filter runs, Ansible replaces every `{{ … }}` expression inside
the settings with its actual value. The filter only ever sees the finished
values, never the `{{ … }}` text. Three things follow from that:

- You **can** use `{{ … }}` inside the catalog settings — `defaults`,
  `consts`, `append` and segment fields can all reference other variables
  (`hv_vswitch: "{{ hv_switch_name }}"`). It works as long as the referenced
  variable exists wherever the catalog is being read.
- `append: "{{ some_list }}"` looks like a piece of text in the YAML file,
  but by the time the filter sees it, it is the actual **list** — so the
  ["text is never treated as a one-item list" rule](#wrong-types--what-the-filter-does-with-them)
  does not bite here. That rule applies to what the filter *receives*, after
  the `{{ … }}` has been filled in.
- The filter itself never processes `{{ … }}`. The `{a}.{b}` templates you
  see in [field specs](#field-specs) are a different, simpler mechanism:
  each `{name}` is swapped for that field's value. Don't write `{{ … }}`
  there.

## Parameters — config dict

| Parameter | Type | Required | Default | Description |
|---|---|---|---|---|
| `platforms` | list of str | no | `[]` | The membership labels a segment may list in `platforms[]`. When set, an unknown label is a validation error; when empty, no checking happens. Also drives the `on_<platform>` booleans on every row. Distinct from view namespaces — [the three meanings](#membership-vs-namespace-the-three-meanings-of-platform). |
| `names` | dict | no | `{}` | Name **recipes** — see [Name recipes](#name-recipes). Each recipe produces one name per segment. |
| `name_default` | str | no | first recipe | Which recipe is the segment's **primary** name (the `name` field, used for duplicate detection, `_segment_names` and [pin auditing](#pinning-a-name)). |
| `views` | dict | no | `{}` | `{namespace: {list_name: spec}}` — the output row shapes. See [Views](#views). |
| `pools` | dict | no | `{}` | Spec-driven segment generation. See [Pools](#pools). |
| `required` | dict | no | `{}` | `{all: [field], by_platform: {platform: [field]}}` — fields a well-formed segment must carry. Reported in `missing`. **Checked against the segment as written, before `defaults` are merged and before enrichment** — a field supplied only by `defaults` (or computed by the engine) still counts as missing. |
| `defaults` | dict | no | `{}` | Fields every segment inherits unless it declares its own. Applied **before** the segment, so the segment always wins. Invisible to the `required` check (above). |
| `partition_fields` | list of str | no | `[]` | Which segment fields get `by` / `cidrs_by` partitions. The `platform` partition is always built regardless. [Semantics](#partition-and-lookup-semantics). |
| `desc_template` | str | no | `""` | Fallback description for a segment with no `description`, written as a text template — `{field}` placeholders are swapped for that segment's values (plus `key`, `name`, and `vid` as a plain number). If a placeholder names a field that doesn't exist, the result is `""` rather than an error. A segment's own `desc_template` beats this one. |

### Wrong types — what the filter does with them

If a setting has the wrong type (say, text where a list belongs), the filter
does not crash — it quietly substitutes something safe, usually "empty".
Knowing these rules explains most cases of "why did my value disappear".
(The rules apply *after* Ansible has filled in any `{{ … }}` —
[see above](#when-variables-get-filled-in).)

| Expected | You passed | Result |
|---|---|---|
| dict (`views`, `names`, a segment) | anything not a mapping, incl. `None` | `{}` — treated as absent (the segments **root** additionally gets a [named error](#what-errors-catches)) |
| list (`platforms`, `parts`, `append`) | `None`, a **string**, or a dict | `[]` — **a string is never treated as a one-item list** |
| list | any other sequence | converted to a list |
| number (`vlan_id`) | not actually a number | `0` + a [validation error](#what-errors-catches) |
| number (`vlan_pad`, `instances`, `vlan_base`, `vlan_stride`, `offset`) | not actually a number | the setting's default, silently |
| number (`instance`, for `instance_nn`) | not actually a number | the value as-is, unpadded |

"Empty" for required-field checking means: absent, `None`, a whitespace-only
string, or an empty list/tuple/dict/set. `0` and `false` are **not** empty.

> **How true/false is decided.** For `operator_source`, `emit_when_any` and
> `omit_if_falsy`, the filter asks "is this value empty?" — not "does this
> say false?". A real YAML `false` (no quotes) counts as false, but the
> quoted text `"false"` or `"no"` is non-empty text, so it counts as
> **true**. Always write booleans without quotes.

### Segment fields the filter reads directly

Everything else you put on a segment **passes through untouched** and is usable
as a name token, a view field and a partition field — including fields this
filter has never heard of.

| Field | Type | Required | Default | Description |
|---|---|---|---|---|
| `vlan_id` | int | **yes** | `0` | The VLAN id. Missing or non-numeric falls back to `0` **and** adds a validation error rather than crashing. `> 0` sets `tagged: true`. |
| `platforms` | list of str | no | `[]` | Membership labels. A string or dict here is a validation error. |
| `instance` | int/str | no | `""` | Instance number, for the `instance` / `instance_nn` [tokens](#tokens). Pools set it automatically (1..`instances`). |
| `subnet` | str | no | — | CIDR. Its prefix length feeds `prefixlen` and `gateway_cidr`, and it is what `cidrs_by` collects. |
| `gateway` | str | no | — | With a prefix length, produces `gateway_cidr`. |
| `prefixlen` | int | no | derived | Only consulted when `subnet` carries no `/`. |
| `operator_source` | bool | no | `false` | Marks a subnet an operator connects **from**; collected into `operator_cidrs`. Truthiness caveat [above](#wrong-types--what-the-filter-does-with-them). |
| `names` | dict | no | `{}` | Per-segment recipe overrides: `{recipe_name: {parts: [...], ...}}`. Works for **any** recipe. A recipe name that does not exist is a validation error. |
| `name_parts`, `name_case`, `name_sep`, `name_prefix`, `name_suffix`, `vlan_prefix`, `vlan_pad` | — | no | — | Flat shorthand overriding the **default recipe only**. To override another recipe use `names:`. [Resolution order](#how-one-recipe-is-resolved-for-one-segment). |
| `desc_template` | str | no | — | Per-segment description template (same `{field}` placeholder style as the global one, which it beats). Ignored when `description` is set. |
| *(a field named after a recipe)* | str | no | — | **Pins** that name, overriding the tokens. See [Pinning a name](#pinning-a-name) for what is (and is not) audited. |

Computed fields **overwrite** a segment field of the same name. Declaring
`tagged: true` on a VLAN-0 segment does nothing — the filter recomputes it.
The overwritten set is: `key`, `vlan_id`, `platforms`, `tagged`, `prefixlen`,
`gateway_cidr`, `names`, `name`, `derived_name`, `description`,
`operator_source`, `instance`, and every `on_<platform>`. (`prefixlen` is
*consulted* first when the subnet carries no `/`, then written back.)

---

## Name recipes

A recipe turns [tokens](#tokens) into one name. Recipes are evaluated in
declaration order, so a recipe may inherit from one declared earlier.

| Key | Type | Required | Default | Description |
|---|---|---|---|---|
| `parts` | list | no | `[]` | Ordered tokens joined by `sep`. Empty tokens are dropped, so there are no doubled separators. A **nested list** is a [glue group](#glue-groups). |
| `case` | str | no | `keep` | `upper`, `lower` or `keep`. Applied to the whole joined string. |
| `sep` | str | no | `-` | Separator between parts. |
| `prefix` | str | no | `""` | Auto-**prepended**, unless the literal token `prefix` appears in `parts` (then it is positional only, never doubled). |
| `suffix` | str | no | `""` | Auto-**appended**, same rule with the `suffix` token. |
| `vlan_prefix` | str | no | `""` | The text of the `vlan` / `vlan_label` tokens. |
| `vlan_pad` | int | no | `0` | Zero-pad width for the `vid` token (and the id inside `vlan`). `2` renders VLAN `0` as `00`. Ids longer than the pad are unaffected. |
| `from` | str | no | — | Inherit every unset key from another recipe — **as resolved for this segment**, so a segment that overrides `parts` drags its dependants along. The referenced recipe must be declared earlier ([why](#how-one-recipe-is-resolved-for-one-segment)). |
| `drop_tokens` | list of str | no | `[]` | Tokens to omit. With `from`, this is how a second name reuses the first minus, say, the VLAN part. A [glue group](#glue-groups) is dropped **whole** if any of its tokens is dropped. |

### Tokens

A token is a **field name on the segment**. The filter adds only these:

| Token | Value |
|---|---|
| `vlan` | `vlan_prefix` + zero-padded id — `VLAN09` |
| `vlan_label` | just the prefix — `VLAN` |
| `vid` | just the padded id — `09` |
| `key` | the segment's dict key |
| `instance` | the `instance` field, as a string |
| `instance_nn` | `instance` zero-padded to 2 — `01` (a non-numeric value is used as-is, unpadded) |
| `prefix` / `suffix` | the recipe's affixes |

An **unknown token renders as an empty part** and is dropped — it never raises.
That is why a typo silently shortens a name rather than failing: check the
result with `netshow --tags names`
([runbook](#browsing-and-validating-ops_net_showyml)).

### Glue groups

A `parts` entry may itself be a **list**, whose tokens join with **no
separator**:

```yaml
parts: [vlan, [role, instance_nn]]     # + name_suffix: inside
# instance: 1, role: labpod  ->  VLAN501-LABPOD01-INSIDE  (not LABPOD-01)
```

With `drop_tokens`, a glue group is dropped **whole** if any of its tokens is
listed — that is how "the same name minus the VLAN part" removes a glued
`VLANnn`.

### How one recipe is resolved for one segment

Each recipe is resolved per segment, in this order — later wins:

1. the recipe named by `from:`, **as already resolved for this segment**;
2. the recipe's own keys;
3. `names: {<recipe>: {...}}` on the segment;
4. the flat `name_*` / `vlan_*` shorthand — **only if this is the default recipe**.

Step 1 is what makes `short_name: {from: name}` track a segment that overrode
`name_parts`: it inherits the segment's parts, not the estate's.

> **Declare the parent first.** Recipes resolve in declaration order. A `from:`
> pointing at a recipe declared *later* still works, but falls back to that
> recipe's **raw** definition — without this segment's overrides — which is
> almost never what you want. Unlike views' `source:`, this is **not**
> validated.

### Pinning a name

A segment field named after a recipe wins outright:

```yaml
network_underlays:
  legacy:
    vlan_id: 42
    name: "OLD-VDS-NAME-DO-NOT-RENAME"   # `name` is a recipe -> pinned
```

What is and is not tracked — three related places, three answers:

- The **flat row field** (`row.name`, `row.short_name`) carries the *pinned*
  value — this is what views read.
- The row's **`names` map** always carries what each *recipe produced*
  (`row.names.name` = the derived value), even when the flat field is pinned.
  So `row.short_name != row.names.short_name` is the tell for a pinned short
  name.
- **`name_overrides` / `_names_pinned` audits the primary name only**
  (`row.name != row.derived_name`). A pinned non-primary recipe (say
  `short_name`) does **not** appear there — find those via the `names` map or
  `netshow --tags names` (pins are starred).

`derived_name` still records what the tokens *would* have produced, so "which
names are not token-built" is a query, not an audit.

```yaml
network_name_recipes:
  name:
    parts: [vlan, env, role]
    case: upper
    sep: "-"
    vlan_prefix: "VLAN"
    vlan_pad: 2
  short_name:                       # the same name minus the VLAN part
    from: name
    drop_tokens: [vlan, vlan_label, vid]
```

| parts | + knob | result |
|---|---|---|
| `[vlan, env, role]` | — | `VLAN10-MGT-SVC` |
| `[vlan, tenancy]` | `tenancy: acme` | `VLAN02-ACME` |
| `[vlan, key]` | — | `VLAN120-KTHW` |
| `[vlan]` | `name_suffix: cbr` | `VLAN100-CBR` |
| `[vlan, [role, instance_nn]]` | `instance: 1`, `name_suffix: inside` | `VLAN501-LABPOD01-INSIDE` |

> A recipe (or pin) can render **empty** — no recipes configured, or every
> token empty. An empty primary name is not itself a validation error, but
> two of them collide in `duplicate_names`.

---

## Views

`views` is `{namespace: {list_name: spec}}`. The result is read as
`_net.<namespace>.<list_name>`. The **namespace is a free output label**; only
the spec's `platform:` key filters segments —
[the three meanings of "platform"](#membership-vs-namespace-the-three-meanings-of-platform).

| Key | Type | Required | Default | Description |
|---|---|---|---|---|
| `fields` | dict | **yes** | — | `output_key: FIELD SPEC`. See [Field specs](#field-specs). A view without fields is a validation error. |
| `platform` | str | no | — | Keep only segments whose `platforms[]` contains this **membership label** (checked against the declared `platforms`). Omit to consider every segment. On a [chained view](#chained-views-source) this almost always yields `[]` — output rows don't carry a `platforms` field unless you copied it into the output yourself. |
| `where` | dict | no | `{}` | Extra equality filters on **source-row** fields, e.g. `{tagged: true}`. All must match. Exact `==`, so types matter (`true` ≠ `"true"`). On a chained view the source rows are the previous view's **output**, so `where` keys are output keys. |
| `source` | str | no | segments | Build from **another view's output** instead of the segments. See [Chained views](#chained-views-source). |
| `consts` | dict | no | `{}` | Extra names available to format templates — device-level values that are not per-segment. |
| `group_by` | str | no | — | A field name. Emits a **dict keyed by that field's values (as text)** instead of a list. |
| `sort_by` | str | no | — | Output key to sort each result by. **The one thing in this engine that can raise** — see [the failure model](#validation-and-failure-model). |
| `unique_by` | str | no | — | Output key to remove duplicates on; the first row with each value is kept. Rows missing the key all count as the same value (only the first survives); values that are lists or dicts are compared by their text form. |
| `omit_if_falsy` | list of str | no | `[]` | Output keys removed from a row when their value is empty, false or zero — for flags a module should simply not see. |
| `append` | list *or* dict | no | — | Rows added **after** the generated ones, passed through untouched. With `group_by`, pass a dict keyed the same way (an append-only key gets its own bucket). [Full semantics](#how-a-result-is-assembled). |

### Field specs

| Form | Meaning |
|---|---|
| `out: field_name` | Copy that field. **Omitted entirely if the source row lacks it** — no null keys. |
| `out: "{a}.{b}"` | A text template — each `{name}` is swapped for that field's value (row fields + `consts` + `index`/`index0`). Recognised by the presence of `{`. Not Jinja — no `{{ … }}` here. If a `{name}` doesn't exist, the whole output key is silently left out. |
| `out: {const: value}` | A literal. |
| `out: {group: {...}, emit_when_any: [f, ...]}` | A nested dict, emitted only when at least one listed field has a real value on the row (not empty/false/zero). Omit `emit_when_any` to always emit. |
| `out: <non-string>` | Emitted as-is (numbers, bools). |

`index` / `index0` are the row's 1- and 0-based position **within its group**
when `group_by` is set, so per-group numbering is correct. Appended rows never
have them (they skip the reshaping step).

A mapping field spec with neither `const` nor `group` emits **nothing** — it is
not an error, so a typo like `{konst: 5}` silently drops the field
([silent failures](#validation-and-failure-model)).

### How a result is assembled

Order matters, and it explains most surprises:

1. **filter** — `platform`, then every `where` pair;
2. **bucket** — if `group_by` is set, into `str(row[field])`. A row missing that
   field lands under the **empty-string key `""`**, not dropped;
3. **reshape** — build each output row from `fields`, then drop
   `omit_if_falsy` keys;
4. **dedupe** — `unique_by`, first row wins;
5. **append** — `append` rows are added **after** the dedupe;
6. **sort** — `sort_by` applies to the combined list.

Consequences worth knowing:

| Behaviour | Why |
|---|---|
| **Appended rows are never deduped** — `unique_by` runs before the append | step 4 precedes step 5 |
| **`omit_if_falsy` never touches appended rows** | it happens in step 3 |
| **Appended rows have no `index`** | `index` is assigned during the reshape step |
| **`sort_by` does affect appended rows** | step 6 is last |
| **A grouped view given a *list* `append` ignores it silently** | with `group_by`, `append` must be a **dict** keyed the same way |
| **A grouped append key with no generated rows still gets a bucket** | append-only groups are legal |
| **`source:` pointing at a grouped view yields `[]`** | only list-shaped views can be chained; not validated |

> **`append:` rows use the view's OUTPUT keys**, not the segment field names —
> they are appended to the finished list, not run through `fields`. This is the
> point: your existing list keeps its own shape. But note the flip side — a
> *later* [chained view](#chained-views-source) **does** reshape them.

### Chained views (source:)

`source: <list_name>` (same namespace) or `source: <ns>.<list_name>` builds a
view from **another view's finished output** instead of the segments. The
referenced view must be declared **earlier** (validated; the reference
resolves against the *namespace*, never the platform filter).

The crucial vocabulary shift: from here on, everything operates on the
previous view's **output keys**.

- `fields` copy/template **output** keys (`vlan`, not `vlan_id`, if that is
  what the parent emitted).
- `where` compares **output** keys.
- `platform:` is almost always wrong here — output rows carry no `platforms[]`
  list, so the filter matches nothing.
- **Appended rows flow through.** The parent's `append:` rows are part of its
  output, so the chained view reshapes them like any other row — a hand-written
  exception (this repo's TRUNK port group) picks up the child view's shaping
  (`vlan_trunk`, conditional `security:`) automatically. Fields the appended
  row lacks are simply left out.
- A chained view can chain further, subject to the same rules. A **grouped**
  view is a dead end — `source:` on it yields `[]` silently.

```yaml
edge:
  interfaces:                          # from segments
    platform: edge
    where: {tagged: true}
    fields: {name: "{parent}.{vlan_id}", zone: "z-{zone}"}
  zones:                               # from interfaces' OUTPUT
    source: interfaces
    unique_by: name
    fields:
      name: zone                       # 'zone' = the OUTPUT key above
      mode: {const: layer3}
```

---

## Pools

Generate `instances` × `roles` segments from one block instead of writing each by
hand. A generated segment is **indistinguishable downstream**: it goes through
the same defaults, the same name recipes, the same validation and the same
views as a hand-written one.

```
vlan = vlan_base + (n - 1) * vlan_stride + role_offset      # n = 1..instances
```

### Pool keys

| Key | Type | Required | Default | Description |
|---|---|---|---|---|
| `vlan_base` | int | no | `0` | First VLAN id. |
| `instances` | int | no | `1` | How many copies of the role group to generate; each stamps `instance: 1..N` on its segments. `instances: 0` emits nothing. |
| `vlan_stride` | int | no | **role count** | Ids per instance. The default tight-packs instances back to back; a larger value leaves numbering gaps for later expansion. |
| `roles` | list *or* dict | no | `[]` | See [Role forms](#role-forms). No roles → no segments. |
| `key_parts` | list | no | `[pool, instance, role]` | Tokens building each generated segment KEY. [Glue groups](#glue-groups) work. See [Key tokens](#key-tokens--not-the-same-as-name-tokens). |
| `key_case` | str | no | `lower` | `lower`, `upper`, or anything else to keep as-is. Lowercase by default because vars are referenced lowercase. |
| `key_sep` | str | no | `-` | Separator for the key. |
| `subnet_base` | str | no | — | Give each generated segment a **computed, unique subnet** starting at this network. See [Addressing generated segments](#addressing-generated-segments). |
| `subnet_stride` | int | no | `1` | Networks (of `subnet_base`'s size) to step per segment. |
| `gateway_offset` | int | no | `0` | With `subnet_base`: `gateway` = network address + this. `0` = no gateway. |
| `subnet_index` | str | no | `dense` | `dense` packs networks back-to-back in emission order; `vlan` numbers them by the VLAN's distance from `vlan_base`, mirroring the VLAN gaps. |
| **everything else** | — | no | — | **Stamped onto every segment the pool emits.** This includes the naming knobs ([Naming generated segments](#naming-generated-segments)) and literal addressing ([Addressing generated segments](#addressing-generated-segments)). |

A pool value that is not a dict is skipped silently; a number setting that
isn't actually a number falls back to its default
([wrong-types table](#wrong-types--what-the-filter-does-with-them)). Each generated
segment also gets `role`, `instance` (the instance number) and `pool` (the pool
key) set automatically, so all three are usable as name tokens, view fields
and partition fields.

### Role forms

```yaml
roles: [app, db, cache]                    # offset = list position: 0, 1, 2

roles:                                     # list entry as a single-key map
  - app                                    #   attaches per-role fields
  - db: {platforms: [switches]}            #   offset is still the position (1)
  - cache

roles:                                     # dict form: explicit offsets
  app:     {offset: 0}
  db:      {offset: 1}
  monitor: {offset: 5, platforms: [alpha]}   # offsets need not be contiguous
```

Per-role fields **override pool-level ones for that role only**. `offset` is
consumed by the generator and never lands on the segment.

### Naming generated segments

The naming knobs are ordinary fields, so a pool stamps them like any other —
which is how generated segments get their own naming scheme without touching
the estate recipes:

```yaml
network_name_recipes:
  name:
    parts: [vlan, tenancy, env, role, instance_nn]
    case: upper
    sep: "-"
    vlan_prefix: "VLAN"
    vlan_pad: 4

network_segment_pools:
  lab:
    vlan_base: 2000
    instances: 2
    tenancy: acme
    env: dev
    platforms: [hypervisor]
    roles: [app, db]
    name_parts: [vlan, tenancy, role, instance_nn]   # override, stamped on all
    name_suffix: seg                                 # ditto
    key_parts: [tenancy, vid, role]
```

```text
# generated key          name
acme-2000-app            VLAN2000-ACME-APP-01-SEG
acme-2001-db             VLAN2001-ACME-DB-01-SEG
acme-2002-app            VLAN2002-ACME-APP-02-SEG
acme-2003-db             VLAN2003-ACME-DB-02-SEG
```

Anything a segment can carry, a pool can stamp: `name_parts`, `name_case`,
`name_sep`, `name_prefix`, `name_suffix`, `vlan_prefix`, `vlan_pad`, a full
`names: {recipe: {...}}` override, or a field named after a recipe to
[**pin**](#pinning-a-name) that name for every member of the pool.

### Addressing generated segments

Two modes, chosen per pool. Declared values always beat computed ones.

**Mode 1 — identical every instance.** For self-isolated environments (each
VLAN NAT'd behind its own router), every instance deliberately gets the
*same* addressing, so the environments are predictable clones. Just stamp
the literals — a pool stamps any field:

```yaml
network_segment_pools:
  labs:
    vlan_base: 100
    instances: 20
    roles: [inside]
    subnet: "192.168.1.0/24"      # all 20 segments get exactly this
    gateway: "192.168.1.1"
    netmask: "255.255.255.0"
```

**Duplicate subnets are legal, on purpose.** The engine's uniqueness checks
are names and tagged VLAN ids only — nothing rejects or deduplicates a
repeated subnet, because isolated L2s never meet. (`netshow --tags check`
does *advisory* overlap checking, but only for a subnet you ask about.)

**Mode 2 — unique per segment, computed.** Set `subnet_base` and each
generated segment that doesn't declare its own subnet gets the next network
of that size; `netmask` is derived, and `gateway_offset` places the gateway:

```yaml
network_segment_pools:
  tenants:
    vlan_base: 2000
    vlan_stride: 10
    instances: 20
    roles: [app, db, web, cache, mgmt]
    subnet_base: "10.64.0.0/24"
    gateway_offset: 1
    subnet_index: vlan
```

```text
# key             vlan   subnet          gateway
tenants-01-app    2000   10.64.0.0/24    10.64.0.1
tenants-01-db     2001   10.64.1.0/24    10.64.1.1
tenants-02-app    2010   10.64.10.0/24   10.64.10.1    <- vlan-indexed: the
tenants-02-db     2011   10.64.11.0/24   10.64.11.1       gap mirrors the VLANs
```

Behaviour worth knowing:

- `subnet_index: dense` (the default) packs networks back-to-back in
  emission order — no address space wasted on VLAN gaps. `vlan` trades that
  space for a mental map: *subnet number == VLAN offset*.
- A segment (or role) that declares its own `subnet` keeps it, and its
  neighbours' computed addressing **does not shift** — the index advances
  positionally either way.
- The base's prefix length sets the size of every network (`/26` bases step
  in /26s); IPv6 bases work the same way.
- A bad `subnet_base` or a computed network that runs off the end of the
  address space is a [named error](#what-errors-catches) — the segments
  still emit, without a subnet.

### Key tokens — *not* the same as name tokens

`key_parts` is evaluated by the pool generator **before** any recipe runs, so
it sees a different token set. Getting this wrong yields a silently shortened
key, because an unknown token renders empty and is dropped
([silent failures](#validation-and-failure-model)).

| Token | Available in `key_parts`? | Value |
|---|---|---|
| `pool` | **yes** | the pool's key |
| `role` | **yes** | the role name |
| `instance` | **yes** | set number, `1`-based |
| `instance_nn` | **yes** | set number zero-padded to 2 |
| `vid` | **yes** | the VLAN id — **unpadded** (padding is a recipe concern) |
| `offset` | **yes** | the role's offset |
| *any pool or per-role field* | **yes** | `tenancy`, `site`, `env`, … |
| `vlan`, `vlan_label` | **no** | they need a recipe's `vlan_prefix`, which does not exist yet |
| `key` | **no** | this *is* what is being built |

```yaml
key_parts: [pool, instance, instance_nn, vid, offset, role, tenancy, site,
            vlan, vlan_label, key]        # last three render empty and drop
# -> x-1-01-13-3-app-t-s
```

### Collisions

A generated key that collides with a hand-written one **loses** — the
hand-written segment wins, which is how you pin one member of a pool by
declaring it explicitly. The clash is reported in `errors` so it is never
silent.

---

## Return value

A dict. The contract file names each slice — the full mapping is the
[Variable name map](#variable-name-map); the "Exposed as" names below are this
repo's.

| Key | Exposed as | Type | Description |
|---|---|---|---|
| `views` | `_net` | dict | `{namespace: {list: rows}}` — a grouped view is a dict keyed by its `group_by` values. |
| `segments` | `_segments` | list of dict | Every segment as a flat [enriched row](#enriched-row-fields). |
| `by` | `_segments_by` | dict | `<field>.<value>` → `[rows]`, from `partition_fields`, plus `platform`. |
| `cidrs_by` | `_cidrs_by` | dict | The same keys → `[subnet, ...]`. |
| `vlan_ids_by_platform` | `_vlan_ids_by_platform` | dict | platform → sorted unique **tagged** VLAN ids. |
| `vlan_ranges_by_platform` | `_vlan_ranges_by_platform` | dict | The same ids compressed to **range strings** — `['10', '2000-2004', …]`. Ready for switch trunk lines via `batch` + `join` ([example](#consumer-wiring)). |
| `by_key` | `_segment_by_key` | dict | key → row. |
| `keys` / `names` | `_segment_keys` / `_segment_names` | list | Columns. |
| `vlan_ids` / `tagged_vlan_ids` | same, `_`-prefixed | list | Sorted unique ids. |
| `vlan_by_key`, `name_by_key`, `subnet_by_key`, `gateway_by_key`, `key_by_vlan` | same | dict | Lookup maps. `key_by_vlan` is keyed by the id **as a string**. |
| `operator_cidrs` | `_operator_cidrs` | list | Subnets of segments with `operator_source: true` **that have a subnet**. |
| `derived_names` | `_names_from_recipes` | dict | key → what the recipe produced. |
| `name_overrides` | `_names_pinned` | list | `{key, name, derived_name}` for every segment whose **primary** name is pinned rather than token-built ([non-primary pins are not listed](#pinning-a-name)). Want: empty. |
| `errors` | `_config_errors` | list of str | Shape problems — see [What `errors` catches](#what-errors-catches). |
| `missing` | `_missing_required_fields` | list of str | Required-field gaps, `"<key>: missing or empty <field>"`. |
| `duplicate_names` / `duplicate_vlans` | same | list | Each colliding **value** once (not once per occurrence). VLAN duplicates consider **tagged only**, so an untagged `0` may legitimately repeat across sites. |

### Partition and lookup semantics

- **Bucket keys are always text.** `partition_fields: [floor]` over `floor: 3`
  gives `_segments_by.floor['3']`, not `[3]`. Jinja's `.` access works either
  way, but `selectattr`/`in` comparisons against the number do not.
- **Empty values are skipped**, not bucketed. A segment whose partition field is
  absent, `None` or `""` appears in no bucket for that field.
- **`cidrs_by` only collects rows that have a `subnet`.** A bucket can therefore
  have more rows in `_segments_by` than CIDRs in `_cidrs_by`.
- **The `platform` partition is always built**, even with `partition_fields: []`
  — `_segments_by.platform.<name>` and `_cidrs_by.platform.<name>` are free.
- **`subnet_by_key` / `gateway_by_key` omit segments lacking that field**, so
  they are not parallel to `keys`. `vlan_by_key` and `name_by_key` always cover
  every segment.
- **`key_by_vlan` is keyed by the id as a string** (`'21'`), and on a collision
  the **last** segment wins.
- `vlan_ids`, `tagged_vlan_ids` and `vlan_ids_by_platform` are sorted and
  de-duplicated.
- **`vlan_ranges_by_platform` covers tagged ids only** (it is built from
  `vlan_ids_by_platform`), and a platform with no tagged segments has no key
  in either.

### Enriched row fields

Your own fields, plus:

| Field | Description |
|---|---|
| `key` | The dict key. |
| `name` | The primary name (from `name_default`) — the **pinned** value when pinned. |
| `derived_name` | What the primary recipe produced, even when `name` is pinned. |
| `names` | Every **recipe's** output: `{recipe: name}` — derived values, never pins ([details](#pinning-a-name)). |
| `platforms` | The membership list, cleaned up to a proper list (never text). |
| `on_<platform>` | One bool per **declared** platform, so `selectattr('on_hypervisor')` needs no `contains` test. |
| `tagged` | `vlan_id > 0`. |
| `prefixlen` | From `subnet`, else the `prefixlen` field, else `0`. |
| `gateway_cidr` | `gateway/prefixlen`, or `""` when either is missing. |
| `description` | Declared, else `desc_template` rendered, else `""`. |
| `operator_source` | Converted to true/false ([how that's decided](#wrong-types--what-the-filter-does-with-them)). |
| `instance` | As declared (as text), or `""`. |

### What `errors` catches

| Message | Cause |
|---|---|
| `no segments — …` | Both the segments dict and `pools` are empty. |
| `network_underlays must be a dict of segments, got <type>` | Wrong type for the segments root (it is then treated as empty). |
| `<src>.<key>: vlan_id is required` | Segment has no `vlan_id`. |
| `<src>.<key>: vlan_id '<v>' is not a number (degraded to 0)` | Non-numeric `vlan_id`. |
| `<src>.<key>: platforms must be a LIST, got str` | A string or dict where a list belongs. |
| `<src>.<key>: unknown platform 'x' (allowed: …)` | Typo in `platforms[]`, checked against `platforms`. |
| `<src>.<key>: names.'x' is not a recipe…` | Per-segment override for a recipe that does not exist. |
| `pool-generated key 'x' collides…` | A pool key shadowed by a hand-written segment. |
| `pools.<key>: subnet_base '…' is not a valid network` | Unparseable [`subnet_base`](#addressing-generated-segments); segments emit without subnets. |
| `pools.<key>: computed subnet for '…' falls outside the address space…` | `subnet_base` + index stepped past the end of IPv4/IPv6 space; that segment gets no subnet. |
| `views.<ns>.<v>: fields is required…` | View with no `fields`. |
| `views.<ns>.<v>: source 'x' is not a view declared earlier` | Forward or unknown reference ([resolved against the namespace](#chained-views-source)). |
| `views.<ns>.<v>: platform 'x' is not in the declared platforms` | The view's **filter** targets an undeclared membership label. Namespace keys are never checked. |

`<src>` is `underlays` or `pools`, so you know which one to open.

---

## Validation and failure model

Four distinct failure behaviours. Know which bucket you are in before
debugging.

**1. Reported — lands in a validation variable.** Everything in
[What `errors` catches](#what-errors-catches), plus `missing`,
`duplicate_names`, `duplicate_vlans`. Assert all four before consuming
([preflight](#preflight-assert)).

**2. Falls back silently, by design.** The engine deliberately accepts fields
it has never heard of, so a typo in one cannot be told apart from a new field
— these cases quietly produce less output instead of an error:

| You wrote | You get | How to catch it |
|---|---|---|
| An unknown [name token](#tokens) | A silently shorter name | `netshow --tags names` |
| An unknown [`key_parts` token](#key-tokens--not-the-same-as-name-tokens) | A silently shorter key | inspect `_segment_keys` |
| A field-spec mapping typo (`{konst: 5}`) | The output key vanishes | `netshow --tags view` |
| A `{template}` referencing a missing name | The output key vanishes | `netshow --tags view` |
| A string where a list is expected | `[]` | [coercion table](#wrong-types--what-the-filter-does-with-them) |
| A *list* `append` on a grouped view | Ignored | `netshow --tags view` |
| `source:` pointing at a grouped view | `[]` | `netshow --tags view` |
| `platform:` on a [chained view](#chained-views-source) | Usually `[]` | `netshow --tags view` |
| A forward `from:` in a recipe | Parent's raw definition (segment overrides lost) | `netshow --tags names` |
| A number setting (pool knobs, `vlan_pad`, `instance`) that isn't a number | The setting's default / the value as-is, unpadded | [wrong-types table](#wrong-types--what-the-filter-does-with-them) |
| A non-dict pool value | Skipped | inspect `_segment_keys` |

**3. Wrong-but-plausible output.** The result looks fine but isn't what you
meant, and nothing flags it: a `where` comparing a real `true` against the
text `"true"` (they never match); a quoted `"false"` counting as true
([how true/false is decided](#wrong-types--what-the-filter-does-with-them));
partition keys being text rather than numbers; two segments with the same
VLAN id fighting over `key_by_vlan` (the last one wins).

**4. Raises — the one deliberate exception.** **`sort_by`** sorts on the raw
value, so a field that is a string on some rows and missing (`None`) on others
raises `TypeError: '<' not supported between…`. Sort on a key every row is
guaranteed to have — remember appended rows may lack it. Because the whole
filter call dies, **no validation vars exist either** — a `sort_by` raise
presents as the catalog erroring at first reference, not as a failed assert.

### Preflight assert

Put this first in any play that consumes the catalog (it is also
`netshow --tags validate`):

```yaml
- name: Validate the network catalog
  ansible.builtin.assert:
    that:
      - _config_errors | length == 0
      - _missing_required_fields | length == 0
      - _duplicate_names | length == 0
      - _duplicate_vlans | length == 0
    fail_msg: >-
      Errors: {{ _config_errors }}
      Missing: {{ _missing_required_fields }}
      Duplicate names: {{ _duplicate_names }}
      Duplicate VLANs: {{ _duplicate_vlans }}
```

---

## Evaluation order

Understanding this explains what can reference what:

1. **Pools expand** → generated segments. `key_parts` runs here, before any
   recipe exists, which is why
   [its token set is smaller](#key-tokens--not-the-same-as-name-tokens).
2. **Merge** → generated first, hand-written on top (hand-written wins;
   [collisions](#collisions) are reported).
3. **Per segment**: `defaults` → segment fields → name recipes (in declaration
   order) → computed fields (which
   [overwrite](#segment-fields-the-filter-reads-directly)).
4. **Partitions** are built from the finished segments.
5. **Views** are built in declaration order, per namespace, so
   [`source:`](#chained-views-source) can only reach a view already built.
6. **Validation** runs over the merged segments and the view declarations.
   (`required` is checked against the **raw** segments from step 2 — before
   defaults and enrichment.)

## Notes and limitations

- **Failure model in one line**: the engine reports what it can in the
  validation variables, quietly falls back to something safe everywhere else,
  and crashes in exactly one place (`sort_by`). The full breakdown:
  [Validation and failure model](#validation-and-failure-model).
- **Values are calculated on every read.** Ansible doesn't work out a
  variable until something reads it, and the whole catalog is re-built each
  time that happens. Call the filter **once** in group_vars and slice the
  result —
  [details and the recursion trap](#two-traps-recalculation-and-recursion).
- **Declaration order matters twice**, with different enforcement:
  [`source:`](#chained-views-source) in views is **validated** (a forward
  reference is an error);
  [`from:`](#how-one-recipe-is-resolved-for-one-segment) in recipes is **not**
  — a forward reference silently falls back to the parent's raw definition,
  losing that segment's overrides.
- **A grouped view is a dead end for chaining.** `source:` only accepts a
  list-shaped view; pointing it at one with `group_by` yields `[]` silently.
- **Unknown fields are a feature.** Anything you add to a segment is available
  as a name token, a view field and a partition field with no plugin change —
  which is also why field typos are not errors
  ([what that costs you](#validation-and-failure-model)).
- **`platforms: []` is legal**: the segment is documented, validated and
  partitioned, but appears in no platform list. Useful for planned or
  decommissioned space you still want reserved and visible (`netshow --tags
  matrix` shows it as all dots).

## Examples

```yaml
# Minimal: two segments, one platform, one list.
network_platforms: [alpha]
network_primary_name_recipe: name
network_name_recipes:
  name: {parts: [vlan, purpose], case: lower, sep: "_", vlan_prefix: seg, vlan_pad: 4}

network_underlays:
  web:  {vlan_id: 1101, subnet: "10.11.1.0/24", platforms: [alpha], purpose: web}
  data: {vlan_id: 1102, subnet: "10.11.2.0/24", platforms: [alpha], purpose: data}

network_platform_lists:
  alpha:
    segments:
      platform: alpha
      fields: {display_name: name, vlan_ids: vlan_id}
```

```yaml
# _net.alpha.segments  =>
- {display_name: seg1101_web,  vlan_ids: 1101}
- {display_name: seg1102_data, vlan_ids: 1102}
```

```yaml
# Brownfield: keep an existing hand-maintained list, gain the generated rows.
network_platform_lists:
  alpha:
    segments:
      platform: alpha
      fields: {display_name: name, vlan_ids: vlan_id, mtu: mtu}
      append: "{{ my_existing_nsx_rows }}"      # their shape, untouched

# Equivalent at the call site if you would rather not touch the view:
#   loop: "{{ my_existing_nsx_rows + _net.alpha.segments }}"
```

```yaml
# Grouped output: one firewall consumes one site.
network_platform_lists:
  firewall:                              # namespace — a free label
    vlans:
      platform: firewall                # membership filter
      where: {tagged: true}
      group_by: site
      fields:
        interface: fw_parent
        vlan_id: vlan_id
        descr: fw_descr

# host_vars/fw-site-a-01.yml
fw_network_vlans: "{{ _net.firewall.vlans.site-a }}"
```

```yaml
# Templates, consts, chaining, and a conditional nested block.
network_platform_lists:
  edge:
    interfaces:
      platform: edge
      where: {tagged: true}
      consts: {parent: "{{ edge_parent_interface }}"}
      fields:
        name: "{parent}.{vlan_id}"        # ethernet1/1.21
        tag: vlan_id
        ip: gateway_cidr
        zone: "z-{zone}"                  # z-mgt
    zones:                                # CHAINED: reads interfaces' OUTPUT keys
      source: interfaces
      unique_by: name
      fields:
        name: zone                        # the OUTPUT key 'zone' (z-mgt)
        mode: {const: layer3}

  hypervisor:
    port_groups:
      platform: hypervisor
      fields:
        name: name
        vlan: vlan_id
        allow_promiscuous: hv_allow_promiscuous
    hv_mgr_portgroups:
      source: port_groups                 # appended TRUNK row flows through too
      fields:
        name: name
        vlan_id: vlan                     # OUTPUT key of port_groups
        vlan_trunk: trunk
        security:                         # only for rows that relax something
          emit_when_any: [allow_promiscuous]
          group: {allow_promiscuous: allow_promiscuous}
      omit_if_falsy: [vlan_trunk]
```

```yaml
# Pools: 2 instances x 3 roles, stride 10, explicit offsets, per-role override.
network_segment_pools:
  tenant_x:
    vlan_base: 3000
    vlan_stride: 10
    instances: 2
    tenancy: tenantx
    platforms: [alpha, beta]
    key_parts: [tenancy, vid, role]
    roles:
      app:     {offset: 0}
      db:      {offset: 1}
      monitor: {offset: 5, platforms: [alpha]}     # this role skips beta
```

```text
# generated segment keys / VLANs
tenantx-3000-app   3000      tenantx-3010-app   3010
tenantx-3001-db    3001      tenantx-3011-db    3011
tenantx-3005-monitor 3005    tenantx-3015-monitor 3015
```

## See also

- [Part 1](#part-1--operating-the-catalog-in-this-repo) — file map, workflows,
  runbook and troubleshooting for this repo.
- `playbooks/ops_net_facts.yml` — the read-only browser
  ([runbook](#browsing-and-validating-ops_net_showyml)).
- `playbooks/group_vars/all/_networks_derived.yml` — the contract
  ([variable name map](#variable-name-map)).
- `playbooks/group_vars/all/networks_config.yml` — this estate's platforms,
  recipes and required fields.
- `playbooks/group_vars/all/networks_platforms.yml` — this estate's views.
- `playbooks/group_vars/all/networks-CHEATSHEET.md` — native Jinja techniques
  for matrix→list work **without** this plugin.
- `tests/unit/roles/test_network_catalog.py` — 63 tests, written against a
  fictional estate so estate vocabulary cannot leak into the engine.
