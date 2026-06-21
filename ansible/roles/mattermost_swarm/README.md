# mattermost_swarm

Deploys **Mattermost** (app + PostgreSQL) as a Docker Swarm stack. A thin,
compose-first wrapper over the generic [`swarm_stack`](../swarm_stack/) engine:
it ships a `templates/compose.yml.j2` plus a `vars/manifest.yml` describing the
docker secrets, configs, overlay network, and NFS-backed volumes, then calls
`include_role: swarm_stack`.

The engine pre-creates the external docker objects (secrets/configs/networks/
volumes) and renders this role's compose template with the resolved (versioned)
object names injected as `secret_names` / `config_names` / `network_names`.

## Secrets — `/run/secrets` only

No plaintext secret value is ever read into an Ansible var or baked into the
rendered compose file. Containers read their secrets from `/run/secrets/<name>`:

- **mm-postgres** uses the native `POSTGRES_PASSWORD_FILE` pattern.
- **mm-app** has no `*_FILE` env for the DSN / at-rest key / public-link salt /
  LDAP bind password, so its `command` is an entrypoint shim that `cat`s each
  secret file, `export`s the value, then `exec /entrypoint.sh mattermost`.

The Vault-backed secrets (`pg_password`, `at_rest_encrypt_key`,
`public_link_salt`) auto-generate on first run and are written to
`{{ mattermost_vault_path }}`. The LDAP bind password is **not** auto-generated
— `playbooks/mattermost_freeipa_prep.yml` seeds it into Vault
first, against the same FreeIPA user it sets.

## LDAP / FreeIPA

When `mattermost_ldap_enabled: true`, `tasks/main.yml` appends a conditional
`mm_ldap_bind_password` secret and a `mm_ldap_ca` docker config (rendered from
`templates/mm-freeipa-ca-cert.j2`) before handing off to the engine, and the
compose template wires the `MM_LDAPSETTINGS_*` env plus mounts the CA config at
`mattermost_ldap_ca_cert_path`.

## Variables

Image, database, NFS backend, and FreeIPA/LDAP settings are `defaults/main.yml`
overrides; per-environment values come from `inventories/swarm/group_vars`.

## Usage

```yaml
- hosts: swarm_bootstrap
  become: true
  roles:
    - role: mattermost_swarm
```

Run via `playbooks/mattermost_swarm.yml`. Re-running redeploys
idempotently; a value rotation in Vault produces a new versioned secret name,
which diffs the compose file and rolls the affected service.

## Maintenance: non-destructive stop / start

For host maintenance (kernel updates, reboots) the underlying `swarm_stack`
engine can scale every service to 0 and back without removing the stack, its
secrets/configs/network, or any volume data — the swarm equivalent of
`docker stop`, not `docker stack rm`:

```bash
# Quiesce: save each service's replica count and scale them all to 0
ansible-playbook -i inventories/swarm/hosts.yml playbooks/mattermost_swarm.yml --tags stop

#   ... patch / reboot the worker nodes ...

# Resume: restore each service to its saved replica count
ansible-playbook -i inventories/swarm/hosts.yml playbooks/mattermost_swarm.yml --tags start
```

`stop` writes the replica counts to `<stack_dir>/.scale-state.json`; `start`
reads them back. See [`swarm_stack`](../swarm_stack/#lifecycle-ops) for the full
lifecycle tag table (`stop` / `start` / `teardown` / `wipe-data`).

> **Pinning gotcha:** `mattermost_pg_pinned_node` must match the swarm node's
> registered hostname exactly. If your nodes join the swarm under FQDNs (e.g.
> `worker-01.example.com`), set the FQDN here — a short name silently matches
> no node and postgres stays `Pending`.
