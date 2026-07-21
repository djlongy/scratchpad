# common

## TL;DR

Tasks-only role for cross-cutting helpers shared by the rest of the playbooks/roles.
Each task file is independent and is invoked as a `tasks_from:` entrypoint — never run
standalone.

```yaml
- ansible.builtin.import_role:
    name: common
    tasks_from: ensure_secrets
```

## Requirements

Install collections before running (repo `requirements.yml`, or ad-hoc):

    ansible-galaxy collection install -r requirements.yml

| Collection | When | Used for |
|---|---|---|
| `community.hashi_vault` | When `ensure_secrets` reads/writes Vault | `vault_kv2_get` / `vault_kv2_write` |
| `community.general` | When `generate_passphrase` runs | `random_words` lookup |
| `community.general` | When `mounts` or `update_hostname` runs | LVM (`filesystem`/`lvg`/`lvol`), SELinux context (`sefcontext`), `timezone` |
| `ansible.posix` | When `mounts` runs | `mount` |

## Key variables

No `defaults/main.yml` or `meta/argument_specs.yml` — this is a task-library role;
each entrypoint takes its own input variables directly (no role-level vars). See
`tasks/<entrypoint>.yml` for the authoritative contract.

**Required** = value must be correct for a successful run (defaults often work).
**Optional** = safe to leave default / empty; phase stays off or uses built-ins.

| Req | Variable | Entrypoint | Purpose |
|---|---|---|---|
| **Required** | `managed_secrets` (list) | `ensure_secrets` | Secrets to resolve — see entry shape below |
| **Required** (per entry) | `vault_path` | `ensure_secrets` | Path within the mount (e.g. `apps/myapp/runtime`) |
| Optional (per entry) | `var` | `ensure_secrets` | Ansible variable name to check/bind; defaults to `name` |
| Optional (per entry) | `vault_mount` | `ensure_secrets` | KV v2 mount point; defaults to `kv` |
| Optional (per entry) | `vault_field` | `ensure_secrets` | Field within the secret; defaults to `name` |
| — | *(none)* | `generate_passphrase` | Takes no input; sets the fact `passphrase` |

## Usage

```yaml
- name: Bootstrap secrets
  ansible.builtin.import_role:
    name: common
    tasks_from: ensure_secrets
  vars:
    managed_secrets:
      - name: mm_pg_password
        var: vault_mm_pg_password
        vault_mount: kv-<env>
        vault_path: apps/mattermost/runtime
        vault_field: pg_password
```

Run it as part of any playbook that lists a role needing those secrets — no
standalone invocation.

## Entrypoints

| `tasks_from` | Purpose |
|---|---|
| `generate_passphrase` | Generate one xkcd-style passphrase (sets `passphrase` fact) |
| `ensure_secrets` | Resolve a list of secrets from scope → Vault → generate |
| `audit_logging` / `audit_accumulate` | Audit log helpers |
| `fapolicyd` | fapolicyd config |
| `loggers/*` | Log shipping per backend (rsyslog, splunk, fluentd, …) |
| `mounts` | Create LVM-backed mounts from a host's non-boot disks |
| `update_disks` | Extend the root partition (Debian) |
| `update_hostname` | Set hostname, `/etc/hosts`, and timezone |

`_ensure_one_secret.yml` is an internal helper — leading underscore by convention, not
meant to be called directly.

## Preconditions

`ensure_secrets` needs `VAULT_ADDR` and valid auth (token in env / `~/.vault-token` /
approle) already resolvable from the control node — Vault reads/writes are delegated
to `localhost`.

## Behaviour

`generate_passphrase` produces a strong xkcd-style passphrase with a random digit
injected — shape **Word7+Word+Word3+Word**: four capitalised dictionary words joined
by a random special character (`^ - = + _`), with one word suffixed with a random
digit. Length is retried until ≥ 29 characters so it satisfies most "min 12"/"min
16"/"min 28" password policies. The `xkcdpass` Python package is auto-installed via
pip on first run, delegated to `localhost` — do not wrap the `import_role` in
`delegate_facts: true`, or the resulting `passphrase` fact fails to land on the
*calling* host's vars.

`ensure_secrets` resolves each entry in `managed_secrets` in this order:

1. **Ansible variable scope** — value already defined and non-empty in `vault.yml` /
   group_vars / host_vars / extra-vars → use as-is.
2. **HashiCorp Vault** — read the entry's `vault_path[vault_field]`. Found and
   non-empty → use, no generation.
3. **Generate** — run `generate_passphrase`, write the result to Vault
   (read-merge-write with CAS so other fields at the same path stay intact), use the
   new value.

The resolved value is set as a fact under the entry's `var` name and a `debug` task
echoes the origin (`scope`/`vault`/`generated`) so a run at a glance shows whether
anything was created.
