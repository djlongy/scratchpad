# hashicorp_vault_container

## TL;DR

Deploys HashiCorp Vault as a Docker container with all state on a persistent
second disk. Topology auto-scales from the hosts in the play: 1 host yields
standalone Raft, an odd N ≥ 3 yields a Raft HA cluster.

```bash
ansible-playbook -i inventories/<env>/hosts.yml playbooks/vault_cluster.yml
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

## Minimum configuration

```yaml
# group_vars/hashicorp_vault_container_hosts.yml
---
# Required
hashicorp_vault_data_mount: "/opt/hashicorp"
hashicorp_vault_nodes: "{{ groups['hashicorp_hosts'] }}"
```

## Usage

```yaml
- name: Deploy containerised HashiCorp Vault cluster
  hosts: vault_servers            # 1 host -> standalone; odd N -> Raft HA
  roles:
    - role: storage                # provision + mount the second disk
    - role: docker                 # container engine + compose plugin
    - role: hashicorp_vault_container
```

Run:

```bash
ansible-playbook -i inventories/<env>/hosts.yml playbooks/vault_cluster.yml
# fast iteration on a single phase:
ansible-playbook -i inventories/<env>/hosts.yml playbooks/vault_cluster.yml --tags policies,ldap
```

## Preconditions

The role consumes a **pre-mounted** disk and an **already-installed** Docker
engine — it asserts both in preflight and fails fast otherwise:

- A persistent second disk mounted at `hashicorp_vault_data_mount` (e.g. via
  the `storage` role).
- Docker engine + compose plugin (e.g. via the `docker` role).
- Only odd node counts are valid (1, 3, 5…) — preflight refuses an even
  count (2-node Raft quorum has no fault tolerance). Grow/shrink 1 ↔ 3
  directly. Do not `--limit` a subset during `deploy`/`init` — topology is
  derived from the play's hosts; pin `hashicorp_vault_nodes` explicitly if
  you need a stable set.

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

## Behaviour

- **Idempotency** — storage backend is always Raft (a standalone node can
  grow into an HA cluster later without a storage migration); re-runs reuse
  existing init material from disk, and the container restarts only when its
  config actually changes.
- **Reboot / unseal** — a restarted node comes up **sealed** by default.
  Bring it back non-destructively with `--tags unseal` (also runs under
  `init`), or set `hashicorp_vault_auto_unseal: true` for a boot-time
  systemd unseal unit (places the unseal key on every node).
- **Scaling** — topology follows `hashicorp_vault_nodes`. **Grow (1 → 3):**
  add the new hosts to the group, keeping the original node first, and
  re-run — `init` sees the cluster already initialised and skips it; new
  nodes `retry_join` and unseal as followers. **Shrink (3 → 1):** drop the
  retiring hosts from the group while the cluster still has a leader, then
  run `--tags remove_peers` (opt-in, `never`-tagged) to evict them from Raft
  membership before powering them off — a normal run otherwise fails in
  `verify.yml` with the exact `raft remove-peer` commands needed.
- **Backup & restore** — a systemd timer runs on every node; only the active
  leader snapshots (checked via `is_self`), so exactly one snapshot is
  produced per schedule. The snapshot is taken inside the container and
  `docker cp`'d to `hashicorp_vault_backup_dir` on the host. Backups
  authenticate with a scoped periodic token (policy: read on
  `sys/storage/raft/snapshot`); restore uses the root token, only from the
  first node. `--tags backup_now` (opt-in, `never`-tagged) forces a snapshot
  now and fails the play on error; `--tags restore` (opt-in, `never`-tagged,
  destructive) rolls back the newest snapshot across all nodes.

  ```yaml
  hashicorp_vault_backup_dir: /mnt/vault-backups        # point off-node for real HA DR
  hashicorp_vault_backup_nfs_server: "nfs.example.com"
  hashicorp_vault_backup_nfs_export: "/exports/vault-backups"
  ```
- **Auth & secrets phases** — each phase is independent unless noted; enable
  only what a given token needs.

  | Who / what | Auth method | Gate variable |
  |---|---|---|
  | Human (directory SSO) | LDAP (FreeIPA) | `hashicorp_vault_ldap_enabled` |
  | Human (local break-glass) | userpass | `hashicorp_vault_userpass_accounts` (non-empty) |
  | Automation | AppRole | `hashicorp_vault_approles` (non-empty) |
  | GitLab CI job | JWT | `hashicorp_vault_gitlab_jwt_enabled` |

  **Policies** (`manage_policies`) — the KV mount is the isolation domain:
  each `{tenant, env}` pair gets its own `kv-<tenant>-<env>` mount and a
  policy of the same name. `wide: true` on any row also writes a tenant-wide
  policy.

  **LDAP** (`ldap_enabled`) — configures `auth/ldap` against FreeIPA and
  auto-maps each tenant to `auth/ldap/groups/vault-<tenant>-<env>`. Does not
  create FreeIPA groups — they must already exist.

  **Identity groups** (non-empty `identity_groups`) — a second RBAC layer on
  top of LDAP: `external` groups alias an LDAP group, `internal` groups nest
  other Identity groups. Requires LDAP for external aliases. Pick one primary
  grant path per FreeIPA group (LDAP-direct or Identity), not both.

  **PKI** (`pki_enabled`, on by default) — mounts `pki/`, tunes
  `max_lease_ttl`, and upserts issuing roles. Does not generate or import an
  intermediate issuer; `pki_issuer_import` (off by default) adopts an
  escrowed, pre-signed issuing CA (cert + key + root, verified by
  fingerprint).

  **Transit** (`transit_enabled`) — enables the Transit engine and creates
  signing keys (e.g. Cosign); the private half never leaves Vault.

  **Audit** (`audit_enabled`) — enables the file audit device under
  `/vault/logs`, persisted on the data mount.

  Copy-paste inventory snippets for LDAP groups, Identity nesting, and
  CI/AppRole add-ons live under `examples/`.
- **Enterprise license** (`--tags license`, opt-in) — off by default
  (`hashicorp_vault_license_enabled: false`); Community images run
  unchanged, no license material required. Vault Enterprise autoloads a
  license from the first match of `VAULT_LICENSE` (raw env string) →
  `VAULT_LICENSE_PATH` (env file path) → `license_path` (HCL); this role
  uses the last two, pointing at `license.hclic` under the bind-mounted
  config dir — never the raw-string env, which would leak via
  `docker inspect`.

  ```yaml
  hashicorp_vault_license_enabled: true
  # Enterprise Hub tags ALWAYS carry the -ent suffix — bare version tags do not exist
  hashicorp_vault_image: "hashicorp/vault-enterprise:1.19.5-ent"
  hashicorp_vault_license: "{{ vaulted_vault_enterprise_license }}"   # vaulted inventory
  # or a controller-side file: hashicorp_vault_license_src: "/secure/path/vault.hclic"
  ```

  Guard rails: **offline validation before install**
  (`hashicorp_vault_license_validate`, default `true`) runs
  `vault license inspect` in a throwaway container, so a mangled paste or
  expired key fails before anything is installed or restarted; **hot reload
  on renewal** applies a changed license file per node via
  `sys/config/reload/license` with no restart/seal (first enable — an image
  swap — recreates the container instead; follow with `--tags unseal`); the
  **verify phase** runs `vault license get` and fails unless the running
  binary reports an **autoloaded** license (file-in-place ≠
  license-in-effect), then prints the expiry.

## Expected result

- A successful run leaves every node unsealed and, in HA, every peer joined
  to Raft — asserted automatically (`verify` phase); spot-check manually
  with `vault status`.
- `--tags renew_drill` (opt-in, `never`-tagged, `hashicorp_vault_tls_onhost`
  only) proves the certmonger TLS-renewal path today instead of discovering
  it broken ~90 days out: forces a re-issue and asserts a new certificate
  serial was issued, published, and Vault reloaded via SIGHUP without
  sealing.
