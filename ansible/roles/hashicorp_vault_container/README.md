# hashicorp_vault_container

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
`hashicorp_vault` (the native role). It reuses proven *config*
(container uid mapping, `SKIP_SETCAP/SKIP_CHOWN`, Raft/TLS listener shape) but
shares no code.

## What it does

1. **preflight** — assert the data mount is a real mountpoint, Docker Compose is
   present, and derive the scaling facts (`hashicorp_vault_is_ha`, first node, scheme).
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

Two opt-in (`never`-gated) tags refine this: **`backup_now`** forces an
on-demand snapshot and fails the run on error (for nightly CI); **`restore`**
rolls back to a snapshot (destructive).

## Prerequisites (composed at the playbook level)

Because the role consumes a **pre-mounted** disk and an **already-installed**
Docker, wire these ahead of it (it asserts both and fails fast otherwise):

- **A persistent second disk mounted at `hashicorp_vault_data_mount`** — e.g. via the
  universal [`storage`](../storage/README.md) role.
- **Docker engine + compose plugin** — e.g. via the [`docker`](../docker) role.

## Minimal usage

```yaml
- name: Deploy containerized HashiCorp Vault (auto-scaling)
  hosts: vault_container          # 1 host -> standalone; 3 hosts -> Raft HA
  become: true
  roles:
    - role: storage               # provision + mount the second disk
    - role: docker                # container engine + compose plugin
    - role: hashicorp_vault_container
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

hashicorp_vault_data_mount: /opt/vault
hashicorp_vault_tls_extra_sans:
  - "DNS:vault.dev.example.com"
```

Run everything, or a single phase for fast iteration:

```bash
```

> **Do not `--limit` a subset during deploy/init.** Topology is derived from the
> play's hosts, so a limited run would misconfigure Raft. Target the whole group,
> or pin `hashicorp_vault_nodes` explicitly in inventory.

## Key variables

See `defaults/main.yml` (full list) and `meta/argument_specs.yml` (contract).

| Variable | Default | Purpose |
|---|---|---|
| `hashicorp_vault_image` | `hashicorp/vault:1.19` | Container image |
| `hashicorp_vault_data_mount` | `/opt/vault` | **Second-disk mountpoint** (all state under it) |
| `hashicorp_vault_require_mounted` | `true` | Fail unless it is a real mountpoint |
| `hashicorp_vault_nodes` | play's hosts | Cluster members (auto-scales) |
| `hashicorp_vault_advertise_addr` | `{{ ansible_host }}` | Peer/client address (IP by default) |
| `hashicorp_vault_api_port` / `_cluster_port` | `8200` / `8201` | Listener ports |
| `hashicorp_vault_key_shares` / `_threshold` | `1` / `1` | Unseal key sharing |
| `hashicorp_vault_tls_enabled` | `true` | Serve TLS |
| `hashicorp_vault_tls_generate` | `true` | Self-sign a CA + cert |
| `hashicorp_vault_tls_extra_sans` | `[]` | Extra SANs (FQDNs/VIPs) |
| `hashicorp_vault_domain` | `""` | Adds `<host>.<domain>` to SANs |

## Scaling the cluster (grow / shrink)

Topology follows the hosts in the play (`hashicorp_vault_nodes`, default
`ansible_play_hosts_all`). Preflight enforces an **odd** count; `verify.yml`
asserts that Raft membership exactly matches the desired set (no missing nodes,
no stale peers) on **every** run — standalone included.

**Grow (1 → 3):** add the two new hosts to the group and re-run the playbook.
Keep the original node **first** in the host list — `init` inspects the first
node, sees the cluster is already initialised, and skips init; the two new nodes
`retry_join`, inherit the Shamir seal, and are unsealed as followers. (The
original node restarts once to pick up the new `retry_join` config and is
re-unsealed in the same run.)

> ⚠️ Do **not** let a new host sort ahead of the initialised one. If the first
> node in `hashicorp_vault_nodes` is uninitialised, `init` would run `operator init`
> again and create a second, conflicting cluster. Append new hosts.

