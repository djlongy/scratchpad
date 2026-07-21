# hashicorp_vault

## TL;DR

Deploys HashiCorp Vault as a Docker container on a persistent second disk, with
topology auto-scaling from the play's host count (1 host = standalone Raft, N odd
hosts = Raft HA).

```bash
ansible-playbook -i inventories/<env>/hosts.yml <playbook>.yml --tags <phase>
```

## Requirements

Install collections before running (repo `requirements.yml`, or ad-hoc):

    ansible-galaxy collection install -r requirements.yml

| Collection | When | Used for |
|---|---|---|
| `community.crypto` | When `hashicorp_vault_tls_generate` (default true) | self-signed CA + server certificate |
| `ansible.posix` | When `hashicorp_vault_manage_firewall` (default true) / NFS backup target | firewalld ports, NFS mount for backups |
| `community.hashi_vault` | When `hashicorp_vault_ldap_enabled` + bindpass fallback | fetch the LDAP bind password from an existing Vault |

## Key variables

Full list: `defaults/main.yml`. Contract: `meta/argument_specs.yml`.

**Required** = value must be correct for a successful run (defaults often work).
**Optional** = safe to leave default / empty; phase stays off or uses built-ins.
**When X** = required only if that feature is on.

| Req | Variable | Default | Purpose |
|---|---|---|---|
| **Required** | `hashicorp_vault_data_mount` | `/opt/vault` | Persistent second-disk mount; preflight asserts it's a real mountpoint |
| Optional | `hashicorp_vault_require_mounted` | `true` | Set false to bypass the mountpoint assertion |
| Optional | `hashicorp_vault_nodes` | play's hosts | Cluster members (odd count). Pin explicitly to decouple from `--limit` |
| Optional | `hashicorp_vault_image` | `hashicorp/vault:1.19.5` | Container image (pin an exact tag) |
| Optional | `hashicorp_vault_advertise_addr` | `{{ ansible_host }}` | Address peers/clients use to reach this node |
| Optional | `hashicorp_vault_api_port` / `_cluster_port` | `8200` / `8201` | API and Raft cluster listeners |
| Optional | `hashicorp_vault_key_shares` / `_key_threshold` | `1` / `1` | Shamir unseal key shares and threshold at init |
| Optional | `hashicorp_vault_auto_unseal` | `false` | Systemd unseal-on-boot (places the unseal key on every node) |
| Optional | `hashicorp_vault_tls_enabled` | `true` | Serve the API over TLS |
| Optional | `hashicorp_vault_tls_generate` | `true` | Self-sign CA + server cert; `false` requires supplied `_tls_ca_cert`/`_server_cert`/`_server_key` |
| Optional | `hashicorp_vault_tls_onhost` | `false` | Use cert/key/CA already present on the node (certmonger) instead of generating |
| Optional | `hashicorp_vault_manage_firewall` | `true` | Open API + cluster ports via firewalld |
| Optional | `hashicorp_vault_backup_enabled` | `true` | Scheduled Raft snapshots via systemd timer |
| Optional | `hashicorp_vault_manage_policies` | `false` | Ensure `kv-<tenant>-<env>` mounts + ACL policies |
| Optional | `hashicorp_vault_ldap_enabled` | `false` | LDAP auth method + group→policy mappings |
| Optional | `hashicorp_vault_pki_enabled` | `true` | PKI secrets engine mount + tune + issuing roles |

Other data-driven, empty-skips-the-phase inputs: `hashicorp_vault_tenants`,
`hashicorp_vault_identity_groups`, `hashicorp_vault_userpass_accounts`,
`hashicorp_vault_approles`, `hashicorp_vault_gitlab_jwt_roles`,
`hashicorp_vault_transit_keys`, `hashicorp_vault_pki_roles`,
`hashicorp_vault_audit_enabled`. See `defaults/main.yml` for shapes and examples.

## Usage

```yaml
- name: Deploy HashiCorp Vault
  hosts: <group>          # 1 host -> standalone; odd N -> Raft HA
  become: true
  roles:
    - role: storage        # provision + mount the second disk
    - role: docker          # container engine + compose plugin
    - role: hashicorp_vault
```

