# hashicorp_vault_container

## TL;DR

Deploys HashiCorp Vault as a Docker container with all state on a persistent
second disk. Topology auto-scales from the hosts in the play: 1 host yields
standalone Raft, an odd N ≥ 3 yields a Raft HA cluster. The native-package
variant is `hashicorp_vault`.

```bash
ansible-playbook -i inventories/<env>/hosts.yml playbooks/vault_cluster.yml
```

## What it does

1. **preflight** — assert the data mount is a real mountpoint, Docker Compose
   is present, and derive scaling facts (`hashicorp_vault_is_ha`, first node).
2. **prereqs** — create state directories on the disk, open firewall ports.
3. **tls** — self-sign a CA + multi-SAN server cert (or use supplied/on-host
   certs), distribute to every node.
4. **deploy** — template `vault.hcl` + `docker-compose.yml`, start the
   container, wait for the API.
5. **init** — initialize once, unseal the leader, then unseal followers so
   they join the Raft cluster. Unseal key(s) + root token are written to
   `<hashicorp_vault_keys_dir>/vault_init.json` (mode 0400) on the first node.
6. **verify** — confirm every node is unsealed and (HA) all peers joined.
7. **backup** — install a systemd timer that takes scheduled Raft snapshots
   (leader-only) with age-based retention.

Auth/secrets phases (policies, LDAP, identity, userpass, approle, GitLab JWT,
transit, PKI, audit) are gated by their own variables — see the table below.

**Only odd node counts are valid** (1, 3, 5…). Preflight refuses an even
count (2-node Raft quorum has no fault tolerance). Grow/shrink 1 ↔ 3 directly.
Do not `--limit` a subset during deploy/init — topology is derived from the
play's hosts; pin `hashicorp_vault_nodes` explicitly if you need a stable set.

## Prerequisites (composed at the playbook level)

The role consumes a **pre-mounted** disk and an **already-installed** Docker
engine — it asserts both in preflight and fails fast otherwise:

- A persistent second disk mounted at `hashicorp_vault_data_mount` (e.g. via
  the `storage` role).
- Docker engine + compose plugin (e.g. via the `docker` role).

## Key variables

Full list: `defaults/main.yml`. Contract: `meta/argument_specs.yml`.

**Required** = value must be correct for a successful run (defaults often work).
**Optional** = safe to leave default / empty; phase stays off or uses built-ins.
**When X** = required only if that feature is on.

| Req | Variable | Default | Purpose |
|---|---|---|---|
| **Required** | `hashicorp_vault_data_mount` | `/opt/vault` | Persistent second-disk mount; all state lives under it |
| **Required** | `hashicorp_vault_nodes` | play's hosts | Cluster members (odd count). Pin to ignore `--limit` |
| Optional | `hashicorp_vault_image` | `hashicorp/vault:1.19.5` | Container image (pin an exact tag) |
| Optional | `hashicorp_vault_advertise_addr` | `{{ ansible_host }}` | Address peers/clients use for this node |
| Optional | `hashicorp_vault_api_port` / `_cluster_port` | `8200` / `8201` | API/UI and Raft cluster listeners |
| Optional | `hashicorp_vault_key_shares` / `_key_threshold` | `1` / `1` | Shamir unseal shares generated / required at init |
| Optional | `hashicorp_vault_auto_unseal` | `false` | Boot-time systemd unseal (places the key on every node) |
| Optional | `hashicorp_vault_tls_enabled` | `true` | Serve the API over TLS |
| Optional | `hashicorp_vault_tls_generate` | `true` | Self-sign CA + server cert; `false` supplies your own PEM paths |
| Optional | `hashicorp_vault_manage_firewall` | `true` | Open API + cluster ports via firewalld |
| Optional | `hashicorp_vault_backup_enabled` | `true` | Scheduled Raft snapshots (leader-only) |
| When `manage_policies` | `hashicorp_vault_tenants`, `hashicorp_vault_policies` | `[]` | KV mounts + HCL policies |
| When `ldap_enabled` | `hashicorp_vault_ldap_url`, `_binddn`, `_bindpass`, `_userdn`, `_groupdn` | `""` | FreeIPA human SSO |
| When `pki_enabled` (default `true`) | `hashicorp_vault_pki_roles` | `[]` | PKI mount + issuing roles |