**Shrink (3 → 1):** Raft does not auto-evict a node just because it left the
play — a removed host lingers as a **ghost peer** that still counts toward
quorum. Removal is a deliberate, `never`-gated step:

```bash
# 1. Drop the retiring hosts from the group (survivors only in the play).
# 2. While the cluster STILL HAS A LEADER (before powering the old nodes off):
ansible-playbook -i inventories/<env>/hosts.yml \
# 3. Decommission the retired VMs. A normal run now verifies clean.
```

`--tags remove_peers` evicts every Raft member not in `hashicorp_vault_nodes` and
re-checks. It runs from the first surviving node — Vault forwards the eviction to
the active leader, so it works even when the current leader is one of the nodes
being removed (that just triggers a leadership transfer). It never removes a node
still in the play. If you skip it, the next normal run **fails** in `verify.yml`
with the exact `raft remove-peer` commands to run.

> remove-peer needs the surviving cluster up and holding quorum. Always run it
> **before** powering the retiring nodes off — if you lose quorum first, recovery
> is a `peers.json` operation, which is out of scope for this tag.

## Secrets

The unseal key(s) and root token are written to
`<hashicorp_vault_keys_dir>/vault_init.json` (0400) **on the first node's persistent
disk**. Move them into your out-of-band secret store per site policy — this role
does not push them anywhere. Tasks handling them use `no_log: true`.

## Backup & restore

The role installs a **scheduled Raft snapshot backup** (the correct backup for a
Raft-backed Vault — not a copy of `/vault/data`). A systemd timer runs on every
node; only the **active leader** actually snapshots (checked via `is_self`), so
exactly one snapshot is produced per schedule. The snapshot is taken inside the
container and `docker cp`'d out to `hashicorp_vault_backup_dir` on the host, so
container-user / NFS-squash permissions never block it. Old snapshots are pruned
by age.

```yaml
# defaults — override in inventory
hashicorp_vault_backup_enabled: true
hashicorp_vault_backup_dir: /opt/vault/backups        # the "given path"
hashicorp_vault_backup_schedule: "*-*-* 02:00:00"      # systemd OnCalendar (daily 02:00)
hashicorp_vault_backup_retention_days: 7
```

**NFS backup target** (the common work pattern — mount NFS as the backup
location). Set both vars and point the backup dir at a dedicated mountpoint; the
role installs `nfs-utils` and mounts it (`nofail`) before backing up:

```yaml
hashicorp_vault_backup_dir: /mnt/vault-backups
hashicorp_vault_backup_nfs_server: "nfs.example.com"
hashicorp_vault_backup_nfs_export: "/exports/vault-backups"
```

Run just the backup setup: `--tags backup`. Check status:
`systemctl list-timers vault-container-backup.timer`.

**Force a backup on demand (nightly CI/CD)** — `--tags backup_now` triggers the
already-deployed `vault-container-backup.service` **synchronously** on every node
and returns non-zero if the leader's snapshot fails, so a scheduled pipeline goes
red and the failure is visible each morning. It reuses the exact scheduled script
(leader snapshots, standbys skip), so a forced run == the 02:00 timer run. The
unit must already be deployed (`--tags backup` once first).

```bash
# force a snapshot NOW and fail the play on error
ansible-playbook -i inventories/<env>/hosts.yml \
```

```yaml
# GitLab scheduled pipeline (nightly)
vault-backup:
  script:
    - ansible-playbook -i inventories/$ENV/hosts.yml
```

Under the hood you can still trigger it directly with
`systemctl start vault-container-backup.service` on any node (only the leader
acts), but the tag adds the assert + loud CI failure.

**Restore** is destructive and **opt-in** (gated by the `never` tag):

Use the **`vault_container` platform playbook** so `--tags` only touches this
role (the L0_lab E2E playbook also carries vsphere_vm/baseline/storage, whose
`always`-tasks would fire under a tag filter):

