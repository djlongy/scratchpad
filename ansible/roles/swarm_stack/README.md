# swarm_stack

Generic **compose-first** Docker Swarm stack deployer. A thin `<app>_swarm`
wrapper role supplies a compose template plus a manifest, and this engine
pre-creates the external docker objects (secrets/configs/networks/volumes),
renders the compose template with the resolved object names injected, and runs
`docker stack deploy -c`.

Adding a workload is a thin role — a compose template, a manifest, and a
two-line `tasks/main.yml` — never a copy of this engine.

## TL;DR

**Most common: deploy a stack via its wrapper.** This is a generic engine — never run it directly; a thin `<app>_swarm` wrapper (e.g. `mattermost_swarm`) supplies the compose template + manifest and imports it, so run *that* role's play. A no-tag run does the full idempotent deploy; `--tags redeploy` destroys and recreates.

```bash
ansible-playbook -i inventories/<env>/hosts.yml playbooks/<app>_swarm.yml [--tags redeploy]
```

## How a stack is described

A wrapper role (e.g. `mattermost_swarm`) ships:

```
roles/<app>_swarm/
├── defaults/main.yml          # app behavioural defaults (image, replicas, …)
├── vars/manifest.yml          # the swarm_stack_* manifest (engine inputs)
├── templates/compose.yml.j2   # the stack topology (portable, no secret values)
└── tasks/main.yml             # include_vars manifest → include_role: swarm_stack
```

`tasks/main.yml`:

```yaml
# Anchor the manifest's template paths on THIS role, not role_path: once
# swarm_stack is imported, role_path refers to the engine, so a bare
# {{ role_path }} in the manifest resolves to swarm_stack/templates and the
# compose file is not found.
- name: Resolve caller role path for manifest path references
  ansible.builtin.set_fact:
    swarm_stack_caller_role: "{{ role_path }}"

- name: Load <app> stack manifest
  ansible.builtin.include_vars: "{{ role_path }}/vars/manifest.yml"

# import_role (not include_role) so tags like --tags teardown / stop / start
# propagate into the engine's tasks at compile time.
- name: Deploy <app> swarm stack
  ansible.builtin.import_role:
    name: swarm_stack
```

In the manifest, reference template files via `swarm_stack_caller_role`:

```yaml
swarm_stack_compose_template: "{{ swarm_stack_caller_role }}/templates/compose.yml.j2"
swarm_stack_configs:
  - {name: <app>_static, src: "{{ swarm_stack_caller_role }}/templates/<app>.yml.j2"}
```

## What the engine does (phase order)

`validate → secrets → configs → networks → volumes → deploy → prune`

A no-tag run does the full idempotent deploy. Phase tags refine it
(`--tags secrets`, `--tags deploy`, …). `--tags redeploy` does a full
destroy→recreate; `--tags teardown` only tears down. NFS data on disk is
preserved unless you add `--tags wipe-data` (destructive).

**Lifecycle ops** (all `never`-gated — never run on a no-tag converge):

| Tag | Effect | Data |
|---|---|---|
| `--tags stop` | Save each service's replica count to `<dir>/.scale-state.json`, then scale every replicated service to 0 — the swarm equivalent of `docker stop`. For host maintenance / reboots. | preserved |
| `--tags start` | Restore each service to its saved replica count. | preserved |
| `--tags teardown` | Remove the stack + its labeled secrets/configs/networks. | NFS preserved |
| `--tags wipe-data` | (with teardown/redeploy) also wipe NFS data dirs. | **destroyed** |

`stop`/`start` leave the stack definition, secrets, configs, networks and all
volume data in place — only the running tasks stop. Global-mode services
can't be scaled to 0 and are reported + left running (drain their nodes
instead). `docker service scale` is cluster-wide and idempotent, so these are
safe to run against a single manager or the whole manager group.

| Phase | What it creates |
|---|---|
| `secrets` | external docker secrets from Vault (rolling versions, `no_log`) |
| `configs` | external docker configs from templates (rolling versions) |
| `networks`| attachable + encrypted overlay networks |
| `volumes` | NFS subpaths / bind paths (delegated) + NFS local-driver volumes |
| `deploy`  | render `compose.yml.j2` → `docker stack deploy -c` (prune + resolve-image) |
| `prune`   | drop obsolete labeled secret/config versions |