When a gate is off or a list is empty, leave the related variables unset — the
phase is skipped.

## Usage

```yaml
- name: Deploy containerised HashiCorp Vault cluster
  hosts: vault                    # 1 host -> standalone; odd N -> Raft HA
  roles:
    - role: storage                # provision + mount the second disk
    - role: docker                 # container engine + compose plugin
    - role: hashicorp_vault_container
```

```yaml
# inventories/<env>/group_vars/vault.yml
storage_volumes:
  - name: vault-data
    disk: "by-size:50G"
    lvm: true
    vg: vg_vault
    lv: lv_vault
    size: 100%FREE
    fstype: xfs
    mount: /opt/vault
    provision: true

hashicorp_vault_data_mount: /opt/vault
hashicorp_vault_tls_extra_sans:
  - "DNS:vault.example.com"
```

```bash
ansible-playbook -i inventories/<env>/hosts.yml playbooks/vault_cluster.yml
# fast iteration on a single phase:
ansible-playbook -i inventories/<env>/hosts.yml playbooks/vault_cluster.yml --tags policies,ldap
```

## Phases (tags)

| Tag | Purpose |
|---|---|
| `preflight` | Always runs — validates mount/docker, derives topology |
| `tls`, `deploy`, `init` | Core provisioning |
| `unseal` | Non-destructive re-unseal after a reboot (also runs under `init`) |
| `verify` | Health + Raft peer / seal assertions |
| `policies`, `ldap`, `identity`, `userpass`, `approle`, `gitlab_jwt`, `transit`, `pki`, `pki_issuer`, `audit` | Auth/secrets phases, each gated by its own variable |
| `license` | Enterprise license install/reload — gated by `hashicorp_vault_license_enabled` |
| `backup` | Installs the scheduled snapshot timer |
| `remove_peers` (`never`) | Evicts stale Raft peers after a scale-down; opt-in |
| `backup_now` (`never`) | Forces an on-demand snapshot, fails the play on error (nightly CI) |
| `restore` (`never`) | Rolls back to a Raft snapshot — destructive, opt-in |
| `renew_drill` (`never`) | Forces a real certmonger cert re-issue to prove the renewal path |

## Scaling the cluster

Topology follows `hashicorp_vault_nodes`. **Grow (1 → 3):** add the new hosts
to the group, keeping the original node first, and re-run — `init` sees the
cluster already initialised and skips it; new nodes `retry_join` and unseal as
followers. **Shrink (3 → 1):** drop the retiring hosts from the group while
the cluster still has a leader, then run `--tags remove_peers` to evict them
from Raft membership before powering them off — a normal run otherwise fails
in `verify.yml` with the exact `raft remove-peer` commands needed.

## Backup & restore

A systemd timer runs on every node; only the active leader snapshots (checked
via `is_self`), so exactly one snapshot is produced per schedule. The snapshot
is taken inside the container and `docker cp`'d to `hashicorp_vault_backup_dir`
on the host. Backups authenticate with a scoped periodic token (policy: read
on `sys/storage/raft/snapshot`); restore uses the root token, only from the
first node.

```yaml
hashicorp_vault_backup_dir: /mnt/vault-backups        # point off-node for real HA DR
hashicorp_vault_backup_nfs_server: "nfs.example.com"
hashicorp_vault_backup_nfs_export: "/exports/vault-backups"
```

```bash
# force a snapshot now and fail the play on error
ansible-playbook -i inventories/<env>/hosts.yml playbooks/vault_cluster.yml --tags backup_now

# restore the newest snapshot across all nodes (destructive, opt-in)
ansible-playbook -i inventories/<env>/hosts.yml playbooks/vault_cluster.yml --tags restore
```

## Auth & secrets phases

Each phase is independent unless noted; enable only what a given token needs.

