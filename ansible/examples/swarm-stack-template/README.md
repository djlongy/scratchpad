# Swarm-stack template

Reusable pattern for deploying any application stack onto an existing
Docker Swarm: NFS-backed volumes, docker secrets backed by an
ansible-vault YAML file, optional rendered-config-as-docker-config,
content-versioned rolling updates.

Two roles:

- **`app_swarm_stack`** — the generic role. Owns the mechanics:
  NFS subpath ensure (delegated to the NFS host), docker secret/config
  create with `rolling_versions: true`, deploy (compose-file or
  per-service module), and prune of obsolete labeled objects.

- **`app_mattermost_swarm`** — a thin wrapper using **stack mode**.
  Calls `app_swarm_stack` with mattermost-specific volumes, secrets,
  and a `stack.yml.j2` template that gets rendered + applied via
  `docker stack deploy`. Use it as the pattern for adding new apps
  with a compose-style template.

- **`app_mattermost_swarm_services`** — sibling wrapper using
  **services mode**. Same secrets, volumes, and overlay as above, but
  each service is defined as a kwargs dict and deployed via
  `community.docker.docker_swarm_service`. No stack template — the
  service definitions live inline in the role's `tasks/main.yml`.
  Use this pattern when you'd rather have Ansible-native per-service
  idempotency and keep service definitions next to the wrapper data.

## Layout

```
swarm-stack-template/
├── inventory/
│   ├── hosts.yml
│   └── group_vars/
│       └── swarm_bootstrap/
│           ├── swarm_bootstrap.yml   # non-secret tunables
│           └── vault.yml.example     # secret values (ansible-vault)
├── playbooks/
│   ├── mattermost_swarm.yml          # stack-mode deploy entry point
│   └── mattermost_swarm_services.yml # services-mode deploy entry point
└── roles/
    ├── app_swarm_stack/              # generic
    ├── app_mattermost_swarm/         # stack-mode wrapper
    └── app_mattermost_swarm_services/# services-mode wrapper
```

## How a deploy flows

1. Caller play targets `swarm_bootstrap` (one swarm manager).
2. Wrapper role assembles `swarm_stack_*` variables and includes the
   generic role.
3. Generic role:
   - mkdirs the NFS subpaths (delegate_to the NFS host).
   - Reads each declared secret value from Ansible's variable scope
     (which loads them from `group_vars/.../vault.yml`).
   - Creates a docker secret per entry, name suffixed `_v1`, `_v2`, …
     when content changes.
   - Renders + creates docker configs (same versioning).
   - Renders the wrapper's `stack.yml.j2`, fed
     `swarm_stack_config_names`, `_secret_names`, and `_secret_values`.
   - `docker stack deploy` via `community.docker.docker_stack`.
   - Prunes labeled objects no longer referenced (skips in-use).

## Mount types

`swarm_stack_volumes` entries declare a `type` field that matches the
docker mount type the consuming service will reference. The role
provisions the host-side resources accordingly:

- `type: volume` (default — NFS-backed local-driver). Role mkdirs the
  NFS subpath and pre-creates a local-driver docker volume with NFS
  driver_opts on every host in `swarm_stack_volume_hosts`. Caller
  references it as a docker volume by name.
- `type: bind`. Role mkdirs `source` on every host in `bind_hosts`
  (defaults to `swarm_stack_volume_hosts`). Caller mounts it directly
  via `type=bind, source=<host path>` in the service spec. Use this
  for services that don't tolerate NFS storage, e.g. Elasticsearch.
  Pin the consuming service to `bind_hosts` via placement constraints
  — bind mounts can't be shared.
- `type: tmpfs`. Role does nothing host-side. Caller declares the
  mount via `type=tmpfs` in the service spec.

Untyped entries are treated as `type: volume` so existing wrappers
keep working without changes.

## Deploy modes

The role supports two deploy backends, selected via
`swarm_stack_deploy_mode`:

- **`stack`** (default) — caller supplies a `stack.yml.j2` Jinja
  template. The role renders it, drops it on the manager, and applies
  via `community.docker.docker_stack`. Best when you want the full
  Compose schema or already have a stack file.

