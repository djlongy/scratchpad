# nfs_client

## TL;DR

Mounts the `nfs_server` export tree on demand at `/net/users/<user>` and
`/net/share/<share>` via autofs, using `soft,nofail` so a dead server fails
fast instead of hanging a login. Home directories stay local — this is a
separate network drive, not a home-directory mount.

```bash
ansible-playbook -i inventories/<env>/hosts.yml playbooks/nfs_client.yml
```

## Requirements

Install collections before running (repo `requirements.yml`, or ad-hoc):

    ansible-galaxy collection install -r requirements.yml

| Collection | When | Used for |
|---|---|---|
| `community.general` | always | NFSv4 idmapd `Domain` via `ini_file` |

## Key variables

Full list: `defaults/main.yml`. Contract: `meta/argument_specs.yml`.

**Required** = value must be correct for a successful run (defaults often work).
**Optional** = safe to leave default / empty; phase stays off or uses built-ins.
**When X** = required only if that feature is on.

| Req | Variable | Default | Purpose |
|---|---|---|---|
| **Required** | `nfs_client_server` | derived from the `nfs_servers` inventory group | NFS server FQDN |
| Optional | `nfs_client_base` | `/net` | Client mount root |
| Optional | `nfs_client_server_export_base` | `/srv/nfs` | Must match the server's export base |
| Optional | `nfs_client_kerberos` | `true` | `sec=krb5p` vs `sec=sys` — must match the server |
| Optional | `nfs_client_realm_domain` | global `domain` | NFSv4 idmapd Domain — must match the server or files show `nobody:nobody` |
| Optional | `nfs_client_mount_options` | `soft,nofail,timeo=30,retrans=2` | Fail-fast autofs mount options |
| Optional | `nfs_client_network_symlink` | `Network` | Login-time `~/<name>` symlink; `""` disables |
| Optional | `nfs_client_manage_service` | `true` | Enable/start autofs; disable in CI |

## Minimum configuration

```yaml
# group_vars/nfs_client_hosts.yml
---
# Required
nfs_client_server: service.example.internal
```

## Usage

```yaml
- name: Mount the NFS network drive
  hosts: nfs_clients
  become: true
  roles:
    - role: nfs_client
```

Run it:

```bash
ansible-playbook -i inventories/<env>/hosts.yml playbooks/nfs_client.yml
```

## Preconditions

- `nfs_client_server` must resolve via DNS and be running the `nfs_server`
  role with export paths under `nfs_client_server_export_base`.
- `nfs_client_kerberos` and `nfs_client_realm_domain` must match the
  server's settings, or NFSv4 id-mapping falls back to `nobody:nobody`.

## Behaviour

- Deploys a `/etc/profile.d` snippet that creates `~/Network ->
  /net/users/$USER` on first login (disable via
  `nfs_client_network_symlink: ""`).
- Mounts are autofs indirect (lazy) — a bad `nfs_client_server` value
  doesn't fail the playbook run, only the first access under `/net/...`.

## Out of scope

- Home directories — they stay local; this is a separate `/net` drive. Do
  not also mount NFS over `/home` on the same host — pick one home model
  per fleet.
