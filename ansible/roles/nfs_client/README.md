# nfs_client

Mounts the `nfs_server` tree on demand at `/net/users/<user>` and
`/net/share/<share>` via autofs with `soft,nofail` so a dead server fails fast and
never hangs login. Home directories stay local — this is a separate network drive.

A `/etc/profile.d` snippet creates `~/Network -> /net/users/$USER` on first login
for discoverability.

It also sets the NFSv4 idmapd `Domain` (`nfs_client_realm_domain`, default the
realm) — this **must match the server** or files show as `nobody:nobody`.

> **One home model per host.** This role keeps `/home` **local** and adds a
> separate `/net` drive. Do **not** also apply `nfs_home_client` (which mounts
> NFS *over* `/home`, the roaming model) to the same host — pick one per fleet.

## TL;DR

**Most common: converge the autofs mounts.** Set `nfs_client_server` (match `nfs_client_kerberos` to the server), then run — mounts appear on demand under `/net`; re-running is idempotent.

```bash
ansible-playbook -i inventories/<env>/hosts.yml playbooks/site.yml [--tags nfs_client] [--limit <host>]
```

## Minimal config

```yaml
nfs_client_server: nfs-01.example.com
nfs_client_kerberos: true        # match the server
```

## Variables

| Variable | Default | Purpose |
|---|---|---|
| `nfs_client_server` | — (required) | NFS server FQDN |
| `nfs_client_base` | `/net` | client mount root |
| `nfs_client_server_export_base` | `/srv/nfs` | must match server `nfs_server_base` |
| `nfs_client_kerberos` | `true` | `sec=krb5p` vs `sec=sys` |
| `nfs_client_mount_options` | `soft,nofail,timeo=30,retrans=2` | fail-fast opts |
| `nfs_client_network_symlink` | `Network` | login-time `~/<name>` symlink (`""` disables) |