## Compose template contract

`deploy.yml` renders `swarm_stack_compose_template` with three dicts in scope so
the compose file references the **resolved** (versioned) object names:

| In the template | Resolves to |
|---|---|
| `secret_names[<logical>]`  | external docker secret name (e.g. `pg_password_v0003`) |
| `config_names[<logical>]`  | external docker config name |
| `network_names[<logical>]` | overlay network name |

Reference them in the compose top-level maps:

```yaml
services:
  app:
    image: "{{ app_image }}"
    secrets: [pg_password]
    networks: ["{{ network_names.app }}"]
    environment:
      POSTGRES_PASSWORD_FILE: /run/secrets/pg_password
secrets:
  pg_password:
    external: true
    name: "{{ secret_names.pg_password }}"
networks:
  "{{ network_names.app }}":
    external: true
```

Because secrets are created with `rolling_versions: true`, rotating a value
yields a new versioned name → a compose diff → swarm rolls the service.
Rolling secret rotation is preserved under compose-first.

## Secret resolution — portable, HashiCorp-optional

Each `swarm_stack_secrets` entry resolves its value **declared-value-first**:

1. `value` — supplied directly (e.g. from an ansible-vault var). When set it
   **wins** and no HashiCorp Vault read happens for that entry.
2. `vault_path` + `vault_field` — HashiCorp Vault **fallback**, read on the
   control node only when `value` is unset.
3. `auto_generate: true` — generate a passphrase and write it to Vault if it is
   missing there (Vault-backed entries only).

Supply `value` for every secret and the role never invokes the `vault` CLI, so it
runs on sites without HashiCorp Vault. Mix the two per entry otherwise.

```yaml
swarm_stack_secrets:
  # Portable: value from an ansible-vault-encrypted inventory var
  - name: pg_password
    value: "{{ vault_pg_password }}"
  # HashiCorp Vault fallback, auto-generated if absent
  - name: at_rest_key
    vault_path: kv/apps/myapp/runtime
    vault_field: at_rest_key
    auto_generate: true
```

## Hard rule — secrets are consumed via `/run/secrets`

The engine **never** reads a plaintext secret value into an Ansible variable for
templating, nor bakes one into the rendered compose file. Containers read their
secret from `/run/secrets/<logical_name>`:

- **Native `*_FILE` env** where the image supports it
  (`POSTGRES_PASSWORD_FILE`, `ELASTIC_PASSWORD_FILE`, …).
- **Entrypoint shim** for apps that need the secret inside a composed string:

  ```yaml
  command:
    - sh
    - -c
    - 'export DSN="postgres://u:$(cat /run/secrets/pg_password)@db/app"; exec /entrypoint.sh app'
  ```

The rendered compose file therefore holds no credentials and is kept at `0640`
for auditability (set `swarm_stack_keep_rendered: false` to render-and-remove).

## Manifest variables

See `meta/argument_specs.yml` and `defaults/main.yml` for the full contract.
Key inputs:

| Variable | Purpose |
|---|---|
| `swarm_stack_name` | stack name (docker stack name + label) |
| `swarm_stack_compose_template` | path to the wrapper's `compose.yml.j2` |
| `swarm_stack_secrets` | list of `{name, value?, vault_path?, vault_field?, auto_generate?}` — `value` wins, Vault is the fallback |
| `swarm_stack_configs` | list of `{name, src[, target, mode]}` |
| `swarm_stack_networks` | list of `{name, encrypted, subnet, …}` |
| `swarm_stack_volumes` | list of `{name, type: volume\|bind\|tmpfs, …}` |
| `swarm_stack_nfs` | `{host, server, base}` (required when NFS volumes declared) |
| `swarm_stack_with_registry_auth` | pass `--with-registry-auth` for private registries |
| `swarm_stack_keep_rendered` | keep the rendered compose file (default `true`) |

## Consumers

`mattermost_swarm`, `traefik_swarm`, `splunk_swarm`, `elk_swarm` — each a thin
wrapper over this engine.
