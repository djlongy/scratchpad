# nfs_server

One NFS server role for a **dual-purpose storage host**: generic exports (e.g.
docker_swarm app data) *and* per-user/shared user storage. Every export source is
a list, so config scales out from inventory. Renders a single `/etc/exports`.

## Phases (tags)

| Tag | Phase |
|---|---|
| `install` | packages + start `nfs-server` (+ optional NFSv4-only, idmapd, SELinux) |
| `firewall` | open NFS firewalld services |
| `storage` | bind-mount + generic, per-user, and shared export dirs |
| `exports` | render `/etc/exports` + reload |

A no-tag run runs everything.

## Three composable export sources

```yaml
# 1. Generic exports — arbitrary path + options (docker_swarm app data, …)
nfs_server_allowed_cidr: "192.168.21.0/24"
nfs_server_exports:
  - {path: /srv/nfs/mattermost-data, options: "rw,sync,no_subtree_check,no_root_squash"}

# 2. Per-user private storage → /srv/nfs/users/<name> (0700)
nfs_server_users:
  - {name: alice}
  - {name: bob}

# 3. Shared/group storage → /srv/nfs/share/<name> (2770 setgid)
nfs_server_shares:
  - {name: ops, group: ops-admins}
```

## Security model

`nfs_server_kerberos` controls the **generated** per-user/shared exports:

- `false` (default) → `sec=sys`, scoped by `nfs_server_allowed_cidr`.
- `true` → `sec=krb5p` (per-user Kerberos, NFSv4); host must be FreeIPA-enrolled
  (run `freeipa_client` first). Sets the idmapd `Domain`.

Generic `nfs_server_exports` always use their own `options` string, so a single
server can serve `sec=sys` app data **and** `sec=krb5p` user storage at once.

Set `nfs_server_v4_only: true` to drop rpcbind/NFSv3 and tighten the firewall to
just `nfs` (tcp/2049).

## Backward compatibility

Existing deployments that only set `nfs_server_exports` + `nfs_server_allowed_cidr`
(+ bind-mount vars) are unchanged: the new lists default empty and
`nfs_server_kerberos`/`nfs_server_v4_only` default off, so `/etc/exports` and the
package/firewall set render exactly as before.

## Variables

See `meta/argument_specs.yml` and `defaults/main.yml` for the full contract.
