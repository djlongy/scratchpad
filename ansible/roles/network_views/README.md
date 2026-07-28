# network_views

Build per-platform lists from the network catalog by declaring them in YAML,
using ordinary Ansible template syntax. Prints them; changes nothing.

`network_catalog` already enriches every segment — names, `on_<platform>`
flags, defaults merged. This turns those rows into the exact shape a module
loops over.

## See it

```bash
ansible-playbook -i inventories/prod/hosts.yml playbooks/ops_net_views.yml
```

| Want | Add |
|---|---|
| one namespace | `-e network_views_select=hypervisor` |
| one list | `-e network_views_select=hypervisor.port_groups` |
| one column | `-e network_views_select=hypervisor.port_groups -e network_views_field=name` |
| the declaration too | `-e network_views_explain=true` |
| your own file | `-e network_views_file=/path/to/views.yml` |

## Declaring a list

`files/views.yml` is the shipped example. A view is five keys, two required:

```yaml
hypervisor:                         # namespace — a free output label
  port_groups:                # list name  → _views.hypervisor.port_groups
    platform: hypervisor            # REQUIRED: takes segments whose on_hypervisor is true
    fields:                   # REQUIRED: the shape you want out
      name: "{{ seg.name }}"
      vlan: "{{ seg.vlan_id }}"
      ports: "{{ seg.hv_num_ports | default(0) }}"
    where: {tagged: true}     # optional: extra filter, before projection
    unique_by: [vlan]         # optional: one row per distinct combination
    consts: {type: staticv4}  # optional: fixed columns on every row
    append: "{{ extras }}"    # optional: hand-written rows, passed through
```

Those seven keys are the whole vocabulary — a test keeps this list in step
with the engine, so anything the engine accepts is documented here.

`source` chains one list off another: the second projects the first's **output
rows** rather than the segments, which is how one shape is refined into another
without repeating the field list.

```yaml
hypervisor:
  port_groups:      {platform: hypervisor, fields: {...}}
  hv_mgr_shape:
    source: port_groups          # reads port_groups' OUTPUT keys
    fields:
      vds: "{{ seg.switch }}"    # `switch` is an output key of port_groups
```

The source must be declared **above** the view that reads it — lists chain in
file order. A chained view must NOT set `platform:`: it reads projected rows,
which carry output keys and no `on_<platform>` flags, so a platform filter
there matches nothing. Filter in the source, or with `where:` here. Chaining
off a `group_by` view is refused for the same class of reason — it emits
buckets, not rows.

`group_by` names a **segment field**, not an output key — grouping happens
before projection. A view that declares it emits `{bucket: [rows]}` instead of
`[rows]`, which is how one view serves several sites:

```yaml
firewall:
  vlans:
    platform: firewall
    group_by: fw_site      # -> _views.firewall.vlans.site-a, .site-b
    fields:
      vlan_id: "{{ seg.vlan_id }}"
```

With `group_by`, `append` becomes a **mapping** keyed by bucket
(`append: {site-a: [ {...} ]}`); a bucket that exists only in `append` is
still emitted, so a wholly hand-maintained site does not vanish. Bucket keys
are stringified, so they match the generated ones.

`unique_by` takes a **list**, because uniqueness is usually composite. Once
VLAN 10 exists at two sites, `[vlan]` would collapse two real L2 domains and
`[vlan, site]` is the truthful key. It names **output** keys, not segment
fields, because dedup runs after projection. `append` rows are never deduped.

`seg` is the current segment, the way `item` is the current loop element.
`{{ }}`, `~`, `| default(...)` and interpolation mean exactly what they mean
anywhere else — there is nothing role-specific to learn.

Read a value, or build one:

```yaml
fqdn:  "{{ seg.key }}.{{ seg.site }}.example.com"
label: "{{ seg.site ~ '/' ~ seg.zone }}"
name:  >-
  {{ (['VLAN' ~ '%02d' | format(seg.vlan_id),
       seg.env | default(''), seg.role | default('')]
      | select | join('-')) | upper }}
```

Types survive: `vlan` comes back an `int`, not `"1101"`. That needs
`jinja2_native = True`, which `ansible.cfg` sets; the filter refuses to run
without it rather than hand a module a stringified number.

## Using a list in your own play

```yaml
- name: Reconcile port groups
  your_collection.dvs_portgroup:      # your platform's port-group module
    portgroup_name: "{{ item.name }}"
    vlan_id: "{{ item.vlan }}"
  loop: "{{ _views.hypervisor.port_groups }}"
  vars:
    _views: >-
      {{ _segments | network_views(lookup('file', 'path/to/views.yml') | from_yaml) }}
```

The `lookup(...) | from_yaml` chain is load-bearing. Ansible renders a
variable the moment it is referenced, so a views dict declared as a plain var
would be templated before any row exists — against a scope with no `seg` in
it. Ansible does not re-template a filter chain's output, which is what lets
the `{{ seg.x }}` survive to the filter. `!unsafe` is not an alternative: on a
mapping it preserves the top-level strings and **empties the nested ones**.

## When it refuses

Every rejection names the view and the key, because the alternative is a wrong
list reaching a device quietly:

```
network_views [hypervisor.port_groups]: unknown key 'wheree'. Did you mean 'where'?
  Known keys: append, consts, fields, platform, where

network_views [hypervisor.port_groups]: `platform: wifi` is not a platform this
  catalog knows — no segment carries 'on_wifi', so this view would build
  nothing. Declared: catalog_only, hypervisor, firewall, edge, switches, wifi

network_views [firewall.interfaces]: `where: {vlan_id: '30'}` is a str but
  segments carry 'vlan_id' as int — the comparison can never match.
  Quote or unquote the value.
```

Caught: unknown spec keys, a platform no segment carries, `where` on an
unknown field or with a mistyped value, `fields`/`consts` collisions, `append`
of the wrong shape, rows of the wrong shape, and any field expression that
raises — reported with the field name and the segment it broke on.

## Tests

```bash
uv run --with pytest --with pyyaml --with jinja2 -- \
  python -m pytest tests/unit/roles/test_network_views.py -q
```

24 tests, no ansible install required.