```bash
# newest snapshot ACROSS ALL NODES:
ansible-playbook -i inventories/<env>/hosts.yml \
# a specific snapshot (absolute path on the leader):
  -e hashicorp_vault_restore_snapshot=/opt/vault/backups/vault-snapshot-20260707-020000.snap
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
> `hashicorp_vault_backup_dir` at a shared/NFS location (see above) so a lost node
> doesn't take its snapshots with it.

## Multi-tenant policies & LDAP (optional)

Two optional, **data-driven** phases turn the cluster into a multi-tenant store
where a user sees only their tenant's slice of their environment.

**Policies** (`--tags policies`, gated by `hashicorp_vault_manage_policies`). The
**mount is the isolation domain**: each `{tenant, env}` in `hashicorp_vault_tenants`
gets its own KV v2 mount `kv-<tenant>-<env>` and a policy `<tenant>-<env>` granting
the **whole mount** — so a tenant is offboarded with one
`vault secrets disable kv-<tenant>-<env>`, and the path grammar
(`<type>/<service>/<context>`) inside stays tenant-agnostic. Tenant and env names
**must be hyphen-free** (`^[a-z0-9]+$`) or the mount name is ambiguous (preflight
asserts it). The role ensures each `kv-<tenant>-<env>` mount exists. Extra
non-tenant policies come from `hashicorp_vault_policies` (templates under
`templates/policies/`; a generic `admin.hcl.j2` is bundled).

```yaml
hashicorp_vault_manage_policies: true
hashicorp_vault_tenants:
  - {tenant: acme,   env: prod}   # -> mount kv-acme-prod,  policy acme-prod
  - {tenant: globex, env: dev}    # -> mount kv-globex-dev, policy globex-dev
```

**LDAP** (`--tags ldap`, gated by `hashicorp_vault_ldap_enabled`). Enables + configures
the LDAP auth method (FreeIPA here) and maps groups to policies. Each tenant's
LDAP group — `vault-<tenant>-<env>` by default (`hashicorp_vault_ldap_group_pattern`) —
maps to `[default, <tenant>-<env>]`, so a directory user in `vault-acme-prod` gets
exactly the `acme-prod` policy. Non-tenant groups go in `hashicorp_vault_ldap_extra_groups`.
The bind password is resolved **declared-var-first** (`hashicorp_vault_ldap_bindpass`),
falling back to HashiCorp Vault (`hashicorp_vault_ldap_bindpass_vault_path`), and is
passed to Vault over stdin (never argv).

```yaml
hashicorp_vault_ldap_enabled: true
hashicorp_vault_ldap_url: "ldaps://10.0.10.11"
hashicorp_vault_ldap_binddn: "uid=admin,cn=users,cn=accounts,dc=example,dc=au"
hashicorp_vault_ldap_bindpass_vault_path: "kv-ops/data/platform/freeipa/runtime:admin_password"
hashicorp_vault_ldap_userdn:  "cn=users,cn=accounts,dc=example,dc=au"
hashicorp_vault_ldap_groupdn: "cn=groups,cn=accounts,dc=example,dc=au"
```

Result: `vault login -method=ldap username=<user>` gives a token carrying only
that user's tenant policies — proven with tenant-isolation tests (an `acme-prod`
user cannot read `globex` or any other environment).

## Operator, automation & audit entities (optional)

Three more **data-driven**, off-by-default phases let this role stand up the
common auth entities without a separate role. All are idempotent and, like the
policy/LDAP phases, run once from the leader via `docker exec` (root token in the
process env, secrets over stdin). Keep the **data** (accounts, passwords, role
names) in a **vaulted inventory** — never in role code.

```yaml
# Human operators (--tags userpass). Upserted every run; password over stdin.
hashicorp_vault_userpass_accounts:
  - {username: alice, password: "{{ vaulted_alice_pw }}", policies: [default, ops-admin]}

# Automation AppRoles (--tags approle). Roles created; role_id/secret_id retrieval
# is out of band (they are credentials — the role never writes secret_ids to disk).
hashicorp_vault_approles:
  - {role_name: svc-ansible, policies: [svc-automation], token_ttl: 4h, token_max_ttl: 12h}

