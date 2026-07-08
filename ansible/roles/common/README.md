# common

Tasks-only role for cross-cutting helpers shared by the rest of the
playbooks/roles. Each task file is independent and is invoked as a
`tasks_from:` entrypoint:

```yaml
- ansible.builtin.import_role:
    name: common
    tasks_from: <task_name>      # filename in tasks/ without the .yml
```

Current entrypoints:

| `tasks_from` | Purpose |
|---|---|
| `generate_passphrase` | Generate one xkcd-style passphrase (sets `passphrase` fact) |
| `ensure_secrets` | Resolve a list of secrets from scope → Vault → generate |
| `audit_logging` / `audit_accumulate` | Audit log helpers |
| `fapolicyd` | fapolicyd config |
| `loggers/*` | Log shipping per backend (rsyslog, splunk, fluentd, …) |

`_ensure_one_secret.yml` is an internal helper — leading underscore by
convention; not meant to be called directly.

## TL;DR

**Most common: include a helper via `tasks_from`.** Never run standalone — import the role and pick an entrypoint (e.g. `ensure_secrets` to resolve secrets from scope → Vault → generate).

```yaml
- ansible.builtin.import_role:
    name: common
    tasks_from: ensure_secrets
```

---

## generate_passphrase

Generates a strong xkcd-style passphrase with a random digit injected.
Output lands in the fact `passphrase`.

**Shape** — `Word7+Word+Word3+Word`: four capitalised dictionary words
joined by a random special character (`^ - = + _`), with one word
suffixed with a random digit. Length is retried until ≥ 29 characters
so it satisfies most "min 12" / "min 16" / "min 28" password policies.

**Dependencies**
- `xkcdpass` Python package on the **control node** (auto-installed via
  pip on first run).
- `community.general` collection (for the `random_words` lookup).

**Usage**
```yaml
- name: Make a passphrase
  ansible.builtin.import_role:
    name: common
    tasks_from: generate_passphrase

- name: Use it
  ansible.builtin.debug:
    msg: "Generated: {{ passphrase }}"
```

The xkcdpass install step is internally delegated to localhost. Do
**not** wrap the import_role in `delegate_facts: true` — we want the
resulting `passphrase` fact on the *calling* host's vars.

### Why not `lookup('password', ...)` or `random_string`?

Both work, but produce either flat random characters (hard to type,
hard to remember) or a single passphrase word with no policy controls.
This pattern gives you:

- multiple capitalised dictionary words (memorable, dictation-friendly)
- a special character delimiter (covers complexity rules)
- one digit (covers digit-required rules)
- a length floor enforced by retry

…in one fact, with `no_log` discipline so the value never lands in
playbook output.

---

## ensure_secrets

Declarative secret bootstrapping with HashiCorp Vault fall-back. For
each entry in `managed_secrets`, the value is resolved in this order:

1. **Ansible variable scope** — value already defined and non-empty
   in `vault.yml` / group_vars / host_vars / extra-vars → use as-is.
2. **HashiCorp Vault** — read the entry's `vault_path[vault_field]`.
   Found and non-empty → use, no generation.
3. **Generate** — run `generate_passphrase`, write the result to Vault
   (read-merge-write with CAS so other fields at the same path stay
   intact), use the new value.

The resolved value is set as a fact under the entry's `var` name
(defaults to `name`) and a `debug` task echoes the origin
(`scope` / `vault` / `generated`) so you can see at a glance whether
anything was created on this run.

**Entry shape**

| key | required | default | meaning |
|---|---|---|---|
| `name` | ✓ | — | logical key; doubles as default for `var` and `vault_field` |
| `var` |   | `name` | Ansible variable name to check / bind |
| `vault_mount` |   | `kv` | KV v2 mount point |
| `vault_path` | ✓ | — | path within the mount (e.g. `apps/myapp/runtime`) |
| `vault_field` |   | `name` | field within the secret |

**Usage**

Call as a `pre_tasks` step so secrets exist before any role runs:

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
  pre_tasks:
    - name: Bootstrap secrets
      ansible.builtin.import_role:
        name: common
        tasks_from: ensure_secrets
  roles:
    - role: mattermost_swarm
```

After the import_role, every entry's `var` is guaranteed defined and
non-empty for the rest of the play.

**Requirements**
- `community.hashi_vault` collection on the control node.
- `VAULT_ADDR` + valid auth (token in env / `~/.vault-token` / approle)
  resolvable from the control node — Vault reads/writes are delegated
  to `localhost`.