- **`services`** — caller supplies `swarm_stack_services`, a list
  where each entry is the kwargs dict for
  `community.docker.docker_swarm_service`. The role iterates and
  applies one service at a time. Best when you prefer Ansible-native
  idempotency over a compose file, or want to keep service definitions
  inline with role data.

Either mode reuses the same secrets / configs / networks / volumes
phases, so callers reference resolved names via
`swarm_stack_secret_names[<key>]` and `swarm_stack_config_names[<key>]`
in either case. Prune still runs in both modes (services get the
`app_swarm_stack=<stack-name>` label automatically).

## Secret pattern

Two kinds of secrets fit cleanly:

- **File-as-secret** (postgres, redis, anything that reads `*_FILE`).
  Listed in `swarm_stack_secrets` and the stack template references
  `secrets:` + mounts at `/run/secrets/<name>`. Use
  `POSTGRES_PASSWORD_FILE: /run/secrets/mm_pg_password`.

- **Bake-into-config-or-env** (apps that don't read `/run/secrets/`).
  Same `swarm_stack_secrets` entry, but the stack/config template
  references `swarm_stack_secret_values.<name>` to template the value
  in directly. The plaintext lives only in raft (encrypted) inside the
  service spec.

Mattermost is in the second camp — it can't run with a read-only
config.json mount (writes its version stamp on first boot), so this
template uses `MM_*` env vars rendered from `swarm_stack_secret_values`.

## Worker node setup (one-time)

The `community.docker.docker_*` modules need the Docker Python SDK on
the swarm manager. On AlmaLinux/Rocky/RHEL 9:

```bash
sudo dnf install -y python3-docker python3-requests python3-jsondiff
```

(`python3-jsondiff` lives in EPEL.)

## Adding a new app

1. Copy `roles/app_mattermost_swarm/` to `roles/app_<svc>_swarm/`.
2. Edit `defaults/main.yml` for the new app's tunables.
3. Edit `tasks/main.yml` — change the `swarm_stack_*` vars (volumes,
   secrets list, configs list).
4. Replace `templates/stack.yml.j2` with the new app's compose YAML,
   referencing `swarm_stack_secret_names`, `_config_names`, and
   `_secret_values` for the bits the role resolves.
5. Add a playbook in `playbooks/` that invokes the new wrapper role.
6. Add the app's secret variables to your encrypted vault file.

## Vault file

Copy `vault.yml.example` to `vault.yml`, populate values, then encrypt:

```bash
ansible-vault encrypt inventory/group_vars/swarm_bootstrap/vault.yml
```

Run plays with `--ask-vault-pass` (or configure
`vault_password_file` in `ansible.cfg`).

When you adopt HashiCorp Vault later, swap `tasks/create_secrets.yml`
to read via `vault kv get` (or `community.hashi_vault.vault_kv2_get`)
instead of `vars[item.var]` — the rest of the role doesn't change.

## Rolling-version mechanics

`docker_secret`/`docker_config` with `rolling_versions: true` inspects
existing objects matching `<name>_v[0-9]+`. If the latest version's
content matches what you're submitting, it's a no-op. If different, it
creates `<name>_v(N+1)` and your stack template's `external: true`
reference picks up the new name on the next deploy → swarm rolls.

The `prune` step deletes labeled objects no longer referenced by the
current stack file. In-use objects are skipped (docker refuses to
remove them; the role treats that as a no-op).

## Knobs worth knowing

- `mattermost_pg_pinned_node` — postgres can't safely run multi-replica
  on shared NFS. Pin to a single worker.
- `mattermost_max_per_node: 1` + `update_config.order: stop-first` —
  combined, this lets a 3-replica/3-worker stack roll without a
  deadlock (start-first would wait forever for a free slot).
- `mattermost_at_rest_key` and `_public_link_salt` MUST be ≥ 32 chars
  or mattermost refuses to start.

## Notes for porting

This template was extracted from a working homelab deployment. The
homelab version reads secrets from HashiCorp Vault (CLI-driven) and
auto-generates missing values via a passphrase generator. This
template strips that out in favour of an ansible-vault YAML file —
appropriate when you don't have HCV stood up yet.