```bash
ansible-playbook -i inventories/<env>/hosts.yml <playbook>.yml
```

Do not `--limit` a subset during `deploy`/`init` — topology is derived from the
play's hosts, so a limited run would misconfigure Raft. Target the whole group,
or pin `hashicorp_vault_nodes` explicitly in inventory.

## Preconditions

- A persistent second disk already mounted at `hashicorp_vault_data_mount`
  (e.g. via the `storage` role) — preflight asserts it's a real mountpoint.
- Docker engine + compose plugin already present (e.g. via the `docker` role).
- Only odd node counts are valid (1, 3, 5, 7…) — preflight refuses an even
  count because a 2-node Raft has no fault tolerance (quorum = 2). Grow/shrink
  1 ↔ 3 directly.

## Behaviour

- **Reboot / unseal** — a restarted node comes up **sealed** by default.
  Bring it back non-destructively with `--tags unseal` (never re-initializes
  or wipes state). For hands-off recovery, set
  `hashicorp_vault_auto_unseal: true` — this places the unseal key on every
  node; keep it off for a stricter posture.
- **Secrets** — unseal key(s) and root token are written to
  `<hashicorp_vault_keys_dir>/vault_init.json` (mode 0400) on the first
  node's persistent disk. Move them into an out-of-band secret store per
  site policy — the role does not push them anywhere. Tasks handling them
  use `no_log: true`.
- **Auth and policies** — enable only what a given set of tokens needs:
  `manage_policies` creates KV mounts and HCL policies (inert until an auth
  method attaches them); `ldap`, `userpass`, `approle`, and `gitlab_jwt` are
  independent auth methods that attach policies by name. `identity_groups`
  is a second RBAC layer on top of LDAP (external groups map an LDAP group
  via an alias; internal groups nest other Identity groups). Worked examples
  for two RBAC shapes live under `examples/`.

  ```yaml
  hashicorp_vault_manage_policies: true
  hashicorp_vault_tenants:
    - {tenant: acme, env: prod}     # -> mount kv-acme-prod, policy acme-prod

  hashicorp_vault_ldap_enabled: true
  hashicorp_vault_ldap_url: "ldaps://ipa.example.com"
  hashicorp_vault_ldap_binddn: "uid=svc-vault,cn=users,cn=accounts,dc=example,dc=com"
  hashicorp_vault_ldap_bindpass: "{{ vaulted_ldap_bindpass }}"
  hashicorp_vault_ldap_userdn: "cn=users,cn=accounts,dc=example,dc=com"
  hashicorp_vault_ldap_groupdn: "cn=groups,cn=accounts,dc=example,dc=com"
  # auto-maps auth/ldap/groups/vault-acme-prod -> policies default,acme-prod
  ```
- **Backup and restore** — a systemd timer runs on every node; only the
  active Raft leader snapshots, so exactly one snapshot is produced per
  schedule. Point `hashicorp_vault_backup_dir` at a shared/NFS location for
  real cluster DR — the default is per-node local storage. `--tags
  backup_now` (opt-in, `never`-tagged) forces an on-demand snapshot and
  fails the run on error. `--tags restore` applies a snapshot on the active
  leader with `-force` and is opt-in and destructive; `"latest"` resolves
  across every node before staging onto the leader.

  ```yaml
  hashicorp_vault_backup_dir: /mnt/vault-backups
  hashicorp_vault_backup_nfs_server: "nfs.example.com"
  hashicorp_vault_backup_nfs_export: "/exports/vault-backups"
  ```
- **Scale-down** — `--tags remove_peers` (opt-in, `never`-tagged) evicts
  stale Raft peers left behind after removing a node from the group.

## Expected result

- A successful run leaves every node unsealed and, in HA, every peer joined
  to Raft — asserted automatically (`verify` phase); spot-check manually
  with `vault status` against `hashicorp_vault_advertise_addr`.
- `--tags renew_drill` (`hashicorp_vault_tls_onhost` only, opt-in, `never`-tagged)
  proves the certmonger TLS-renewal path today instead of discovering it
  broken ~90 days out at the first real renewal: forces a re-issue and
  asserts a new certificate serial was issued, published, and Vault reloaded
  via SIGHUP without sealing.