# File audit device (--tags audit). Writes under /vault/logs (persisted on disk).
hashicorp_vault_audit_enabled: true
```

**Dict keys name the Vault object** (unambiguous by design): `policy_name` /
`ldap_username` / `ldap_group` / `role_name` / `username` / `identity_group_name`
/ `tenant`. Everything else keeps Vault's own field names (`policies`,
`token_ttl`, …) so each dict maps 1:1 to what lands in Vault.

## Human SSO: LDAP / FreeIPA (not GitLab OIDC)

Human login for this role is **LDAP against FreeIPA** (`--tags ldap`). GitLab
OIDC was intentionally removed — do not reintroduce it. Operators log in with:

```bash
vault login -method=ldap username=<uid>
```

See the **Multi-tenant policies & LDAP** section above for FreeIPA bind/group
config (`hashicorp_vault_ldap_*`). Use `hashicorp_vault_ldap_users` and/or
group mappings (`hashicorp_vault_ldap_extra_groups`, tenant groups) for RBAC.

## PKI secrets engine (`--tags pki`)

**Enabled by default** (`hashicorp_vault_pki_enabled: true`). Converge-safe:

1. enable `pki/` if the mount is absent
2. tune `max_lease_ttl` only on drift (default 87600h = 10y)
3. upsert `hashicorp_vault_pki_roles` (full-replace per role; empty = mount only)

```yaml
hashicorp_vault_pki_enabled: true
hashicorp_vault_pki_mount: "pki"
hashicorp_vault_pki_max_lease_ttl_hours: 87600
hashicorp_vault_pki_roles:
  - name: int-example-org
    config:
      allowed_domains: ["int.example.org"]
      allow_subdomains: true
      allow_wildcard_certificates: true
      max_ttl: "8760h"
```

**Not included here:** intermediate issuer lifecycle
(`intermediate/generate/internal` → external cold-root sign → `set-signed`).
That path can re-key the mount if mis-gated and is owned by
[`vault_pki`](../vault_pki/README.md) + [`playbooks/25_plat_pki.yml`](../../playbooks/25_plat_pki.yml)
(`--tags vault_intermediate`). Use this role to get the engine online; use
`vault_pki` when you have a cold root ready to sign the intermediate CSR.

### Import a pre-signed issuing CA (`--tags pki_issuer`)

**Off by default** (`hashicorp_vault_pki_issuer_import: false`). When an offline
ceremony has **already signed** an issuing CA against your cold root, this phase
*adopts* that issuer into the `pki/` mount — no CSR round-trip, no re-key. It runs
**after** the `pki` phase (the mount must exist) and is idempotent + drift-only, so
it is safe on every converge once enabled.

The escrow (issuing cert, its **private key**, and the public **root** cert) is
produced elsewhere and stored **Ansible-Vault-encrypted** in inventory. Map it onto
these role-prefixed vars — the role never references the ceremony's own names:

```yaml
hashicorp_vault_pki_issuer_import: true
hashicorp_vault_pki_issuer_cert: "{{ vaulted_issuing_ca_cert }}"   # issuing CA PEM cert
hashicorp_vault_pki_issuer_key:  "{{ vaulted_issuing_ca_key }}"    # issuing CA PEM key (unencrypted)
hashicorp_vault_pki_root_cert:   "{{ vaulted_root_ca_cert }}"      # public root PEM cert
# optional AIA / CRL distribution points (drift only; only the keys you set are touched):
hashicorp_vault_pki_urls:
  issuing_certificates:    ["https://vault.{{ env }}.{{ domain }}:8200/v1/pki/ca"]
  crl_distribution_points: ["https://vault.{{ env }}.{{ domain }}:8200/v1/pki/crl"]
