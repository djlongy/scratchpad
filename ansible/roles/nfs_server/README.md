# nfs_server

## TL;DR

Configures a dual-purpose NFS server: generic exports (e.g. app data) plus
per-user and shared/group storage. Every export source is a list, so config
scales out from inventory; the role renders a single `/etc/exports`.

```bash
ansible-playbook -i inventories/<env>/hosts.yml playbooks/nfs_server.yml --tags exports
```

## Requirements

Install collections before running (repo `requirements.yml`, or ad-hoc):

    ansible-galaxy collection install -r requirements.yml

| Collection | When | Used for |
|---|---|---|
| `ansible.posix` | always | firewalld services, SELinux booleans, bind-mount |
| `community.general` | When NFSv4-only or Kerberos | `/etc/nfs.conf` / `/etc/idmapd.conf` via `ini_file` |
| `community.hashi_vault` | When Kerberos, no `nfs_server_ipa_admin_password` | HashiCorp Vault fallback lookup for the IPA admin credential |
| `freeipa.ansible_freeipa` | When Kerberos | Create the `nfs/<fqdn>` service principal |

## Key variables

Full list: `defaults/main.yml`. Contract: `meta/argument_specs.yml`.

**Required** = value must be correct for a successful run (defaults often work).
**Optional** = safe to leave default / empty; phase stays off or uses built-ins.
**When X** = required only if that feature is on.

| Req | Variable | Default | Purpose |
|---|---|---|---|
| Optional | `nfs_server_export_base` | `/srv/nfs` | Canonical export root |
| When sec=sys | `nfs_server_allowed_cidr` | `""` | Client ACL — required whenever any `sec=sys` export exists |
| Optional | `nfs_server_kerberos` | `false` | Per-user/shared exports use `sec=krb5p` (true) or `sec=sys` (false) |
| Optional | `nfs_server_exports` | `[]` | Generic exports: `[{path, options}]` |
| Optional | `nfs_server_users` | `[]` | Per-user private storage: `[{name}]` → `<base>/users/<name>` (0700) |
| Optional | `nfs_server_shares` | `[]` | Shared storage: `[{name, group}]` → `<base>/share/<name>` (2770 setgid) |
| Optional | `nfs_server_v4_only` | `false` | Drop rpcbind/NFSv3, trim firewall to `nfs` (tcp/2049) |
| When Kerberos | `nfs_server_ipa_admin_password` | `""` | IPA admin credential to create the `nfs/<fqdn>` service principal |
| Optional | `nfs_server_manage_service` | `true` | Enable/start `nfs-server` + run exportfs; disable in CI |

## Usage

```yaml
- name: Configure NFS storage servers
  hosts: nfs_servers
  become: true
  roles:
    - role: freeipa_client   # only needed when nfs_server_kerberos is true
    - role: nfs_server
      vars:
        nfs_server_allowed_cidr: "10.0.0.0/24"
        nfs_server_exports:
          - {path: /srv/nfs/app-data, options: "rw,sync,no_subtree_check,no_root_squash"}
        nfs_server_users:
          - {name: alice}
        nfs_server_shares:
          - {name: ops, group: ops-admins}
```

Run it:

```bash
ansible-playbook -i inventories/<env>/hosts.yml playbooks/nfs_server.yml
```

## Preconditions

- `nfs_server_kerberos: true` requires the host is already FreeIPA-enrolled
  — `rpc.gssd` needs the `nfs/<fqdn>` principal, which this role creates
  and fetches into the host keytab.
- Each `nfs_server_users` entry's `name` must resolve as a real user on the
  host (FreeIPA or local) — the export directory is chowned to it.
- `sec=sys` exports (`nfs_server_kerberos: false`) need
  `nfs_server_allowed_cidr` set, or any client can mount.

## Behaviour

- `nfs_server_kerberos: true` creates the `nfs/<fqdn>` service principal in
  FreeIPA and fetches its key into the host keytab (idempotent, checked via
  `klist -k`) — a one-time bootstrap, not a per-run no-op.
- Generic `nfs_server_exports` always use their own `options` string, so a
  single server can serve `sec=sys` app data and `sec=krb5p` user storage
  at once.
