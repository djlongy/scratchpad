# Network matrix → lists: Jinja cheat sheet

**Purpose:** portable patterns for turning **one dictionary matrix** (SSOT) into
every list/map a platform role needs to loop over — without `set_fact`, without
duplicating VLAN IDs.

**Audience:** any Ansible estate (lab or production). Examples use generic
RFC1918 names only — no site, vendor lock-in, or personal topology.

Copy this file into a repo. Rename SSOT fields to match the local matrix.

**Companion example vars (optional):** `networks.yml` in the same directory if present.

T0–T15 stay lean (pattern only). Long line-by-line Jinja walks live in
**[Appendix A](#appendix-a--annotated-t4-enrich-loop)** at the end.

---

## How to read this sheet (pattern recognition)

Every trick uses the **same tiny toy matrix** as BEFORE (unless noted). Scan the
**BEFORE** shape until it matches what you have at work, then look at **AFTER** —
that is what your role will `loop:`.

```text
  BEFORE (what you have)              trick            AFTER (what you loop)
  ──────────────────────              ─────            ─────────────────────
  dict of dicts          ──────────►  T1 dict2items ─► list of {key, value}
  list of dicts          ──────────►  T5 selectattr ─► shorter list of dicts
  list of dicts          ──────────►  T3 map attr   ─► list of scalars
  list of dicts          ──────────►  T6/T7 reshape ─► list of renamed dicts
  dynamic list + hand    ──────────►  T10 a + b     ─► one combined list
```

---

## Shared toy matrix (BEFORE for every trick)

Real estates are larger; the **shape** is what matters.

```yaml
fabric_underlays:
  mgt:
    vlan_id: 10
    portgroup: "VLAN10-MGT-SVC"
    subnet: "192.168.10.0/24"
    gateway: "192.168.10.1"
    dns: ["192.168.10.1"]
    site: site_a
    zone: mgt
    role: svc
    fw_parent: eth1
    fw_descr: "V10_MGT_SVC"
    wifi_name: "V10_MGT_SVC"
    wifi_site: a
    admin: true

  prod:
    vlan_id: 30
    portgroup: "VLAN30-PROD-SVC"
    subnet: "192.168.30.0/24"
    gateway: "192.168.30.1"
    dns: ["192.168.30.1"]
    site: site_a
    zone: prod
    role: svc
    fw_parent: eth1
    fw_descr: "V30_PRD_SVC"
    wifi_name: "V30_PRD_SVC"
    wifi_site: a

  infra:
    vlan_id: 0
    portgroup: "VLAN00-INFRA"
    subnet: "192.168.0.0/24"
    gateway: "192.168.0.1"
    dns: ["192.168.0.1"]
    site: site_a
    zone: infra
    role: infra
    admin: true
    # no fw_parent, no wifi_site

  site_b_lab:
    vlan_id: 100
    portgroup: "VLAN100-SITEB"
    subnet: "192.168.100.0/24"
    gateway: "192.168.100.1"
    dns: ["192.168.100.1"]
    site: site_b
    zone: lab
    role: lab
    fw_parent: eth1
    fw_descr: "VLAN100"
    wifi_name: "VLAN100"
    wifi_site: b
```

```text
fabric_underlays          ← dict (map)
├── mgt:     { vlan_id, subnet, site, … }
├── prod:    { … }
├── infra:   { … }
└── site_b_lab: { … }
```

---

## Mental model

```
┌─────────────────────────┐
│  SSOT dict (matrix)     │  key → {vlan_id, subnet, site, …}
│  fabric_underlays.*    │
└───────────┬─────────────┘
            │  lazy Jinja (at reference time)
            ▼
┌─────────────────────────┐
│  derived vars           │  lists / maps shaped per consumer
└─────────────────────────┘
            │
            ▼
   host_vars / roles loop them
```

**Rules of thumb**

1. Define a fact **once** in the matrix. Never re-type a VLAN ID in host_vars.
2. Inventory only **selects** a row: `fabric_underlays[network_segment].gateway`.
3. Pick the lightest trick that produces the AFTER shape you need.
4. Hand-curate only what is not in the matrix (trunks, legacy absents).
5. At a new job: check core version + `jinja2_native` (next section).

---

## Version & ansible.cfg quirks

Patterns in this sheet work on **ansible-core 2.14–2.18+**. Work often runs
**2.16**; this repo currently runs **2.18**. The *syntax* is the same; the
**type of a templated value** can differ, mostly because of one setting.

### First thing at a new site

```bash
ansible --version | head -2
ansible-config dump | grep -E 'JINJA2_NATIVE|DEFAULT_JINJA|COLLECTIONS_PATH|HOST_KEY'
# or read their ansible.cfg [defaults]
```

| Check | Why it matters |
|---|---|
| `ansible-core` version | 2.16 vs 2.18: same Jinja filters; packaging / defaults differ slightly |
| `jinja2_native` | **Biggest** behaviour fork for group_vars lists/strings |
| `jmespath` installed? | T6 `json_query` fails with a clear error if missing |
| `community.general` | T7 map+json_query, T13 `counter` |
| Python on controller | 3.9+ typical for 2.16; 3.11+ common for 2.18 |

### `jinja2_native` — the setting that changes everything

```ini
# ansible.cfg
[defaults]
jinja2_native = True    # or False
```

| | `jinja2_native = False` (Ansible default through 2.16 era) | `jinja2_native = True` (this repo) |
|---|---|---|
| What it does | Template results stay **strings** unless you cast | Template results are parsed into **native Python types** (list, dict, int, bool, …) |
| `{{ [1, 2] }}` in a var | Often a string that *looks* like a list (older cores) | Real `list` |
| `{{ true }}` / `{{ 1 + 1 }}` | `"True"` / `"2"` strings more often | `True` / `2` |
| `selectattr('tagged')` on bools | May need `\| bool` if flags came through as strings | Bools usually real — bare `selectattr('tagged')` works after T4 |
| `\| join(',')` on `[2, 3, 10]` **in group_vars** | Usually string `"2,3,10"` | **Re-parsed as tuple** `(2, 3, 10)` — classic footgun |
| Same `join(',')` **in a task** template | string | string (safe) |
| Loops over derived lists | Sometimes need `\| from_json` / `\| list` | Lists loop cleanly if you built real lists |

**Defaults by era (approximate — always verify with `ansible-config dump`):**

| ansible-core | Typical default `jinja2_native` |
|---|---|
| ≤ 2.16 | **False** (unless site set it) |
| 2.17–2.18 | Still **False** by default; many modern repos set **True** |
| 2.19+ | Moving toward native-by-default in upstream messaging — **measure, don't assume** |

This repo (`ansible/ansible.cfg`):

```ini
[defaults]
jinja2_native = True
```

**Portable pattern that works on both 2.16 (native off) and 2.18 (native on):**

```yaml
# Stage 1 — always materialise complex structures as JSON text
my_items: |
  {% set items = [] %}
  {% for key, net in fabric_underlays.items() %}
  {%   set _ = items.append({'key': key, 'vlan_id': net.vlan_id | int, ...}) %}
  {% endfor %}
  {{ items | to_json }}

# Stage 2 — always decode before filter/loop
my_tagged: "{{ my_items | from_json | selectattr('tagged') | list }}"
```

Why: when native is **off**, a bare `{{ items }}` at the end of a block scalar can
hand you a **string repr** of the list on some 2.14–2.16 paths; `to_json` /
`from_json` forces a round-trip that is identical on every core. When native is
**on**, the bridge is harmless (you get a list either way after `from_json`).

### Other `ansible.cfg` knobs that affect matrix / Jinja work

```ini
[defaults]
# --- typing / templates ---
jinja2_native = True          # see table above; set explicitly at every job
# allow_broken_conditionals   # older; avoid relying on broken truthiness

# --- variable merge (rare; don't surprise yourself) ---
# hash_behaviour = replace    # DEFAULT — later dict wins entirely
# hash_behaviour = merge      # deep-merge dicts (deprecated path; avoid new use)

# --- facts (not Jinja, but changes what you can map over) ---
gathering = smart
fact_caching = jsonfile
fact_caching_connection = /tmp/ansible_facts/...
fact_caching_timeout = 7200

# --- collections / filters used by this sheet ---
collections_path = ./collections:~/.ansible/collections:/usr/share/ansible/collections

# --- inventory / connection (ops, not typing) ---
host_key_checking = True      # or False in labs
interpreter_python = auto_silent
```

| Setting | Effect on “dict → lists” work |
|---|---|
| `jinja2_native` | Type of every templated group_var (list vs str, int vs `"10"`) |
| `hash_behaviour=merge` | Inventory/group_vars dicts **merge** instead of replace — can silently combine two `network_*` maps; prefer **replace** + explicit `combine` |
| `gathering` / fact cache | Only matters if you derive from **facts** (`ansible_interfaces`, …), not from a static matrix |
| `collections_path` | Must see `community.general` for T7/T13; must see `ansible.utils` if you use `ipaddr` |
| `stdout_callback` / `result_format=yaml` | How `debug` looks when you dump vars (paste quality), not runtime types |

Env var overrides (useful when the site cfg is read-only):

```bash
# Temporarily force native off to compare behaviour
ANSIBLE_JINJA2_NATIVE=False ansible-playbook ...

# See effective config
ansible-config dump --only-changed
```

### ansible-core 2.16 vs 2.18 — practical deltas

| Topic | 2.16 (typical work) | 2.18 (this repo) | What to do |
|---|---|---|---|
| Jinja filter set for T0–T15 | Same (`dict2items`, `selectattr`, `map`, `combine`, `to_json`) | Same | No syntax fork |
| `jinja2_native` default | Usually **False** | Still usually False; **we set True** | Always set explicitly in cfg or document |
| List-in-var typing | More often need `to_json`/`from_json` | Cleaner with native on | Keep the bridge in shared snippets |
| `items2dict` | Available | Available | T9 OK on both |
| `community.general.counter` | Needs collection installed | Same | Pin collection in `requirements.yml` |
| `json_query` / jmespath | Needs `pip install jmespath` on **controller** | Same | CI image must include it |
| `ansible.utils.ipaddr` | Needs `netaddr` + collection | Same | Or `subnet.split('/')[1]` (no dep) |
| Nested `selectattr('value.admin')` | Works on dict2items | Works | Same T2 pattern |
| Data tagging / unsafe | Older model | Stricter in places | Prefer `\| string` / explicit casts if a filter complains |
| Controller Python | 3.9–3.11 common | 3.11–3.12 common | Don't assume 3.12-only syntax in filter plugins |

**There is no separate “2.16 Jinja dialect” for these tricks.** Failures at work are almost always:

1. `jinja2_native` different from what you tested at home  
2. Missing `jmespath` / `community.general`  
3. Forgot `| from_json` on a T4/T8 intermediate  
4. Comma-`join` of ints stored in **group_vars** under native=True  

### Quick type probe (run at work and at home)

```yaml
# /tmp/jinja-type-probe.yml
- hosts: localhost
  gather_facts: false
  vars:
    as_list: "{{ [2, 3, 10] }}"
    as_join: "{{ [2, 3, 10] | join(',') }}"
    as_pipe: "{{ [2, 3, 10] | join('|') }}"
    as_json: "{{ [2, 3, 10] | to_json }}"
    as_bool: "{{ true }}"
  tasks:
    - ansible.builtin.debug:
        msg:
          core: "{{ ansible_version.full }}"
          native_cfg: "run: ansible-config dump | grep JINJA2_NATIVE"
          as_list: "{{ as_list | type_debug }} => {{ as_list }}"
          as_join: "{{ as_join | type_debug }} => {{ as_join }}"
          as_pipe: "{{ as_pipe | type_debug }} => {{ as_pipe }}"
          as_json: "{{ as_json | type_debug }} => {{ as_json }}"
          as_bool: "{{ as_bool | type_debug }} => {{ as_bool }}"
    # Task-level join (usually str even when var-level join is weird)
    - ansible.builtin.debug:
        msg: "task_join={{ [2, 3, 10] | join(',') }} ({{ [2, 3, 10] | join(',') | type_debug }})"
```

```bash
ansible-playbook /tmp/jinja-type-probe.yml
ansible-config dump | grep JINJA2_NATIVE
```

Interpret:

| `as_join` type | Likely cause |
|---|---|
| `str` → `2,3,10` | native off, or task-level template |
| `tuple` → `(2, 3, 10)` | **native on** + group_vars/play vars |
| `str` with quotes/brackets | native off, list never really a list — use `to_json`/`from_json` |

### Recommended portable `ansible.cfg` snippet for matrix repos

Minimal explicit settings so home and work don't diverge silently:

```ini
[defaults]
# Pin typing behaviour — do not rely on core-version defaults
jinja2_native = True

# Or, if the enterprise standard is classic strings:
# jinja2_native = False

# Collections for T6/T7/T13 (adjust paths to the job)
collections_path = ./collections:~/.ansible/collections:/usr/share/ansible/collections

# Optional: make debug dumps paste-friendly
callback_result_format = yaml
```

**Policy that travels well:**

1. Set `jinja2_native` **explicitly** in every repo's `ansible.cfg`.  
2. Use **T4 `to_json` / `from_json`** for any multi-step derived list shared across sites.  
3. Keep **lists** in group_vars; `join(',')` only in **tasks** (or use `join('|')` if a var must be a scalar under native).  
4. Pin `community.general` + document `jmespath` on the controller.  
5. Re-run the type probe when someone upgrades core or copies cfg from another project.

---

## Trick picker (match the shape you see)

| I see BEFORE… | I want AFTER… | Trick |
|---|---|---|
| Dict; I know key `mgt` | One gateway / portgroup string | **T0** |
| Dict of segments | List I can `loop:` | **T1** |
| Dict with `admin: true` on some rows | List of those subnets | **T2** |
| Dict or list of rows | Just the `vlan_id` column | **T3** |
| Dict with optional keys | Flat list + defaults + bools | **T4** |
| Flat list of rows | Only `site == site_a` | **T5** |
| Flat list | Filter + rename in one expression | **T6** |
| Flat list | Filter in Jinja, then rename | **T7** |
| Flat list | Build `eth1.10` / counters | **T8** |
| Flat list | Map `mgt → 10` | **T9** |
| Dynamic list + ~10 weird hand rows | One role list | **T10** |
| Flat list | Nested loop by site | **T11** |
| List of ids / dicts | Sorted unique ids (CSV at task) | **T12** |
| Flat list | List of duplicate vlan_ids | **T13** |
| Optional keys missing | Filled display names | **T14** |
| Flat list with `role:` | All svc CIDRs | **T15** |
| Dict of dicts | Loop/display sorted by nested field (e.g. vlan_id) | **T16** |

---

## T0 — Direct lookup

**Recognise:** dict keyed by short name; you already know the key.

### BEFORE
```yaml
fabric_underlays:                    # dict
  mgt:
    gateway: "192.168.10.1"
    portgroup: "VLAN10-MGT-SVC"
    dns: ["192.168.10.1"]
  prod:
    gateway: "192.168.30.1"
    portgroup: "VLAN30-PROD-SVC"
```

### Jinja
```yaml
network_gateway:    "{{ fabric_underlays.mgt.gateway }}"
guest_portgroup: "{{ fabric_underlays.mgt.portgroup }}"
# inventory sets network_segment: prod
# network_gateway: "{{ fabric_underlays[network_segment].gateway }}"
```

### AFTER
```yaml
network_gateway: "192.168.10.1"           # string
guest_portgroup: "VLAN10-MGT-SVC"      # string
```

**When:** inventory / host_vars selection. Most plays stop here.

---

## T1 — `dict2items` / keys

**Recognise:** dict-of-dicts; roles need a **list** to loop.

### BEFORE
```yaml
fabric_underlays:                    # dict
  mgt:     { vlan_id: 10, … }
  prod:    { vlan_id: 30, … }
  infra:   { vlan_id: 0,  … }
  site_b_lab: { vlan_id: 100,… }
```

### Jinja
```yaml
fabric_underlay_keys: "{{ fabric_underlays.keys() | list }}"
fabric_underlay_items_raw: "{{ fabric_underlays | dict2items }}"
```

### AFTER
```yaml
fabric_underlay_keys:                # list of strings
  - mgt
  - prod
  - infra
  - site_b_lab

fabric_underlay_items_raw:           # list of {key, value}
  - key: mgt
    value:
      vlan_id: 10
      portgroup: "VLAN10-MGT-SVC"
      subnet: "192.168.10.0/24"
      # … entire original body under .value …
  - key: prod
    value:
      vlan_id: 30
      # …
  - key: infra
    value: { … }
  - key: site_b_lab
    value: { … }
```

```text
BEFORE:  { mgt: {…}, prod: {…} }      dict
AFTER:   [ {key, value}, … ]          list  (loopable)
```

---

## T2 — `selectattr` on raw `dict2items`

**Recognise:** only some dict rows have a flag; you want **one field** from those rows.

### BEFORE
```yaml
fabric_underlays:
  mgt:     { admin: true,  subnet: "192.168.10.0/24", … }
  prod:    {               subnet: "192.168.30.0/24", … }   # no admin
  infra:   { admin: true,  subnet: "192.168.0.0/24",  … }
  site_b_lab: {               subnet: "192.168.100.0/24", … }
```

### Jinja
```yaml
fabric_admin_cidrs: >-
  {{ fabric_underlays | dict2items
     | selectattr('value.admin', 'defined')
     | selectattr('value.admin')
     | map(attribute='value.subnet')
     | list }}
```

### AFTER
```yaml
fabric_admin_cidrs:         # list of strings
  - "192.168.10.0/24"                 # mgt
  - "192.168.0.0/24"                  # infra
# prod + site_b_lab gone
```

```text
BEFORE:  dict with optional admin: true
AFTER:   ["cidr", "cidr"]             only flagged rows
```

**Gotcha:** use `value.admin` after dict2items — not bare `admin`.

---

## T3 — `map(attribute=…)` one column

**Recognise:** many rows; you only need **one column**.

### BEFORE
```yaml
fabric_underlays:
  mgt:     { vlan_id: 10, subnet: "192.168.10.0/24" }
  prod:    { vlan_id: 30, subnet: "192.168.30.0/24" }
  infra:   { vlan_id: 0,  subnet: "192.168.0.0/24" }
  site_b_lab: { vlan_id: 100, subnet: "192.168.100.0/24" }
```

### Jinja
```yaml
fabric_underlay_vlan_ids: >-
  {{ fabric_underlays | dict2items
     | map(attribute='value.vlan_id')
     | list | unique | sort }}
```

### AFTER
```yaml
fabric_underlay_vlan_ids:            # list of ints
  - 0
  - 10
  - 30
  - 100
```

```text
BEFORE:  rows with many fields
AFTER:   [0, 10, 30, 100]             just that column
```

---

## T4 — for-loop enrich (`to_json` / `from_json`)

**Recognise:** optional keys; later filters need **flat** rows with defaults + bools.

### BEFORE
```yaml
fabric_underlays:
  mgt:
    vlan_id: 10
    portgroup: "VLAN10-MGT-SVC"
    subnet: "192.168.10.0/24"
    gateway: "192.168.10.1"
    site: site_a
    fw_parent: eth1
    fw_descr: "V10_MGT_SVC"
  infra:
    vlan_id: 0
    portgroup: "VLAN00-INFRA"
    subnet: "192.168.0.0/24"
    gateway: "192.168.0.1"
    site: site_a
    # missing: fw_parent, fw_descr
```

### Jinja
```yaml
fabric_underlay_items: |
  {% set items = [] %}
  {% for key, net in fabric_underlays.items() %}
  {%   set prefixlen = (net.subnet.split('/')[1] | int)
         if (net.subnet is defined and '/' in (net.subnet | string))
         else 24 %}
  {%   set _ = items.append({
         'key': key,
         'vlan_id': net.vlan_id | int,
         'portgroup': net.portgroup,
         'subnet': net.subnet,
         'gateway': net.gateway,
         'gateway_cidr': (net.gateway ~ '/' ~ (prefixlen | string))
            if (net.fw_parent is defined and (net.vlan_id | int) > 0)
            else '',
         'site': net.site,
         'fw_parent': net.fw_parent | default(''),
         'fw_descr': net.fw_descr | default(net.portgroup, true),
         'prefixlen': prefixlen,
         'tagged': (net.vlan_id | int) > 0,
         'has_fw': net.fw_parent is defined
       }) %}
  {% endfor %}
  {{ items | to_json }}

# always:  {{ fabric_underlay_items | from_json | selectattr(...) }}
```

Line-by-line walk-through of this block → **[Appendix A](#appendix-a--annotated-t4-enrich-loop)**.

### AFTER (after `| from_json`)
```yaml
- key: mgt
  vlan_id: 10
  portgroup: "VLAN10-MGT-SVC"
  subnet: "192.168.10.0/24"
  gateway: "192.168.10.1"
  gateway_cidr: "192.168.10.1/24"     # NEW — edge-FW / L3 shape
  site: site_a
  fw_parent: eth1
  fw_descr: "V10_MGT_SVC"
  prefixlen: 24
  tagged: true                         # NEW
  has_fw: true                    # NEW

- key: infra
  vlan_id: 0
  portgroup: "VLAN00-INFRA"
  subnet: "192.168.0.0/24"
  gateway: "192.168.0.1"
  gateway_cidr: ""
  site: site_a
  fw_parent: ""
  fw_descr: "VLAN00-INFRA"        # defaulted from portgroup
  prefixlen: 24
  tagged: false
  has_fw: false
```

```text
BEFORE:  dict, sparse optional keys
AFTER:   list of flat dicts, every row same schema + flags
```

---

## T5 — `selectattr` / `rejectattr` slices

**Recognise:** you already have T4's flat list; you want a **subset**.

### BEFORE
```yaml
# fabric_underlay_items | from_json
- { key: mgt,     site: site_a, tagged: true,  has_fw: true }
- { key: prod,    site: site_a, tagged: true,  has_fw: true }
- { key: infra,   site: site_a, tagged: false, has_fw: false }
- { key: site_b_lab, site: site_b, tagged: true,  has_fw: true }
```

### Jinja
```yaml
fabric_underlays_site_a: >-
  {{ fabric_underlay_items | from_json
     | selectattr('site', 'equalto', 'site_a') | list }}

fabric_underlays_tagged: >-
  {{ fabric_underlay_items | from_json | selectattr('tagged') | list }}
```

### AFTER (`…_site_a`)
```yaml
- { key: mgt,   site: site_a, … }
- { key: prod,  site: site_a, … }
- { key: infra, site: site_a, … }
# site_b_lab removed
```

### AFTER (`…_tagged`)
```yaml
- { key: mgt,     tagged: true, … }
- { key: prod,    tagged: true, … }
- { key: site_b_lab, tagged: true, … }
# infra removed (vlan 0)
```

```text
BEFORE:  [ row, row, row, row ]
AFTER:   [ row, row ]              fewer rows, same shape
```

---

## T6 — whole-list `json_query` (filter + rename)

**Recognise:** flat rich rows; platform wants **different field names** and only some rows.

### BEFORE
```yaml
- key: mgt
  vlan_id: 10
  wifi_name: "V10_MGT_SVC"
  wifi_site: a
  has_wifi: true
  switch_name: "MGT-SVC"
  on_switch: true
- key: site_b_lab
  vlan_id: 100
  wifi_name: "VLAN100"
  wifi_site: b
  has_wifi: true
  switch_name: "CBR"
  on_switch: false
- key: infra
  vlan_id: 0
  has_wifi: false
  switch_name: "INFRA"
  on_switch: true
```

### Jinja
```yaml
network_wifi_lid_vlans: >-
  {{ fabric_underlay_items | from_json
     | json_query('[?has_wifi && wifi_site==`a`].{name: wifi_name, vlan_id: vlan_id, purpose: `vlan-only`}') }}

network_switch_vlans: >-
  {{ fabric_underlay_items | from_json
     | json_query('[?on_switch].{id: vlan_id, name: switch_name}') }}
```

### AFTER (Wi-Fi controller lid)
```yaml
- name: "V10_MGT_SVC"       # renamed from wifi_name
  vlan_id: 10
  purpose: "vlan-only"      # constant
# only lid; cbr + infra gone
```

### AFTER (switch)
```yaml
- id: 10
  name: "MGT-SVC"
- id: 0
  name: "INFRA"
# site_b_lab gone (on_switch: false)
```

```text
BEFORE:  [{wifi_name, wifi_site, vlan_id, …}, …]
AFTER:   [{name, vlan_id, purpose}, …]     fewer fields + fewer rows
```

**Needs:** `jmespath` on controller.

---

## T7 — Jinja filter then `map`+`json_query` rename

**Recognise:** same goal as T6; you prefer **readable selectattr** then a small rename.

### BEFORE
```yaml
- { key: mgt,  site: site_a, has_fw: true, tagged: true,
    fw_parent: eth1, vlan_id: 10, fw_descr: "V10_MGT_SVC" }
- { key: site_b_lab, site: site_b, has_fw: true, tagged: true,
    fw_parent: eth1, vlan_id: 100, fw_descr: "VLAN100" }
```

### Jinja
```yaml
network_fw_site_a_vlans: >-
  {{ fabric_underlay_items | from_json
     | selectattr('has_fw')
     | selectattr('site', 'equalto', 'site_a')
     | selectattr('tagged')
     | map('community.general.json_query',
           '{interface: fw_parent, vlan_id: vlan_id, descr: fw_descr}')
     | list }}
```

### AFTER
```yaml
- interface: eth1                 # was fw_parent
  vlan_id: 10
  descr: "V10_MGT_SVC"           # was fw_descr
# site_b_lab removed by site filter
```

```text
BEFORE:  fw_parent / fw_descr / site / flags
AFTER:   interface / vlan_id / descr          (module-shaped)
```

**Needs:** `community.general`.

---

## T8 — for-loop (computed strings / counters)

**Recognise:** output needs **string build** (`eth1.10`) not a field rename.

### BEFORE
```yaml
- key: mgt
  has_fw: true
  site: site_a
  tagged: true
  fw_parent: eth1
  vlan_id: 10
  fw_descr: "V10_MGT_SVC"
  gateway: "192.168.10.1"
  prefixlen: 24
- key: prod
  has_fw: true
  site: site_a
  tagged: true
  fw_parent: eth1
  vlan_id: 30
  fw_descr: "V30_PRD_SVC"
  gateway: "192.168.30.1"
  prefixlen: 24
```

### Jinja
```yaml
_network_fw_site_a_interfaces_json: |
  {% set items = [] %}
  {% for it in fabric_underlay_items | from_json
       if it.has_fw and it.site == 'site_a' and it.tagged %}
  {%   set _ = items.append({
         'descr': it.fw_descr,
         'interface': it.fw_parent ~ '.' ~ (it.vlan_id | string),
         'ipv4_address': it.gateway,
         'ipv4_prefixlen': it.prefixlen
       }) %}
  {% endfor %}
  {{ items | to_json }}

network_fw_site_a_interfaces: "{{ _network_fw_site_a_interfaces_json | from_json }}"
```

### AFTER
```yaml
- descr: "V10_MGT_SVC"
  interface: "eth1.10"            # COMPUTED
  ipv4_address: "192.168.10.1"
  ipv4_prefixlen: 24
- descr: "V30_PRD_SVC"
  interface: "eth1.30"
  ipv4_address: "192.168.30.1"
  ipv4_prefixlen: 24
```

```text
BEFORE:  parent=eth1, vlan_id=10  (separate fields)
AFTER:   interface="eth1.10"      (one string the module wants)
```

---

## T9 — `items2dict` lookup map

**Recognise:** flat list; you want `map[key]` random access.

### BEFORE
```yaml
- { key: mgt,     vlan_id: 10 }
- { key: prod,    vlan_id: 30 }
- { key: infra,   vlan_id: 0 }
- { key: site_b_lab, vlan_id: 100 }
```

### Jinja
```yaml
fabric_underlay_vlan_by_key: >-
  {{ fabric_underlay_items | from_json
     | items2dict(key_name='key', value_name='vlan_id') }}
```

### AFTER
```yaml
fabric_underlay_vlan_by_key:         # dict, not list
  mgt: 10
  prod: 30
  infra: 0
  site_b_lab: 100

# "{{ fabric_underlay_vlan_by_key['mgt'] }}"  →  10
```

```text
BEFORE:  [ {key, vlan_id}, … ]        list
AFTER:   { mgt: 10, prod: 30, … }     map
```

---

## T10 — `dynamic + custom` concat

**Recognise:** 90% generable; ~10 hand rows with **no pattern**.

### BEFORE
```yaml
hypervisor_port_groups_dynamic:             # from matrix
  - { name: "VLAN10-MGT-SVC",  vlan_id: 10 }
  - { name: "VLAN30-PROD-SVC", vlan_id: 30 }
  - { name: "VLAN00-INFRA",    vlan_id: 0 }

hypervisor_port_groups_custom:              # the weird tail from the old hand list
  - { name: TRUNK, vlan_id: "0-4094", trunk: true }
  - { name: "VLAN501-SPECIAL-LAB", vlan_id: 501 }
  - { name: "VLAN40", vlan_id: 40, state: absent }
```

### Jinja
```yaml
hypervisor_port_groups: "{{ hypervisor_port_groups_dynamic + hypervisor_port_groups_custom }}"
# role still:  loop: "{{ hypervisor_port_groups }}"
```

### AFTER
```yaml
hypervisor_port_groups:
  - { name: "VLAN10-MGT-SVC",  vlan_id: 10 }
  - { name: "VLAN30-PROD-SVC", vlan_id: 30 }
  - { name: "VLAN00-INFRA",    vlan_id: 0 }
  - { name: TRUNK, vlan_id: "0-4094", trunk: true }
  - { name: "VLAN501-SPECIAL-LAB", vlan_id: 501 }
  - { name: "VLAN40", vlan_id: 40, state: absent }
```

```text
BEFORE:  dynamic list  +  custom list     (two piles)
AFTER:   one list the role already loops
```

| Not this | Why |
|---|---|
| `combine` | That's for **maps**, not portgroup lists |
| `unique` on dicts | Doesn't merge “same name” objects |
| Stuff custom into SSOT | Pollutes the matrix with one-offs |

---

## T11 — `groupby`

**Recognise:** flat list; nested loop “for each site, for each VLAN”.

### BEFORE
```yaml
- { key: mgt,     site: site_a }
- { key: prod,    site: site_a }
- { key: infra,   site: site_a }
- { key: site_b_lab, site: site_b }
```

### Jinja
```yaml
fabric_underlays_by_site: "{{ fabric_underlay_items | from_json | groupby('site') }}"
```

### AFTER
```yaml
# list of (group_name, [members…]) pairs
- - site_b
  - - { key: site_b_lab, site: site_b, … }
- - site_a
  - - { key: mgt,   site: site_a, … }
    - { key: prod,  site: site_a, … }
    - { key: infra, site: site_a, … }

# loop item.0 = "site_a"
# loop item.1 = [ segment, segment, … ]
```

```text
BEFORE:  flat list mixed sites
AFTER:   buckets keyed by site
```

---

## T12 — `unique` / `sort` / task-time `join`

**Recognise:** list of vlan objects; switch wants sorted ids (CSV only when printing CLI).

### BEFORE
```yaml
network_switch_vlans:
  - { id: 10,  name: "MGT-SVC" }
  - { id: 30,  name: "PROD-SVC" }
  - { id: 0,   name: "INFRA" }
  - { id: 100, name: "CBR" }
  - { id: 10,  name: "MGT-DUP" }      # accidental duplicate id
```

### Jinja
```yaml
network_switch_vlan_ids_tagged: >-
  {{ network_switch_vlans
     | selectattr('id', 'gt', 0)
     | map(attribute='id')
     | list | unique | sort }}

network_switch_allowed_vlans: "{{ network_switch_vlan_ids_tagged }}"   # keep LIST in vars

# In a TASK / shell module only:
#   "{{ network_switch_allowed_vlans | join(',') }}"
```

### AFTER (group_vars)
```yaml
network_switch_vlan_ids_tagged:
  - 10
  - 30
  - 100
# 0 removed; duplicate 10 collapsed
```

### AFTER (task join)
```text
10,30,100
```

```text
BEFORE:  [{id, name}, …]
AFTER:   [10, 30, 100]     then optional "10,30,100" at task time
```

**Footgun:** with `jinja2_native=True`, putting `| join(',')` **in group_vars** turns
`"2,3,10"` into tuple `(2, 3, 10)`. Join in tasks instead. See version section.

---

## T13 — duplicate detection

**Recognise:** you want assert “no duplicate vlan_ids” before applying config.

### BEFORE
```yaml
- { key: mgt,  vlan_id: 10, portgroup: "VLAN10-MGT-SVC" }
- { key: prod, vlan_id: 30, portgroup: "VLAN30-PROD-SVC" }
- { key: oops, vlan_id: 10, portgroup: "VLAN10-MGT-SVC" }   # dup
```

### Jinja
```yaml
fabric_duplicate_vlans: >-
  {{ fabric_underlay_items | from_json
     | selectattr('tagged')
     | map(attribute='vlan_id')
     | community.general.counter
     | dict2items
     | selectattr('value', '>', 1)
     | map(attribute='key')
     | list }}
```

### AFTER
```yaml
fabric_duplicate_vlans:
  - 10                    # appeared more than once
# [] means clean
```

```text
BEFORE:  rows (maybe with accidental dups)
AFTER:   [10, …]  or  []
```

---

## T14 — nested `default()` (usually inside T4)

**Recognise:** optional name fields; need one display string without undefined.

### BEFORE
```yaml
mgt:
  portgroup: "VLAN10-MGT-SVC"
  fw_descr: "V10_MGT_SVC"
  wifi_name: "V10_MGT_SVC"       # all three present

tenants:
  portgroup: "VLAN50-DEVLAB"
  fw_descr: "V50_DEVLAB"
  # no wifi_name

infra:
  portgroup: "VLAN00-INFRA"
  # no fw_descr, no wifi_name
```

### Jinja
```yaml
wifi_name: "{{ net.wifi_name
  | default(net.fw_descr | default(net.portgroup, true), true) }}"

switch_name: "{{ net.switch_name
  | default(net.portgroup | regex_replace('^VLAN[0-9]+-', ''), true) }}"
```

### AFTER
```yaml
# wifi_name resolves to:
mgt:     "V10_MGT_SVC"      # from wifi_name
tenants: "V50_DEVLAB"       # fell back to fw_descr
infra:   "VLAN00-INFRA"     # fell back to portgroup

# switch_name from portgroup strip:
mgt: "MGT-SVC"              # VLAN10- removed
```

```text
BEFORE:  sparse optional name keys
AFTER:   one guaranteed string per row
```

---

## T15 — role plane → CIDR list

**Recognise:** rows have `role:`; firewall alias wants all svc subnets.

### BEFORE
```yaml
- { key: mgt,   role: svc,     subnet: "192.168.10.0/24" }
- { key: prod,  role: svc,     subnet: "192.168.30.0/24" }
- { key: cluster,   role: cluster, subnet: "192.168.11.0/24" }
- { key: infra, role: infra,   subnet: "192.168.0.0/24" }
```

### Jinja
```yaml
network_cidrs_svc: >-
  {{ fabric_underlay_items | from_json
     | selectattr('role', 'equalto', 'svc')
     | map(attribute='subnet')
     | list }}
```

### AFTER
```yaml
network_cidrs_svc:
  - "192.168.10.0/24"
  - "192.168.30.0/24"
# cluster + infra gone
```

```text
BEFORE:  mixed roles
AFTER:   ["svc cidr", "svc cidr"]
```

---

## T16 — sort dict-of-dicts by a 2nd-level field

**Recognise:** SSOT is a **dict** keyed by name (`mgt`, `prod`, …) but you want to
**display or loop in vlan_id order** (or any nested field), not key order.

Dicts are not sorted by nested attributes in Jinja — flatten first, then sort.

### BEFORE
```yaml
fabric_underlays:                    # dict — insertion order ≠ vlan order
  prod:  { vlan_id: 30, site: site_a, portgroup: "VLAN30-PROD-SVC" }
  mgt:   { vlan_id: 10, site: site_a, portgroup: "VLAN10-MGT-SVC" }
  infra: { vlan_id: 0,  site: site_a, portgroup: "VLAN00-INFRA" }
  site_b_lab: { vlan_id: 100, site: site_b, portgroup: "VLAN100-SITEB" }
```

### Jinja
```yaml
# Sort by nested field on each value
fabric_underlays_by_vlan: >-
  {{ fabric_underlays | dict2items
     | sort(attribute='value.vlan_id')
     | list }}

# Descending
fabric_underlays_by_vlan_desc: >-
  {{ fabric_underlays | dict2items
     | sort(attribute='value.vlan_id', reverse=true)
     | list }}

# Already flat (T4)? sort the list directly:
# {{ fabric_underlay_items | from_json | sort(attribute='vlan_id') }}
```

### AFTER
```yaml
fabric_underlays_by_vlan:            # list of {key, value} — vlan order
  - key: infra
    value: { vlan_id: 0, site: site_a, portgroup: "VLAN00-INFRA" }
  - key: mgt
    value: { vlan_id: 10, site: site_a, portgroup: "VLAN10-MGT-SVC" }
  - key: prod
    value: { vlan_id: 30, site: site_a, portgroup: "VLAN30-PROD-SVC" }
  - key: site_b_lab
    value: { vlan_id: 100, site: site_b, portgroup: "VLAN100-SITEB" }
```

### Display / loop
```yaml
- name: Show segments in VLAN number order
  ansible.builtin.debug:
    msg: "{{ item.key }} → vlan {{ item.value.vlan_id }} ({{ item.value.portgroup }})"
  loop: "{{ fabric_underlays_by_vlan }}"
  loop_control:
    label: "{{ '%04d' | format(item.value.vlan_id | int) }} {{ item.key }}"
```

```text
BEFORE:  { prod: {vlan_id: 30}, mgt: {vlan_id: 10}, … }   dict (name keys)
AFTER:   [ {key, value}, … ] sorted by value.vlan_id         list (display order)
```

### Values only (drop the key wrapper)
```yaml
segments_by_vlan: >-
  {{ fabric_underlays | dict2items
     | sort(attribute='value.vlan_id')
     | map(attribute='value')
     | list }}
# → [ {vlan_id: 0, …}, {vlan_id: 10, …}, … ]
```

### Gotchas

| Issue | Fix |
|---|---|
| Sorting the dict itself | Don’t — always `dict2items` first |
| `"100"` before `"20"` (string sort) | Force int in T4, or enrich then `sort(attribute='vlan_id')` on flat rows |
| Multi-field sort (vlan then name) | Build a composite `sort_key` (see below) |

### Optional: multi-key sort (vlan, then segment name)
```yaml
fabric_underlays_by_vlan_name: |
  {% set out = [] %}
  {% for k, v in fabric_underlays.items() %}
  {%   set _ = out.append(
         v | combine({
           'key': k,
           'sort_key': '%04d-%s' | format(v.vlan_id | int, k)
         })
       ) %}
  {% endfor %}
  {{ out | sort(attribute='sort_key') | to_json }}
# use:  {{ fabric_underlays_by_vlan_name | from_json }}
```

**When:** inventory dumps, docs generation, “print the fabric in VLAN order”,
reviewing SSOT before a change.

---

## End-to-end: work hand-list → dynamic + custom

### BEFORE (typical work — fully hand-curated)
```yaml
hypervisor_port_groups:
  - { name: "VLAN10-MGT-SVC", vlan_id: 10, switch: vDS-Core }
  - { name: "VLAN30-PROD-SVC", vlan_id: 30, switch: vDS-Core }
  - { name: "VLAN00-INFRA", vlan_id: 0, switch: vDS-Core }
  - { name: TRUNK, vlan_id: "0-4094", switch: vDS-Core, trunk: true }
  - { name: "VLAN501-SPECIAL-LAB", vlan_id: 501, switch: vDS-Core }
  # …eight more one-offs with no pattern…
```

### AFTER (T4/T7 for 90% + T10 for the tail)
```yaml
hypervisor_port_groups_dynamic: >-
  {{ fabric_underlay_items | from_json
     | selectattr('virt')
     | map('community.general.json_query',
           '{name: portgroup, vlan_id: vlan_id, switch: `vDS-Core`}')
     | list }}

hypervisor_port_groups_custom:
  - { name: TRUNK, vlan_id: "0-4094", switch: vDS-Core, trunk: true }
  - { name: "VLAN501-SPECIAL-LAB", vlan_id: 501, switch: vDS-Core }

hypervisor_port_groups: "{{ hypervisor_port_groups_dynamic + hypervisor_port_groups_custom }}"
```

Role still: `loop: "{{ hypervisor_port_groups }}"` — only the source of the list changed.

---

## Controller deps

| Trick | Needs |
|---|---|
| T0–T5, T8–T12, T14–T16 | ansible-core only |
| T6 `json_query` | `pip: jmespath` |
| T7 / T13 | `collections: community.general` |
| optional `ipaddr` | `pip: netaddr` + `collections: ansible.utils` |

---

## Platform mapping (example consumers)

| Consumer | Vars | Trick(s) |
|---|---|---|
| Inventory host net | `fabric_underlays.<seg>.*` | T0 |
| Admin / bastion sources | `fabric_admin_cidrs` | T2 |
| fw-site-a-01 | `network_fw_site_a_*` | T7 / T8 |
| fw-site-b-01 | `network_fw_site_b_*` | T8 |
| Wi-Fi controller | `network_wifi_*_vlans` | T6 + T10 |
| edge-FW PoC | `network_edgefw_*` | T8 |
| leaf switch OS | `network_switch_*` | T6 + T12 |
| hypervisor / virt-mgmt | `network_virt_portgroups` | T7 + T10 |
| Preflight | `fabric_duplicate_*` | T13 |

---

## Recipe: add a consumer

1. Find the BEFORE that looks like your data.
2. Note the AFTER shape your role's `loop:` expects.
3. Copy that T* Jinja; rename fields.
4. Wire host_vars → new list; leave the role loop alone.

---

## Recipe: take this to another job

1. Copy this cheatsheet.
2. Run the type probe + `ansible-config dump | grep JINJA2_NATIVE`.
3. Pin `jinja2_native` explicitly in their `ansible.cfg`.
4. Drop in their matrix as BEFORE; build only the AFTER lists they need.
5. Install `jmespath` / `community.general` if using T6/T7/T13.

On **2.16 with native off**, keep `to_json`/`from_json` even if it feels redundant.

---

## Anti-patterns

| Don't | Do |
|---|---|
| Re-list the same VLAN in three host_vars | One matrix → three AFTER lists |
| `set_fact` the whole catalog every play | Lazy group_vars projections |
| One mega for-loop for every platform | Separate T* with clear AFTER shapes |
| `combine` on two portgroup **lists** | T10 `dynamic + custom` |
| `unique` hoping dicts merge by name | Dedupe by key only if you must |
| JMESPath string-concat hell | T8 for `parent.vlan` |

---

## Quick debug

```bash
cd ansible/
export ANSIBLE_VAULT_PASSWORD=$(cat /path/to/vault-password)
source source /path/to/ansible-venv/bin/activate

ansible-inventory -i inventories/lab/hosts.yml --host fw-site-a-01 \
  | jq '.network_fw_site_a_vlans'

ansible localhost -m debug \
  -e @playbooks/group_vars/all/networks.yml \
  -a 'var=network_switch_vlans'
```

If you get a JSON **string** instead of a list, you forgot `| from_json` on a T4/T8
intermediate (or you're reading the private `_*_json` var).

---

## Appendix A — annotated T4 enrich loop

Deep dive for the multi-line Jinja in **T4**. The main T0–T15 section stays
BEFORE/AFTER only; this is the line-by-line when you need it.

### Comment rules

| Syntax | Where | Notes |
|---|---|---|
| `{# … #}` | Inside the `\|` template, **own line only** | Stripped by Jinja; never part of the value |
| `# …` | Outside the template (normal YAML) | Documents the var for humans |
| `{# … #}` **inside** `append({ … })` | **Illegal** | `unexpected char '#'` — dict is an expression |
| Nested `{# … {# … #} … #}` | **Illegal** | Inner `#}` closes the outer comment early |

### Annotated template

```yaml
# YAML multi-line string = whole Jinja program (templated lazily on reference).
fabric_underlay_items: |

  {# ── 1. Empty accumulator ───────────────────────────────────────────── #}
  {# `items` will hold one flat dict per SSOT row.                         #}
  {% set items = [] %}

  {# ── 2. Walk every SSOT row ─────────────────────────────────────────── #}
  {# .items() → (key, net) pairs                                          #}
  {#   key = "mgt" | "prod" | …                                           #}
  {#   net = { vlan_id, subnet, gateway, … }                              #}
  {% for key, net in fabric_underlays.items() %}

  {# ── 3. Derive prefix length from CIDR ──────────────────────────────── #}
  {# "192.168.10.0/24" → split("/") → index [1] → 24                      #}
  {# Guard: only if subnet exists and contains "/".                       #}
  {# Fallback 24 if bare address (shouldn't happen).                      #}
  {# Multi-line {% set %} is ONE statement (if/else is Jinja, not YAML).  #}
  {%   set prefixlen = (net.subnet.split('/')[1] | int)
         if (net.subnet is defined and '/' in (net.subnet | string))
         else 24 %}

  {# ── 4. Append one normalised row ───────────────────────────────────── #}
  {# Fixed schema so later tricks use flat keys:                          #}
  {#   selectattr('tagged')  not  selectattr('value.something')           #}
  {#                                                                      #}
  {# append() returns None — assign to `_` or the statement is invalid.   #}
  {# Field meanings live in the table below (not between dict keys).      #}
  {%   set _ = items.append({
         'key': key,
         'vlan_id': net.vlan_id | int,
         'portgroup': net.portgroup,
         'subnet': net.subnet,
         'gateway': net.gateway,
         'gateway_cidr': (net.gateway ~ '/' ~ (prefixlen | string))
            if (net.fw_parent is defined and (net.vlan_id | int) > 0)
            else '',
         'site': net.site,
         'fw_parent': net.fw_parent | default(''),
         'fw_descr': net.fw_descr | default(net.portgroup, true),
         'prefixlen': prefixlen,
         'tagged': (net.vlan_id | int) > 0,
         'has_fw': net.fw_parent is defined
       }) %}
  {% endfor %}

  {# ── 5. Emit JSON text (not a raw Python list) ──────────────────────── #}
  {# Consumers MUST:  fabric_underlay_items | from_json | …              #}
  {# Portable on 2.16 (jinja2_native off) and 2.18 (on).                  #}
  {{ items | to_json }}

# Outside the template:
#   {{ fabric_underlay_items | from_json | selectattr('tagged') | list }}
```

### Reading order

| Step | Code | Job |
|---|---|---|
| 1 | `set items = []` | Empty list accumulator |
| 2 | `for key, net in …` | One pass over the matrix |
| 3 | `set prefixlen = …` | Parse `/24` from subnet CIDR |
| 4 | `items.append({…})` | One flat output row per input row |
| 5 | `{{ items \| to_json }}` | Freeze list as JSON text for later |

### Output field cheat-card

| AFTER key | Comes from | Why |
|---|---|---|
| `key` | loop variable | Don't lose segment name |
| `vlan_id` | `net.vlan_id \| int` | Stable type for filters |
| `portgroup`, `subnet`, `gateway`, `site` | SSOT pass-through | Identity / L3 |
| `gateway_cidr` | `gateway ~ '/' ~ prefixlen` | edge-FW / combined form; only if FW parent + tagged; else `""` |
| `fw_parent` | SSOT or `''` | Always defined (no undefined later) |
| `fw_descr` | SSOT or `portgroup` | `default(..., true)` also replaces empty string |
| `prefixlen` | parsed from subnet | Reuse in T8 interfaces |
| `tagged` | `vlan_id > 0` | Filter flag for 802.1Q rows |
| `has_fw` | `fw_parent is defined` | Filter flag for FW VLAN/SVI rows |

### Full-estate enrich (this repo)

A fuller estate matrix may also set `wifi_*`, `switch_name`, `virt` /
`on_switch` / `edgefw`, `admin`, etc. Same structure as above — more keys in the
`append({…})` dict, same five steps.

---

## Appendix B — more annotated examples (placeholder)

Add further deep dives here when a multi-line pattern is hard to read in T0–T15
(e.g. T8 interface build, T13 counter chain). Keep the main tricks lean:
**BEFORE → Jinja → AFTER** only.
