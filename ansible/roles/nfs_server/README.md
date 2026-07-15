# nfs_server

One NFS server role for a **dual-purpose storage host**: generic exports (e.g.
docker_swarm app data) *and* per-user/shared user storage. Every export source is
a list, so config scales out from inventory. Renders a single `/etc/exports`.

## TL;DR

**Most common: re-render exports after adding a share/user.** Edit `nfs_server_exports` / `nfs_server_users` / `nfs_server_shares`, then re-run — a no-tag run does everything; scope to just `/etc/exports` with the `exports` tag.

```bash
ansible-playbook -i inventories/<env>/hosts.yml playbooks/site.yml [--tags exports] [--limit <host>]
```

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
  (run `freeipa_client` first). The role then **creates the `nfs/<fqdn>` service
  principal in FreeIPA and fetches its keytab** (needs an IPA admin credential —
  `nfs_server_ipa_admin_password` declared, or `nfs_server_vault_secret` fallback)
  and sets the idmapd `Domain`. Without this, `rpc.gssd` rejects krb5p mounts.

> **Per-user owners must exist in FreeIPA.** Each `nfs_server_users` entry is
> chowned to that user, so the user must resolve on this host (created via
> `freeipa_server` IAM). The role asserts this with a clear message.

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