| Who / what | Auth method | Gate variable |
|---|---|---|
| Human (directory SSO) | LDAP (FreeIPA) | `hashicorp_vault_ldap_enabled` |
| Human (local break-glass) | userpass | `hashicorp_vault_userpass_accounts` (non-empty) |
| Automation | AppRole | `hashicorp_vault_approles` (non-empty) |
| GitLab CI job | JWT | `hashicorp_vault_gitlab_jwt_enabled` |

**Policies** (`manage_policies`) — the KV mount is the isolation domain: each
`{tenant, env}` pair gets its own `kv-<tenant>-<env>` mount and a policy of the
same name. `wide: true` on any row also writes a tenant-wide policy.

**LDAP** (`ldap_enabled`) — configures `auth/ldap` against FreeIPA and
auto-maps each tenant to `auth/ldap/groups/vault-<tenant>-<env>`. Does not
create FreeIPA groups — they must already exist.

**Identity groups** (non-empty `identity_groups`) — a second RBAC layer on
top of LDAP: `external` groups alias an LDAP group, `internal` groups nest
other Identity groups. Requires LDAP for external aliases. Pick one primary
grant path per FreeIPA group (LDAP-direct or Identity), not both.

**PKI** (`pki_enabled`, on by default) — mounts `pki/`, tunes
`max_lease_ttl`, and upserts issuing roles. Does not generate or import an
intermediate issuer; `pki_issuer_import` (off by default) adopts an escrowed,
pre-signed issuing CA (cert + key + root, verified by fingerprint).

**Transit** (`transit_enabled`) — enables the Transit engine and creates
signing keys (e.g. Cosign); the private half never leaves Vault.

**Audit** (`audit_enabled`) — enables the file audit device under
`/vault/logs`, persisted on the data mount.

See `examples/` for copy-paste inventory snippets covering LDAP groups,
Identity nesting, and CI/AppRole add-ons.

## Enterprise license (`--tags license`, optional)

**Off by default** (`hashicorp_vault_license_enabled: false`) — Community images
run unchanged, no license material required.

Vault Enterprise autoloads a license from the first match of `VAULT_LICENSE`
(raw env string) → `VAULT_LICENSE_PATH` (env file path) → `license_path` (HCL).
This role uses the last two, pointing at `license.hclic` under the bind-mounted
config dir — never the raw-string env, which would leak via `docker inspect`.

```yaml
hashicorp_vault_license_enabled: true
# Enterprise Hub tags ALWAYS carry the -ent suffix — bare version tags do not exist
hashicorp_vault_image: "hashicorp/vault-enterprise:1.19.5-ent"
hashicorp_vault_license: "{{ vaulted_vault_enterprise_license }}"   # vaulted inventory
# or a controller-side file: hashicorp_vault_license_src: "/secure/path/vault.hclic"
```

Guard rails:

- **Offline validation before install** (`hashicorp_vault_license_validate`,
  default `true`): `vault license inspect` runs in a throwaway container — a
  mangled paste or expired key fails the play *before* anything is installed or
  restarted, and it proves the Enterprise image is pullable.
- **Hot reload on renewal**: when the API is already serving, a changed license
  file is applied per node via `sys/config/reload/license` — no restart, no
  seal. First enable (image swap) recreates the container instead; follow with
  `--tags unseal`.
- **Verify-phase assertion**: with the license enabled, `verify` runs
  `vault license get` and fails unless the running binary reports an
  **autoloaded** license (file-in-place ≠ license-in-effect), then prints the
  expiry.

Full operator walkthrough (first enable, renewal, troubleshooting, rollback):
[`../../docs/vault-container-enterprise-license.md`](../../docs/vault-container-enterprise-license.md).

## Notes

- Storage backend is always Raft — a standalone node can grow into an HA
  cluster later without a storage migration.
- Re-runs are idempotent: existing init material is reused from disk; the
  container restarts only when its config actually changes.
- A restarted node comes up **sealed** by default. Bring it back
  non-destructively with `--tags unseal`, or set
  `hashicorp_vault_auto_unseal: true` for a boot-time systemd unseal unit
  (this places the unseal key on every node).
