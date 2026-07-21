# mattermost_swarm

## TL;DR

Deploys Mattermost (app + PostgreSQL) as a Docker Swarm stack. A thin,
compose-first wrapper over the `swarm_stack` engine: it ships
`templates/compose.yml.j2` plus a `vars/manifest.yml` describing the docker
secrets, configs, overlay network, and NFS-backed volumes, then hands off to
`swarm_stack` via `import_role`.

```bash
ansible-playbook -i inventories/<env>/hosts.yml playbooks/mattermost_swarm.yml
```

## Requirements

Install collections before running (repo `requirements.yml`, or ad-hoc):

    ansible-galaxy collection install -r requirements.yml

| Collection | When | Used for |
|---|---|---|
| `community.hashi_vault` | When `mattermost_ldap_enabled` | fetch the FreeIPA CA cert from Vault for the LDAP CA docker config |

## Key variables

Full list: `defaults/main.yml`. No `meta/argument_specs.yml` in this role —
required/optional below is judged from what each default resolves to and how
`vars/manifest.yml` / `templates/compose.yml.j2` consume it.

**Required** = value must be correct for a successful run (defaults often work).
**Optional** = safe to leave default / empty; phase stays off or uses built-ins.
**When X** = required only if that feature is on.

| Req | Variable | Default | Purpose |
|---|---|---|---|
| **Required** | `mattermost_nfs_host` | `""` | Inventory hostname of the NFS server (used for `delegate_to` subpath setup) |
| **Required** | `mattermost_nfs_server` | `""` | Address swarm workers mount the share from |
| **Required** | `mattermost_site_url` | `https://mattermost.example.com` | `MM_SERVICESETTINGS_SITEURL`; must be the real public URL |
| Optional | `mattermost_nfs_base` | `/srv/nfs` | Export root on the NFS host |
| Optional | `mattermost_vault_path` | `kv/apps/mattermost/runtime` | Vault path backing the docker secrets; auto-generated fields land here |
| Optional | `mattermost_pg_uid` / `mattermost_pg_gid` | `70` / `70` | Must match the postgres image variant (alpine=70, Debian=999) |
| Optional | `mattermost_app_uid` / `mattermost_app_gid` | `2000` / `2000` | Mattermost team-edition's runtime uid/gid |
| Optional | `mattermost_postgres_image` | `postgres:17-alpine` | Postgres image; stick to the alpine variant to keep uid 70 |
| Optional | `mattermost_app_image` | `mattermost/mattermost-team-edition:9.11` | Mattermost app image |
| Optional | `mattermost_replicas` | `3` | App service replica count |
| When shared-NFS postgres | `mattermost_pg_pinned_node` | `""` | Swarm node hostname to pin postgres to; must match the node's registered hostname exactly. Empty assumes external HA postgres |
| When LDAP | `mattermost_ldap_enabled` | `false` | Turns on the `MM_LDAPSETTINGS_*` block and the CA-cert docker config |
| When LDAP | `mattermost_ldap_host` / `mattermost_ldap_base_dn` / `mattermost_ldap_bind_username` | placeholder FreeIPA-style values | Directory connection details |

## Minimum configuration

```yaml
# group_vars/mattermost_swarm_hosts.yml
---
# Required
mattermost_nfs_host: "REPLACE_ME_mattermost_nfs_host"
mattermost_nfs_server: service.example.internal
mattermost_site_url: "https://service.example.internal"
```

## Usage

```yaml
- hosts: swarm_bootstrap
  become: true
  roles:
    - role: mattermost_swarm
```

Run it:

```bash
ansible-playbook -i inventories/<env>/hosts.yml playbooks/mattermost_swarm.yml
```

Re-running redeploys idempotently; a Vault value rotation produces a new
versioned secret name, which diffs the compose file and rolls only the
affected service.

## Preconditions

- The NFS server (`mattermost_nfs_server`) must already export a share
  reachable from every swarm worker — this role mounts it, it does not
  provision the NFS server itself.
- `mattermost_pg_pinned_node` must match the swarm node's registered
  hostname exactly. If your nodes join the swarm under FQDNs (e.g.
  `worker-01.example.com`), set the FQDN here — a short name silently
  matches no node and postgres stays `Pending`.

## Behaviour

- **Secrets — `/run/secrets` only** — no plaintext secret value is ever read
  into an Ansible var or baked into the rendered compose file. Containers
  read their secrets from `/run/secrets/<name>`: **mm-postgres** uses the
  native `POSTGRES_PASSWORD_FILE` pattern; **mm-app** has no `*_FILE` env for
  the DSN / at-rest key / public-link salt / LDAP bind password, so its
  `command` is an entrypoint shim that `cat`s each secret file, `export`s the
  value, then `exec /entrypoint.sh mattermost`. `pg_password`,
  `at_rest_encrypt_key`, and `public_link_salt` auto-generate on first run
  and are written to `mattermost_vault_path`. The LDAP bind password is not
  auto-generated — `playbooks/mattermost_freeipa_prep.yml` seeds it into
  Vault first, against the same FreeIPA user it sets.
- **LDAP / FreeIPA** — when `mattermost_ldap_enabled: true`, the role
  appends a conditional `mm_ldap_bind_password` secret and an `mm_ldap_ca`
  docker config (rendered from `templates/mm-freeipa-ca-cert.j2`) before
  handing off to the engine; the compose template wires the
  `MM_LDAPSETTINGS_*` env plus mounts the CA config at
  `mattermost_ldap_ca_cert_path`.
- **Non-destructive stop / start** — for host maintenance (kernel updates,
  reboots), the underlying `swarm_stack` engine can scale every service to 0
  and back without removing the stack, its secrets/configs/network, or any
  volume data:

  ```bash
  # Quiesce: save each service's replica count and scale them all to 0
  ansible-playbook -i inventories/<env>/hosts.yml playbooks/mattermost_swarm.yml --tags stop

  #   ... patch / reboot the worker nodes ...

  # Resume: restore each service to its saved replica count
  ansible-playbook -i inventories/<env>/hosts.yml playbooks/mattermost_swarm.yml --tags start
  ```

  `stop` writes the replica counts to `<stack_dir>/.scale-state.json`;
  `start` reads them back.