```

What it does, in order:

1. **import the issuing CA** (cert **+** key) as one `pem_bundle` → the mount gains a
   signer. The key is passed to Vault **only over stdin** (`docker exec -i`,
   `pem_bundle=-`), never on argv, under `no_log`.
2. **import the root CA** (cert only) as a `pem_bundle` → the mount can build the full
   `ca_chain` up to the root.
3. **set the default issuer** to the imported issuing CA — **only when it differs**
   (drift-only write).
4. **configure AIA/CRL URLs** from `hashicorp_vault_pki_urls` — **only on drift**, and
   only for the keys you declare.
5. **self-verify** — read the default issuer's certificate back and assert its
   **sha256 fingerprint** equals the fingerprint of `hashicorp_vault_pki_issuer_cert`;
   fails with an actionable message otherwise.

Idempotency comes from the import responses: `imported_issuers` / `imported_keys`
being **empty** means the bundle was already present, so a re-run reports no change.
Enabling the toggle with any of cert/key/root empty **fails fast**, naming the
missing var.

## GitLab CI JWT & Transit signing (optional)

Off-by-default, data-driven phases (same idiom: idempotent, run once from the
leader via `docker exec`, secrets over stdin) cover **CI** and image-signing —
not human SSO:

```yaml
# GitLab CI JWT->token exchange (--tags gitlab_jwt). Roles are data.
hashicorp_vault_gitlab_base_url: "https://gitlab.mgt.example.com"
hashicorp_vault_gitlab_jwt_enabled: true
hashicorp_vault_gitlab_jwt_roles:
  - {name: myproject, bound_claims: {project_id: "42", ref: main}, policies: [svc-ci-prod]}

# Transit engine + Cosign signing key (--tags transit). The private half never
# leaves Vault; export_public_key publishes the public half to KV for Kyverno.
hashicorp_vault_transit_enabled: true
hashicorp_vault_transit_keys:
  - name: cosign
    type: ecdsa-p256
    export_public_key: {mount: kv-ops, path: platform/cosign/runtime}
```

`bound_claims` is passed as single-quoted JSON so the CLI parses it per-key.
The Transit public-key export is a **full PUT** of a dedicated KV path
(`public_key`/`key_type`/`key_version`/`transit_path`).

### RBAC tiers & identity groups (scalable, optional)

Three policy tiers, least-privilege by default:

```yaml
hashicorp_vault_tenants:
  - {tenant: example, env: prod}              # tier-1: example-prod  -> kv-example-prod/*
  - {tenant: example, env: dev, wide: true}   # tier-2: + example     -> kv-example-*  (all envs)
hashicorp_vault_policies:
  - {policy_name: vault-superadmin, template: all-kv.hcl.j2}   # tier-3: kv-* (every mount)
```

**Identity groups** (`--tags identity`) are the RBAC layer over LDAP: an
**external** group maps a FreeIPA LDAP group to policies; an **internal** group
can **nest** others for indirect membership (an "admin-all" that inherits every
tenant group):

```yaml
hashicorp_vault_identity_groups:
  - {identity_group_name: example-prod-admins, ldap_group: vault-example-prod, policies: [example-prod]}
  - identity_group_name: platform-superadmins        # internal, nests the tenant groups
    type: internal
    policies: [vault-superadmin]
    member_group_names: [example-prod-admins]
```

Named policies referenced above are supplied through `hashicorp_vault_policies`
(templates under `templates/policies/`) — bundled: `admin.hcl.j2` (CRUD on
`_admin_kv_mounts`) and `all-kv.hcl.j2` (tier-3 `kv-*`).

> Deferred to the production-parity effort (needs live validation): GitLab CI
> JWT, Transit/Cosign, and PKI intermediate import (engine mount is enabled by
> default; issuer lifecycle stays on `vault_pki`). Until then this role covers
> deploy + HA + TLS + backup + policies + LDAP/FreeIPA + userpass + approle +
> pki mount + audit — a complete standalone Vault for new deployments.

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
  ```

  For hands-off recovery, set **`hashicorp_vault_auto_unseal: true`** — the role ships a
  boot-time systemd unit (`vault-container-unseal.service`) that unseals each node
  automatically after a reboot. This stores the unseal key on **every** node;
  toggling the flag back to `false` disables the unit and removes those keys. For
  a stricter posture keep it off, or move to transit/KMS auto-unseal (keys never
  on the node) — out of scope for this role.
