# vault_container

Self-contained, **auto-scaling** containerized HashiCorp Vault. Runs Vault as a
Docker container with **all state on a persistent second disk**, and derives its
topology from the hosts in the play:

| Hosts in play | Result |
|---|---|
| **1** | Standalone Raft (single server) — no `retry_join`, ready to grow later |
| **N (odd ≥ 3)** | Raft HA cluster — every node `retry_join`s all peers |

> **Only odd node counts are valid** (1, 3, 5, 7…). Preflight **refuses an even
> count** — with N=2, Raft quorum is 2, so a single node failure loses quorum and
> Vault stops serving writes (worse than standalone). Grow/shrink **1 ↔ 3**
> directly; never pause on 2. See *Scaling the cluster* below.

This role is **fully self-contained**: it imports **no** tasks from
`hashicorp_vault` or `hashicorp_vault_docker`. It reuses proven *config*
(container uid mapping, `SKIP_SETCAP/SKIP_CHOWN`, Raft/TLS listener shape) but
shares no code.

## TL;DR

**Most common: converge/deploy the cluster.** Idempotent — put the members in the `vault_container` group and run the whole group (topology is derived from the play's hosts, so never `--limit` a subset). Day-2: add tenant policies with `--tags policies`.

```bash
ansible-playbook -i inventories/<env>/hosts.yml playbooks/L3_platform/vault_container.yml
ansible-playbook -i inventories/<env>/hosts.yml playbooks/L3_platform/vault_container.yml --tags policies
```

## What it does

1. **preflight** — assert the data mount is a real mountpoint, Docker Compose is
   present, and derive the scaling facts (`vault_ctr_is_ha`, first node, scheme).
2. **prereqs** — create state directories on the disk, open firewall ports.
3. **tls** — self-sign a CA + multi-SAN server cert on the controller (or use
   your own), distribute to every node.
4. **deploy** — template `vault.hcl` + `docker-compose.yml`, start the container,
   wait for the API.
5. **init** — initialize once, unseal the leader, then unseal followers so they
   join the Raft cluster. Unseal key(s) + root token are written to
   `<keys_dir>/vault_init.json` (mode 0400) on the first node.
6. **verify** — confirm every node is unsealed and (HA) all peers joined.
7. **backup** — install a systemd timer that takes scheduled Raft snapshots
   (leader-only) to a local or NFS-mounted path, with age-based retention.

## Prerequisites (composed at the playbook level)

Because the role consumes a **pre-mounted** disk and an **already-installed**
Docker, wire these ahead of it (it asserts both and fails fast otherwise):

- **A persistent second disk mounted at `vault_ctr_data_mount`** — e.g. via the
  universal [`storage`](../storage/README.md) role.
- **Docker engine + compose plugin** — e.g. via the [`docker`](../docker) role.

## Minimal usage

```yaml
# playbooks/L3_platform/vault_container.yml
- name: Deploy containerized HashiCorp Vault (auto-scaling)
  hosts: vault_container          # 1 host -> standalone; 3 hosts -> Raft HA
  become: true
  roles:
    - role: storage               # provision + mount the second disk
    - role: docker                # container engine + compose plugin
    - role: vault_container
```

```yaml
# inventories/<env>/group_vars/vault_container.yml
# The second disk the `storage` role provisions and mounts at /opt/vault:
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

vault_ctr_data_mount: /opt/vault
vault_ctr_tls_extra_sans:
  - "DNS:vault.dev.example.com"
```

Run everything, or a single phase for fast iteration:

```bash
ansible-playbook -i inventories/dev/hosts.yml playbooks/L3_platform/vault_container.yml
ansible-playbook ... playbooks/L3_platform/vault_container.yml --tags deploy
ansible-playbook ... playbooks/L3_platform/vault_container.yml --list-tags
```

> **Do not `--limit` a subset during deploy/init.** Topology is derived from the
> play's hosts, so a limited run would misconfigure Raft. Target the whole group,
> or pin `vault_ctr_nodes` explicitly in inventory.

## Key variables

See `defaults/main.yml` (full list) and `meta/argument_specs.yml` (contract).

| Variable | Default | Purpose |
|---|---|---|
| `vault_ctr_image` | `hashicorp/vault:1.19` | Container image |
| `vault_ctr_data_mount` | `/opt/vault` | **Second-disk mountpoint** (all state under it) |
| `vault_ctr_require_mounted` | `true` | Fail unless it is a real mountpoint |
| `vault_ctr_nodes` | play's hosts | Cluster members (auto-scales) |
| `vault_ctr_advertise_addr` | `{{ ansible_host }}` | Peer/client address (IP by default) |
| `vault_ctr_api_port` / `_cluster_port` | `8200` / `8201` | Listener ports |
| `vault_ctr_key_shares` / `_threshold` | `1` / `1` | Unseal key sharing |
| `vault_ctr_tls_enabled` | `true` | Serve TLS |
| `vault_ctr_tls_generate` | `true` | Self-sign a CA + cert |
| `vault_ctr_tls_extra_sans` | `[]` | Extra SANs (FQDNs/VIPs) |
| `vault_ctr_domain` | `""` | Adds `<host>.<domain>` to SANs |

## Scaling the cluster (grow / shrink)

Topology follows the hosts in the play (`vault_ctr_nodes`, default
`ansible_play_hosts_all`). Preflight enforces an **odd** count; `verify.yml`
asserts that Raft membership exactly matches the desired set (no missing nodes,
no stale peers) on **every** run — standalone included.

**Grow (1 → 3):** add the two new hosts to the group and re-run the playbook.
Keep the original node **first** in the host list — `init` inspects the first
node, sees the cluster is already initialised, and skips init; the two new nodes
`retry_join`, inherit the Shamir seal, and are unsealed as followers.

> ⚠️ Do **not** let a new host sort ahead of the initialised one. If the first
> node in `vault_ctr_nodes` is uninitialised, `init` would run `operator init`
> again and create a second, conflicting cluster. Append new hosts.

**Shrink (3 → 1):** Raft does not auto-evict a node just because it left the
play — a removed host lingers as a **ghost peer** that still counts toward
quorum. Removal is a deliberate, `never`-gated step:

```bash
# 1. Drop the retiring hosts from the group (survivors only in the play).
# 2. While the cluster STILL HAS A LEADER (before powering the old nodes off):
ansible-playbook -i inventories/<env>/hosts.yml \
  playbooks/L3_platform/vault_container.yml --tags remove_peers
# 3. Decommission the retired nodes. A normal run now verifies clean.
```

`--tags remove_peers` evicts every Raft member not in `vault_ctr_nodes` and
re-checks. It runs from the first surviving node — Vault forwards the eviction to
the active leader, so it works even when the current leader is one of the nodes
being removed. It never removes a node still in the play. If you skip it, the next
normal run **fails** in `verify.yml` with the exact `raft remove-peer` commands.

> To re-add a node that was previously evicted, wipe its stale Raft data
> (`/opt/vault/data/*`, keeping `keys`/`config`/`certs`) before the grow run — a
> removed peer stays sealed with old state and will not rejoin otherwise.

## Secrets

The unseal key(s) and root token are written to
`<vault_ctr_keys_dir>/vault_init.json` (0400) **on the first node's persistent
disk**. Move them into your out-of-band secret store per site policy — this role
does not push them anywhere. Tasks handling them use `no_log: true`.

## Backup & restore

The role installs a **scheduled Raft snapshot backup** (the correct backup for a
Raft-backed Vault — not a copy of `/vault/data`). A systemd timer runs on every
node; only the **active leader** actually snapshots (checked via `is_self`), so
exactly one snapshot is produced per schedule. The snapshot is taken inside the
container and `docker cp`'d out to `vault_ctr_backup_dir` on the host, so
container-user / NFS-squash permissions never block it. Old snapshots are pruned
by age.

```yaml
# defaults — override in inventory
vault_ctr_backup_enabled: true
vault_ctr_backup_dir: /opt/vault/backups        # the "given path"
vault_ctr_backup_schedule: "*-*-* 02:00:00"      # systemd OnCalendar (daily 02:00)
vault_ctr_backup_retention_days: 7
```

**NFS backup target** (the common work pattern — mount NFS as the backup
location). Set both vars and point the backup dir at a dedicated mountpoint; the
role installs `nfs-utils` and mounts it (`nofail`) before backing up:

```yaml
vault_ctr_backup_dir: /mnt/vault-backups
vault_ctr_backup_nfs_server: "nfs.example.com"
vault_ctr_backup_nfs_export: "/exports/vault-backups"
```

Run just the backup setup: `--tags backup`. Trigger a snapshot immediately:
`systemctl start vault-container-backup.service` on any node (only the leader
acts). Check status: `systemctl list-timers vault-container-backup.timer`.

**Restore** is destructive and **opt-in** (gated by the `never` tag):

Use the **`vault_container` platform playbook** so `--tags` only touches this
role (the L0_lab E2E playbook also carries vsphere_vm/baseline/storage, whose
`always`-tasks would fire under a tag filter):

```bash
# newest snapshot ACROSS ALL NODES:
ansible-playbook -i inventories/<env>/hosts.yml \
  playbooks/L3_platform/vault_container.yml --tags restore
# a specific snapshot (absolute path on the leader):
ansible-playbook ... playbooks/L3_platform/vault_container.yml --tags restore \
  -e vault_ctr_restore_snapshot=/opt/vault/backups/vault-snapshot-20260707-020000.snap
```

Restore applies the snapshot on the **active leader** with `-force`; Vault
replaces the data in place and replicates it to the followers automatically —
no follower wipe, restart or re-unseal (those would break quorum). `"latest"`
is resolved **across every node** (not just the leader) and staged onto the
leader, so a leadership flap-back can't restore a stale local snapshot. It
targets **same-cluster rollback** (identical membership + unseal keys, which is
what the scheduled snapshots produce). Restoring a snapshot from a *different*
cluster is a separate migration workflow — out of scope here.

**Auth:** backups use a **scoped periodic token** (policy: `read` on
`sys/storage/raft/snapshot`) minted on the leader and distributed 0400 to each
node; the script renews it each run and the backup phase re-mints it if it's
gone (self-heals after a rollback to a pre-mint snapshot — re-run `--tags
backup` afterwards). Only **restore** uses the root token, and only from the
first node. No root token is spread across the cluster.

> **HA backups belong off-node.** The default backup dir is per-node local,
> which is fine for single-node but is not real DR for a cluster — point
> `vault_ctr_backup_dir` at a shared/NFS location (see above) so a lost node
> doesn't take its snapshots with it.

## Multi-tenant policies & LDAP (optional)

Two optional, **data-driven** phases turn the cluster into a multi-tenant store
where a user sees only their tenant's slice of their environment.

**Policies** (`--tags policies`, gated by `vault_ctr_manage_policies`). Environment
== a KV mount (`kv-<env>`); tenant == a path prefix inside it. Each `{tenant, env}`
in `vault_ctr_tenants` yields a policy `<tenant>-<env>` granting access to **only**
`kv-<env>/data/<tenant>/*`. The role ensures each `kv-<env>` mount exists. Extra
non-tenant policies come from `vault_ctr_policies` (templates under
`templates/policies/`; a generic `admin.hcl.j2` is bundled).

```yaml
vault_ctr_manage_policies: true
vault_ctr_tenants:
  - {tenant: acme,   env: prod}   # -> policy acme-prod   (kv-prod/data/acme/*)
  - {tenant: globex, env: dev}    # -> policy globex-dev  (kv-dev/data/globex/*)
```

**LDAP** (`--tags ldap`, gated by `vault_ctr_ldap_enabled`). Enables + configures
the LDAP auth method (FreeIPA here) and maps groups to policies. Each tenant's
LDAP group — `vault-<tenant>-<env>` by default (`vault_ctr_ldap_group_pattern`) —
maps to `[default, <tenant>-<env>]`, so a directory user in `vault-acme-prod` gets
exactly the `acme-prod` policy. Non-tenant groups go in `vault_ctr_ldap_extra_groups`.
The bind password is resolved **declared-var-first** (`vault_ctr_ldap_bindpass`),
falling back to HashiCorp Vault (`vault_ctr_ldap_bindpass_vault_path`), and is
passed to Vault over stdin (never argv).

```yaml
vault_ctr_ldap_enabled: true
vault_ctr_ldap_url: "ldaps://ldap.example.com"
vault_ctr_ldap_binddn: "uid=admin,cn=users,cn=accounts,dc=example,dc=com"
vault_ctr_ldap_bindpass_vault_path: "kv-secrets/data/platform/ldap/runtime:bindpass"
vault_ctr_ldap_userdn:  "cn=users,cn=accounts,dc=example,dc=com"
vault_ctr_ldap_groupdn: "cn=groups,cn=accounts,dc=example,dc=com"
```

Result: `vault login -method=ldap username=<user>` gives a token carrying only
that user's tenant policies — proven with tenant-isolation tests (an `acme-prod`
user cannot read `globex` or any other environment).

## Notes

- **Storage backend is always Raft** — a single node runs standalone Raft, so it
  can be grown into an HA cluster later without a storage migration.
- **Re-runs are idempotent**: existing init material is reused from the disk;
  the container is only restarted when config actually changes.
- **Reboot / restart = re-seal.** By default a restarted node comes up **sealed**.
  Bring it back **non-destructively** with `--tags unseal` (runs `unseal.yml` only —
  it never re-initialises or wipes anything):

  ```bash
  ansible-playbook -i inventories/<env>/hosts.yml \
    playbooks/L3_platform/vault_container.yml --tags unseal
  ```

  For hands-off recovery, set **`vault_ctr_auto_unseal: true`** — the role ships a
  boot-time systemd unit (`vault-container-unseal.service`) that unseals each node
  automatically after a reboot. This stores the unseal key on **every** node;
  toggling the flag back to `false` disables the unit and removes those keys. For
  a stricter posture keep it off, or move to transit/KMS auto-unseal (keys never
  on the node) — out of scope for this role.
