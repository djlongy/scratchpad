# nfs_server

Dedicated NFS storage server for the lab. Exports a `/srv/nfs` tree —
per-user private dirs (`users/<user>`, 0700) and shared/group dirs
(`share/<share>`, 2770 setgid) — secured by Kerberos **or** host-trust, selected
by `nfs_server_kerberos`. Pairs with the `nfs_client` role (mounts at `/net`).

## Why local homes, not NFS homes

Home directories stay on local disk; this role provides a *separate* persistent
network drive. Login never depends on this server. See
`docs/superpowers/specs/2026-06-23-nfs-network-storage-design.md`.

## Minimal config (Kerberized)

```yaml
# host is FreeIPA-enrolled first (freeipa_client), then:
nfs_server_kerberos: true
nfs_server_users:
  - {name: alice}
  - {name: bob}
nfs_server_shares:
  - {name: ops, group: ops-admins}
```

## Minimal config (host-trust)

```yaml
nfs_server_kerberos: false
nfs_server_sys_networks: ["192.0.2.0/24"]
nfs_server_users: [{name: alice}]
```

## Variables

| Variable | Default | Purpose |
|---|---|---|
| `nfs_server_kerberos` | `true` | `sec=krb5p` vs `sec=sys` |
| `nfs_server_base` | `/srv/nfs` | export root |
| `nfs_server_users` | `[]` | per-user `0700` dirs |
| `nfs_server_shares` | `[]` | shared `2770` dirs |
| `nfs_server_sys_networks` | `[]` | sec=sys allowed CIDRs (required for sys) |
| `nfs_server_export_options` | `rw,soft,no_subtree_check,root_squash` | base export opts |
| `nfs_server_realm_domain` | `domain` | idmapd Domain (krb5p) |
