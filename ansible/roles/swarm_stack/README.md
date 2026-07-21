# swarm_stack

Generic **compose-first** Docker Swarm stack deployer. A thin `<app>_swarm`
wrapper role supplies a compose template plus a manifest, and this engine
pre-creates the external docker objects (secrets/configs/networks/volumes),
renders the compose template with the resolved object names injected, and runs
`docker stack deploy -c`.

## TL;DR

Never run this engine directly — a thin `<app>_swarm` wrapper role (e.g.
`mattermost_swarm`) supplies the compose template + manifest and imports it via
`import_role: swarm_stack`. Run the wrapper's playbook. A no-tag run does the
full idempotent deploy; `--tags redeploy` destroys and recreates.

```bash
ansible-playbook -i inventories/<env>/hosts.yml playbooks/<app>_swarm.yml [--tags redeploy]
```

## Requirements

Install collections before running (repo `requirements.yml`, or ad-hoc):

    ansible-galaxy collection install -r requirements.yml

| Collection | When | Used for |
|---|---|---|
| `community.docker` | always | secrets/configs/networks/volumes lifecycle + `docker stack deploy` |

## Key variables

Full list: `defaults/main.yml`. Contract: `meta/argument_specs.yml`. These are
the manifest the wrapper role supplies (`vars/manifest.yml`), not inventory vars.

**Required** = value must be correct for a successful run (defaults often work).
**Optional** = safe to leave default / empty; phase stays off or uses built-ins.
**When X** = required only if that feature is on.

| Req | Variable | Default | Purpose |
|---|---|---|---|
| **Required** | `swarm_stack_name` | — | Stack name (docker stack name + label) |
| **Required** | `swarm_stack_compose_template` | — | Absolute path to the wrapper's `compose.yml.j2` |
| Optional | `swarm_stack_secrets` | `[]` | `{name, value?, vault_path?, vault_field?, auto_generate?}` — `value` wins, HashiCorp Vault is the fallback |
| Optional | `swarm_stack_configs` | `[]` | `{name, src[, target, mode]}` |
| Optional | `swarm_stack_networks` | `[]` | `{name, encrypted, subnet, …}` overlay networks |
| Optional | `swarm_stack_volumes` | `[]` | `{name, type: volume\|bind\|tmpfs, …}` |
| When NFS volumes | `swarm_stack_nfs` | `{}` | `{host, server, base}` — required when any `type: volume` entry is declared |
| Optional | `swarm_stack_volume_hosts` | every swarm worker | Hosts NFS-backed volumes are pre-created on |
| Optional | `swarm_stack_with_registry_auth` | `false` | Pass `--with-registry-auth` for private registries |
| Optional | `swarm_stack_keep_rendered` | `true` | Keep the rendered compose file on disk (holds no secrets) |
| Optional | `swarm_stack_keep_versions` | `2` | Old hashed secret/config versions retained for rollback |

## Minimum configuration

```yaml
# group_vars/swarm_stack_hosts.yml
---
# Required
swarm_stack_name: "REPLACE_ME_swarm_stack_name"
swarm_stack_compose_template: /path/to/compose.yml.j2
```

## Usage

A wrapper role ships:

```
roles/<app>_swarm/
├── defaults/main.yml          # app behavioural defaults (image, replicas, …)
├── vars/manifest.yml          # the swarm_stack_* manifest (engine inputs)
├── templates/compose.yml.j2   # the stack topology (portable, no secret values)
└── tasks/main.yml             # include_vars manifest → import_role: swarm_stack
```

```yaml
# <app>_swarm/tasks/main.yml
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

Run it:

```bash
ansible-playbook -i inventories/<env>/hosts.yml playbooks/mattermost_swarm.yml
```

The compose template renders with three dicts in scope so it references the
**resolved** (versioned) object names, never the logical ones:

| In the template | Resolves to |
|---|---|
| `secret_names[<logical>]`  | external docker secret name (e.g. `pg_password_v0003`) |
| `config_names[<logical>]`  | external docker config name |
| `network_names[<logical>]` | overlay network name |

```yaml
services:
  app:
    secrets: [pg_password]
    networks: ["{{ network_names.app }}"]
secrets:
  pg_password:
    external: true
    name: "{{ secret_names.pg_password }}"
networks:
  "{{ network_names.app }}":
    external: true
```

## Preconditions

- An NFS server/export already exists and is reachable when any
  `swarm_stack_volumes` entry declares `type: volume` (`swarm_stack_nfs`
  points at it; the role does not create the export).
- A secret using the Vault fallback (`vault_path`/`vault_field`, no
  `auto_generate`) already holds a value at that path.

## Behaviour

- Secrets are created with `rolling_versions: true`: rotating a value yields a
  new versioned name → the rendered compose file diffs → swarm rolls the
  service. A plaintext secret value is never written into the rendered
  compose file; containers read it from `/run/secrets/<logical_name>` (a
  native `*_FILE` env var where the image supports it, or an entrypoint shim
  otherwise).
- `--tags stop` scales every replicated service to 0 after saving its replica
  count; global-mode services can't be scaled to 0 and are reported + left
  running. `--tags start` restores the saved counts.
- `--tags teardown` removes the stack plus its labeled secrets/configs/
  networks; NFS data is preserved unless combined with `--tags wipe-data`.

## Tag safety

`stop`, `start`, `redeploy`, `teardown`, and `wipe-data` are all `never`-gated
lifecycle ops — none of them run on a no-tag converge, and each must be
requested explicitly:

| Tag | Effect | Data |
|---|---|---|
| `stop` | Scale every replicated service to 0 (host-maintenance equivalent of `docker stop`) | preserved |
| `start` | Restore each service to its saved replica count | preserved |
| `redeploy` | Full destroy → recreate | NFS preserved |
| `teardown` | Remove the stack + its labeled secrets/configs/networks | NFS preserved |
| `wipe-data` | (with `teardown`/`redeploy`) also wipe NFS data dirs | **destroyed** |
