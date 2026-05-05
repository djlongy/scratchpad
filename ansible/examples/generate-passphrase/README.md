# generate-passphrase

Drop-in Ansible task file that generates a strong xkcd-style passphrase
with a random digit injected. Output lands in the fact `passphrase`.

## Shape

`Word7+Word+Word3+Word` — four capitalised dictionary words, joined by
a random special character (`^ - = + _`), with one of those words
suffixed with a random digit. Length is retried until ≥ 29 characters
so it satisfies most "min 12" / "min 16" / "min 28" password policies.

## Dependencies

- `xkcdpass` Python package on the **control node** (auto-installed via
  pip on first run).
- `community.general` collection (for the `random_words` lookup).

## Usage

```yaml
- name: Make a passphrase
  block:
    - ansible.builtin.include_tasks: generate_passphrase.yml
  delegate_to: localhost
  become: false

- name: Use it
  ansible.builtin.debug:
    msg: "Generated: {{ passphrase }}"
```

Wrap the include in a `delegate_to: localhost` block when the calling
play targets a remote host — `xkcdpass` runs on the control node, and
remote hosts may not have it installed.

Do **not** use `delegate_facts: true`. We want the resulting
`passphrase` fact on the *calling* host's vars (so subsequent tasks on
that host can reference it), not on localhost.

## Why not just `lookup('password', ...)` or `random_string`?

Both work, but produce either flat random characters (hard to type,
hard to remember) or a single passphrase word with no policy controls.
This pattern gives you:

- multiple capitalised dictionary words (memorable, dictation-friendly)
- a special character delimiter (covers complexity rules)
- one digit (covers digit-required rules)
- a length floor enforced by retry

…in one fact, with `no_log` discipline so the value never lands in
playbook output.

## ensure_secrets.yml — declarative secret bootstrapping

`ensure_secrets.yml` wraps the generator with a HashiCorp Vault
fall-back so a wrapper role can declare which secrets it needs and
have them resolve idempotently. Resolution order per entry:

1. **Ansible variable scope** — value already defined and non-empty
   in `vault.yml` / group_vars / host_vars / extra-vars → use as-is.
2. **HashiCorp Vault** — read the entry's `vault_path[vault_field]`.
   Found and non-empty → use, no generation.
3. **Generate** — run `generate_passphrase.yml`, write the result to
   Vault (read-merge-write with CAS so other fields at the same path
   stay intact), use the new value.

The resolved value is set as a fact under the entry's `var` name
(defaults to `name`) and is also echoed via a `debug` task tagged
with origin (`scope` / `vault` / `generated`) so you can see at a
glance whether anything was created.

```yaml
- hosts: swarm_bootstrap
  vars:
    managed_secrets:
      - name: mm_pg_password
        var: vault_mm_pg_password
        vault_mount: kv-mgt
        vault_path: apps/mattermost/runtime
        vault_field: pg_password
      - name: mm_at_rest_key
        var: vault_mm_at_rest_key
        vault_mount: kv-mgt
        vault_path: apps/mattermost/runtime
        vault_field: at_rest_key
  tasks:
    - ansible.builtin.include_tasks: ensure_secrets.yml

    - ansible.builtin.debug:
        msg: "DSN uses {{ vault_mm_pg_password | length }}-char password"
```

Requires the `community.hashi_vault` collection on the control node
plus a working Vault auth (token / approle / etc.) reachable from
`localhost` — Vault reads/writes are delegated there.
