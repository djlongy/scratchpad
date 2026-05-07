# mattermost_swarm_services

Sibling of `mattermost_swarm` — same payload (postgres + mattermost-app),
same `swarm_stack` engine underneath, but exercises the
`docker_swarm_service`-per-entry path with no FreeIPA/LDAP wiring. Use
this when you want a minimal, no-ceremony Mattermost deploy on swarm and
don't need SSO.

If you need LDAP/SSO, use `mattermost_swarm` instead — that wrapper also
ships the FreeIPA prep playbook and the docker-config-shipped CA cert
plumbing.

## What it does

- Composes a complete `swarm_stack_*` spec for postgres + mattermost-app
  and hands it to `swarm_stack` via `include_role`.
- Drives all secrets through `MM_*` env vars in the per-service env
  template (Mattermost mutates `config.json` on first boot, so docker
  configs aren't viable for the main config file).
- Postgres reads its password through the `*_FILE` pattern.

The actual deploy mechanics — secrets, configs, networks, NFS volumes,
service rollouts, prune, teardown — all live in `swarm_stack`. See its
README for engine-level docs.

## Requires

- Role `swarm_stack` available on `roles_path`.
- An existing Docker Swarm with the Docker Python SDK on every candidate
  worker (see `swarm_stack` README for one-time worker setup).
- Reachable NFS server providing the export path.
- ansible-vault'd YAML with the secrets listed below.

## Quick start

```bash
ansible-playbook -i inventories/swarm/hosts.yml \
                 playbooks/mattermost_swarm_services.yml --ask-vault-pass
```

## Required vault secrets

| variable | meaning |
|---|---|
| `vault_mm_pg_password` | postgres password (read via `*_FILE` pattern) |
| `vault_mm_at_rest_key` | ≥ 32 chars; `MM_FILESETTINGS_*` AES-256 key |
| `vault_mm_public_link_salt` | ≥ 32 chars; mattermost public-link salt |
| `vault_mm_admin_password` | initial admin password (rotate after first login) |

## Knobs worth knowing

- `mattermost_pg_pinned_node` — postgres can't safely run multi-replica
  on shared NFS. Pin to a single worker.
- `mattermost_max_per_node: 1` + `update.order: stop-first` — combined,
  this lets a 3-replica / 3-worker stack roll without a deadlock.
- `vault_mm_at_rest_key` and `vault_mm_public_link_salt` MUST be ≥ 32
  chars or mattermost refuses to start.

## Variables

See `defaults/main.yml` for the full list of `mattermost_*` tunables.
