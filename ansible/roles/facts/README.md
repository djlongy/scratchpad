# facts

## TL;DR

Gathers all facts on a target, prints a curated set (`facts_known`) next to any
variables you hand it (`facts_passed`), and — only when run with `-v` — pauses
so you can eyeball everything before the play continues. Read-only; it changes
nothing on the target.

```bash
ansible-playbook -i inventories/<env>/hosts.yml playbooks/facts.yml --limit myhost -v \
  -e '{"facts_passed": {"target_url": "{{ target_url }}", "run_mode": "check"}}'
```

## Requirements

None beyond `ansible.builtin`.

## Key variables

Full list: `defaults/main.yml`. Contract: `meta/argument_specs.yml`.

**Required** = value must be correct for a successful run (defaults often work).
**Optional** = safe to leave default / empty; phase stays off or uses built-ins.

Nothing is required — every variable has a working default and the role only
reads state, it never asserts on missing input.

| Req | Variable | Default | Purpose |
|---|---|---|---|
| Optional | `facts_passed` | `{}` | Dict of variables to show next to the facts |
| Optional | `facts_known_keys` | curated list (identity/OS/network/runtime) | `ansible_facts` keys to surface; `[]` = show everything gathered |
| Optional | `facts_pause` | `true` | Pause for review after printing (still gated on verbosity) |
| Optional | `facts_pause_min_verbosity` | `1` | Minimum `-v` count for the pause to fire |

## Usage

```yaml
- name: Show facts (troubleshooting)
  hosts: "{{ facts_hosts | default('all') }}"
  gather_facts: false          # the role gathers subset=all itself
  roles:
    - role: facts
```

Run it:

```bash
ansible-playbook -i inventories/<env>/hosts.yml playbooks/facts.yml --limit myhost
```

## Behaviour

- Prints `facts_passed` values to the console — never pass secrets or vaulted
  material through it.
- The pause (`facts_pause`) only fires under `-v` and only makes sense
  interactively; set `facts_pause: false` for CI / non-interactive runs.
- With multiple targeted hosts, facts print per host but the pause prompts
  once before the play continues — scope with `--limit` when only one host
  matters.
- `gather_facts: false` on the calling play is fine — the role runs `setup`
  (subset `all`) itself.
