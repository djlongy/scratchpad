# facts

A tiny **troubleshooting** role. It gathers all facts on the target, prints a readable
set of the useful ones (`facts_known`) next to any variables you hand it
(`facts_passed`), and then — only when you run with `-v` — **pauses** so you can eyeball
everything before the play continues. It's **read-only**: it changes nothing.

Use it when something isn't behaving and you want to confirm what Ansible actually sees
on a host (its hostname, IPs, OS, plus the specific vars a role/play is about to act on).

## TL;DR

**Most common: look at a host's facts + your vars, then continue.**

```bash
ansible-playbook -i inventories/<env>/hosts.yml playbooks/facts.yml --limit myhost -v \
  -e '{"facts_passed": {"target_url": "{{ target_url }}", "run_mode": "check"}}'
```

Drop `-v` to just print and move on (no pause); add `-e '{"facts_known_keys": []}'` to dump
the entire `ansible_facts` instead of the curated set.

## Use it inside another play

Insert it right before the task you're debugging to freeze the run and show state:

```yaml
- name: Deploy the thing
  hosts: web
  tasks:
    - name: Inspect what we're about to use
      ansible.builtin.import_role:
        name: facts
      vars:
        facts_passed:
          app_version: "{{ app_version }}"
          upstream_url: "{{ upstream_url }}"
          resolved_port: "{{ resolved_port | default('UNSET') }}"

    - name: ... the task that was misbehaving ...
      ansible.builtin.debug: { msg: "carry on" }
```

Run the play normally → it prints and continues. Run it with `-v` → it prints and **waits
for ENTER** so you can confirm the facts/vars are right before it proceeds.

## What it shows

A single labelled report so provenance is obvious:

```yaml
facts_report:
  facts_known:        # curated from the gathered ansible_facts
    hostname: web01
    fqdn: web01.example.com
    default_ipv4: {address: 10.0.0.11, interface: eth0, ...}
    distribution: AlmaLinux
    os_family: RedHat
    python: {version: {...}}
    ...
  facts_passed:       # exactly what you handed the role
    app_version: "1.4.2"
    upstream_url: "https://example.com"
    resolved_port: "UNSET"
```

## Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `facts_passed` | `{}` | Dict of variables to show next to the facts. Pass at import time. |
| `facts_known_keys` | *curated list* | Which `ansible_facts` keys to surface. Empty list `[]` = show **all** gathered facts. Keys absent on the host are skipped. |
| `facts_pause` | `true` | Whether to pause for review after printing (still gated on verbosity). |
| `facts_pause_min_verbosity` | `1` | Minimum `-v` count for the pause to fire (1 = pause at `-v`+). |

The default `facts_known_keys` is a sensible set for debugging (identity, OS, network,
runtime). Because absent keys are dropped, the same list works across distros.

## Notes

- **Don't pass secrets in `facts_passed`** — the role prints its values to the console.
  It's a look-at-it tool; keep vaulted material out of it.
- The pause only makes sense interactively. In CI / non-interactive runs, either don't
  pass `-v`, or set `facts_pause: false`.
- With multiple hosts, the facts print per host and the pause prompts once before the
  play continues — scope with `--limit` when you only care about one host.
- `gather_facts: false` on your play is fine: the role runs `setup` (subset `all`) itself.
