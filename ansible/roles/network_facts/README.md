# network_facts

## TL;DR

Prints what the network catalog holds — one small, liftable task per fact. It is
both the helper you run to see the numbers and the worked example you copy tasks
out of. It configures nothing.

    ansible-playbook -i inventories/example playbooks/ops_net_facts.yml

## Requirements

None beyond `ansible.builtin`.

## Key variables

Full list: `defaults/main.yml`. Contract: `meta/argument_specs.yml`.

Nothing is required — the role runs with no configuration and shows everything.
The variables below *narrow* what you get.

| Req | Variable | Default | Purpose |
|---|---|---|---|
| Optional | `network_facts_show` | `[]` | Which recipe groups run. Empty runs all. Same effect as `--tags` |
| Optional | `network_facts_platform` | `""` | A platform label or view namespace — wakes the per-platform recipes |
| Optional | `network_facts_list` | `""` | A list inside that platform, e.g. `port_groups` |
| Optional | `network_facts_site` | `""` | Which site out of a per-site list. Empty takes the first |
| Optional | `network_facts_key` | `""` | One segment key |
| Optional | `network_facts_group` | `""` | A partition field to slice by — `site`, `zone`, `role`, `env`, `tenancy`, `pool`, `platform` |
| Optional | `network_facts_value` | `""` | The value of `network_facts_group` to select |
| Optional | `network_facts_save` | `""` | Controller-side path to write the selection to. Empty writes nothing |
| Optional | `network_facts_save_format` | `""` | `yaml` or `json`. Empty follows the file extension |

## Usage

    - name: Network catalog facts
      hosts: localhost
      gather_facts: false
      connection: local
      roles:
        - role: network_facts

Run:

    export ANSIBLE_VAULT_PASSWORD=$(cat ~/secrets/vault-password.txt)
    ansible-playbook -i inventories/example playbooks/ops_net_facts.yml

Narrow it to one recipe group, then narrow that with a knob:

    ansible-playbook -i inventories/example playbooks/ops_net_facts.yml --tags vlans
    ansible-playbook -i inventories/example playbooks/ops_net_facts.yml --tags vlans \
      -e network_facts_platform=esxi

Take a list away with you:

    ansible-playbook -i inventories/example playbooks/ops_net_facts.yml \
      -e network_facts_platform=esxi \
      -e network_facts_list=vcenter_portgroups \
      -e network_facts_save=/tmp/portgroups.yml

## How it is laid out

Each file in `tasks/` is one recipe group, and each task in it is one fact:

| File | What it answers |
|---|---|
| `segments.yml` | What networks exist, and what fields each carries |
| `vlans.yml` | Which VLAN ids exist, who carries them, as trunk ranges |
| `subnets.yml` | Which CIDRs to point a firewall rule at |
| `platform_lists.yml` | The rows a module loops over, already shaped |
| `lookups.yml` | Key → vlan / name / subnet / gateway |
| `validate.yml` | Is the catalog well-formed |
| `save.yml` | Write the selection out as a file |

Every `when:` that decides *which group runs* lives in `tasks/main.yml`, never in
a recipe file. That is deliberate: it keeps each recipe down to a name and a
one-line expression, so you can lift one straight out. The only conditions inside
a recipe file sit under its `NARROWED` divider, one per task, naming the knob
that feeds it — delete that line and hardcode your value when you copy it.

## Adding or changing a recipe

To change what a task pulls, edit the expression on its `var:` line — that is the
whole task. To add one, copy any three-line task and change the expression:

    - name: Say what you get here
      ansible.builtin.debug:
        var: _segments | map(attribute='name') | list

`debug: var:` prints the expression as its own output label, so the printout
always shows what produced it. To add a whole new group, drop a file in `tasks/`,
add one block to `tasks/main.yml`, and add its name to `network_facts_show` in
`defaults/main.yml`.

New platform lists need no change here at all — declare the view in the catalog
and it shows up automatically.

## Preconditions

- Must be called from a playbook inside `playbooks/`. Every recipe reads the vars
  defined in `playbooks/group_vars/all/_networks_derived.yml`, and that directory
  is only loaded for playbooks living there. The first task asserts this and
  fails with that explanation rather than letting each recipe fail separately.
- No network access, no target host, no credentials. It reads variables the
  controller has already resolved.

## Behaviour

- Read-only by default: every recipe is a `debug` task, so a run is `changed=0`.
- The single exception is `network_facts_save`. Setting it writes that path on the
  controller, overwriting it, and the run reports `changed=1`.
- Tasks whose knob is unset are skipped, not failed — a default run shows a
  healthy number of skips.
- With no tags and no knobs it prints the whole catalog, which is long. `--tags`
  is the fast path.

## Out of scope

- Configures no device — it never talks to a switch, firewall, or vCenter.
- Does not edit the catalog. Segments are added in
  `playbooks/group_vars/all/networks.yml`.
- Does not format tables, search, or pre-flight a new VLAN. That is
  `playbooks/ops_net_facts.yml`, which browses the same catalog for reading rather
  than for copying.
