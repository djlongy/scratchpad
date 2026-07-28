# hashicorp_vault_container

## TL;DR

Runs Vault as a Docker container with all state on a persistent second disk, and
derives its topology from the hosts in the play: **1 host** → standalone Raft;
**N hosts (odd ≥ 3)** → Raft HA with every node `retry_join`ing all peers. Preflight
refuses an even node count (2-node Raft has no fault tolerance).

```bash
ansible-playbook -i inventories/<env>/hosts.yml playbooks/vault_cluster.yml
```

## Requirements

Install collections before running (repo `requirements.yml`, or ad-hoc):

    ansible-galaxy collection install -r requirements.yml

| Collection | When | Used for |
|---|---|---|
| `ansible.posix` | always | firewalld ports, mount facts |
| `community.crypto` | When `hashicorp_vault_tls_generate` | self-signed CA + server certificate |
| `community.hashi_vault` | When an LDAP bind password or Enterprise license path resolves from Vault | HashiCorp Vault lookups |

Control Groups (below) additionally require a **Vault Enterprise** image + an
entitled license — off by default.

## Key variables

Full list: `defaults/main.yml`. Contract: `meta/argument_specs.yml`.

**Required** = value must be correct for a successful run (defaults often work).
**Optional** = safe to leave default / empty; phase stays off or uses built-ins.
**When X** = required only if that feature is on.

| Req | Variable | Default | Purpose |
|---|---|---|---|
| **Required** | `hashicorp_vault_data_mount` | `/opt/vault` | Persistent second-disk mount; all Vault state lives under it (local disk only — see Behaviour) |
| **Required** | `hashicorp_vault_nodes` | play's hosts | Cluster members (odd count: 1, 3, 5…). Pin to ignore `--limit` |
| Optional | `hashicorp_vault_require_mounted` | `true` | Preflight fails unless `data_mount` is a real mountpoint |
| Optional | `hashicorp_vault_image` | `hashicorp/vault:1.19.5` | Container image (pin an exact tag) |
| Optional | `hashicorp_vault_advertise_addr` | `{{ ansible_host }}` | Address peers/clients use for this node |
| Optional | `hashicorp_vault_api_port` / `_cluster_port` | `8200` / `8201` | API/UI listener and Raft cluster listener |
| Optional | `hashicorp_vault_key_shares` / `_key_threshold` | `1` / `1` | Shamir unseal key shares generated / required at init |
| Optional | `hashicorp_vault_auto_unseal` | `false` | Systemd unseal-on-boot (puts the unseal key on every node) |
| When KMS unseal | `hashicorp_vault_seal_config` | `{}` | `{type, config}` seal stanza for external KMS/transit auto-unseal; non-empty skips the Shamir unseal phase and is mutually exclusive with `_auto_unseal` |
| Optional | `hashicorp_vault_backup_enabled` | `true` | Scheduled Raft snapshots |
| Optional | `hashicorp_vault_manage_firewall` | `true` | Open API + cluster ports via firewalld |
| Optional | `hashicorp_vault_tls_enabled` / `_generate` | `true` / `true` | Serve the API over TLS; self-sign CA + multi-SAN server cert on the controller |
| When TLS supplied | `hashicorp_vault_tls_ca_cert` / `_server_cert` / `_server_key` | `""` | Controller paths to supplied PEM material (when `_tls_generate: false`) |
| When TLS on-host | `hashicorp_vault_tls_onhost_cert` / `_key` / `_ca` | `""` | Cert/key/CA already on the node (certmonger/ipa-getcert); overrides `_generate` |
| When portable CA | `hashicorp_vault_tls_ca_url` | `""` | HTTPS URL (e.g. Artifactory generic repo) nodes fetch `ca.crt` from; wins over all other CA-cert sources for node trust |
| When portable CA | `hashicorp_vault_tls_ca_content` | `""` | CA certificate PEM as an inventory var (public); distributes to nodes when `_ca_url` is empty |
| When portable CA | `hashicorp_vault_tls_ca_key_content` | `""` | CA private key PEM (Ansible-Vault-encrypted var); in generate mode with `_ca_content`, reuses one stable CA to sign server certs |
| Optional | `hashicorp_vault_hcl_extra` | `""` | Raw HCL appended verbatim to `vault.hcl` for stanzas the role does not model (cluster_name, lease TTLs, replication, extra listeners) |
| When that phase is enabled | `hashicorp_vault_manage_policies`, `_ldap_enabled`, `_identity_groups`, `_userpass_accounts`, `_approles`, `_gitlab_jwt_enabled`, `_transit_enabled`, `_pki_issuer_import`, `_license_enabled`, `_audit_enabled` | Independent opt-in auth/RBAC phases — off / empty by default; see `defaults/main.yml` for each phase's full variable set |
| When LDAP | `hashicorp_vault_ldap_token_ttl` / `_token_max_ttl` | `""` (Vault default 768h) | Token lifetime for LDAP logins; shorten to narrow how long a removed group member keeps a live token |
| Optional (Enterprise, dormant) | `hashicorp_vault_control_groups_enabled` / `hashicorp_vault_control_groups` | `false` / `[]` | Approval-gated KV reads via Control Groups. Off; never runs without `--tags control_groups` on a licensed Enterprise build — see **Control Groups** below |

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
# playbooks/vault_cluster.yml
- name: Deploy containerized HashiCorp Vault (auto-scaling)
  hosts: vault_container          # 1 host -> standalone; 3 hosts -> Raft HA
  become: true
  roles:
    - role: storage               # provision + mount the second disk
    - role: docker                # container engine + compose plugin
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
  - "DNS:vault.dev.example.com"
```

Run everything, or a single phase for fast iteration:

```bash
ansible-playbook -i inventories/<env>/hosts.yml playbooks/vault_cluster.yml
ansible-playbook ... playbooks/vault_cluster.yml --tags deploy
ansible-playbook ... playbooks/vault_cluster.yml --list-tags
```

## Preconditions

- A persistent second disk already mounted at `hashicorp_vault_data_mount` (e.g.
  via the `storage` role) and Docker engine + compose plugin (e.g. via the
  `docker` role) — preflight asserts both and fails fast otherwise.
- LDAP auth maps FreeIPA groups by name; it does not create them — the groups
  referenced by `hashicorp_vault_tenants` / `_ldap_extra_groups` must already
  exist in FreeIPA.
- `pki_issuer_import` expects the escrowed cert/key/root already staged as
  Ansible-Vault-encrypted vars.

## Behaviour

- **`hashicorp_vault_data_mount` is local disk only — never NFS or another
  network filesystem.** Raft (Integrated Storage) keeps its own copy of the
  data per node; HA is consensus over the API/cluster ports, not a shared
  mount, and Raft needs local `fsync`/locking semantics NFS does not provide.
  Preflight only checks that `data_mount` is a real mountpoint — it does not
  reject NFS, so that silence is not approval. Snapshots (`hashicorp_vault_backup_dir`)
  are the supported way to get Raft data onto NFS.
- Storage backend is always Raft — a single node runs standalone Raft so it can
  be grown into HA later without a storage migration.
- Re-runs are idempotent: existing init material is reused from disk; the
  container only restarts when config actually changes.
- **LDAP KV access is granted by group membership, per mount.** A member of
  `vault-<tenant>-<env>` receives the `<tenant>-<env>` policy, which grants only
  the matching `kv-<tenant>-<env>` mount; add mappings via
  `hashicorp_vault_ldap_extra_groups` (group → policies) or authorise a single
  directory user with `hashicorp_vault_ldap_users`. Grant/revoke by changing
  FreeIPA group membership — the role reads groups by name, it does not create
  them. Revocation is bounded by the token lifetime: a removed member keeps a
  live token until it expires (default `token_max_ttl` is Vault's 768h) or is
  explicitly revoked. Set `hashicorp_vault_ldap_token_max_ttl` shorter to narrow
  that window.
- **Portable CA trust.** By default each controller self-signs its own CA, so the
  trust anchor differs per operator. To share one stable CA across controllers and
  teams: the first run generates the CA under `hashicorp_vault_tls_local_dir`; the
  operator then stores its `ca.key` as an Ansible-Vault-encrypted var
  (`ansible-vault encrypt_string`) and uploads `ca.crt` to an Artifactory generic
  repo. Subsequent runs on any controller set `hashicorp_vault_tls_ca_key_content` +
  `hashicorp_vault_tls_ca_content` (the role reuses that CA to sign server certs
  instead of minting a fresh one) and, optionally, `hashicorp_vault_tls_ca_url` so
  every node fetches the same `ca.crt` for trust. When both a URL and local
  generation are in play, the role asserts the fetched CA fingerprint matches the
  controller signer, so a wrong upload fails the run rather than the TLS handshake.
  CA-cert distribution precedence is `_ca_url` > `_ca_content` > generated/provided
  file.
- A restarted node comes up sealed by default. Bring it back with
  `--tags unseal` (non-destructive, never re-initialises). For hands-off
  recovery set `hashicorp_vault_auto_unseal: true` — this stores the unseal key
  on every node. A non-empty `hashicorp_vault_seal_config` (KMS/transit
  auto-unseal) makes the role skip the unseal phase entirely — the external seal
  unseals Vault on start — and is mutually exclusive with
  `hashicorp_vault_auto_unseal` (the role asserts this).
- **Operator custody (declared-var-first):** set `hashicorp_vault_unseal_keys`
  (list of base64 shares) and `hashicorp_vault_root_token` from Ansible Vault
  (e.g. encrypted `group_vars`). Unseal prefers the share list over on-disk
  `vault_init.json`; every management phase resolves root via
  `tasks/resolve_root_token.yml` (var → disk → assert). With
  `hashicorp_vault_persist_init: false`, unseal will not rewrite
  `root_token.txt` and removes any residual copy — disk-free management for
  seed/manual-unseal hosts.
- First init still writes `vault_init.json` (mode 0400) on the first node as a
  greenfield landing zone. Move that payload into Ansible Vault custody
  (shares + root as separate vars preferred); the role does not auto-escrow yet.
- **License hot-reload** fails closed if no root token is resolvable (declared
  var or on-disk). Previously a soft skip when the token file was missing;
  set `hashicorp_vault_root_token` or keep `persist_init: true` when using
  `--tags license`.
- **Grow (1 → 3):** add the two new hosts to the group and re-run, keeping the
  original node **first** in the host list — `init` sees the cluster is already
  initialised and skips; the new nodes `retry_join` and are unsealed as
  followers.
- **Shrink (3 → 1):** Raft does not auto-evict a departed host — it lingers as a
  ghost peer counting toward quorum. Evict it with `--tags remove_peers` while
  the cluster still has a leader, before powering old nodes off (see
  **Tag safety**). Skipping it fails the next normal run in `verify.yml` with
  the exact `raft remove-peer` commands to run.
- A systemd timer runs on every node; only the active leader snapshots (checked
  via `is_self`), so exactly one snapshot is produced per schedule. The
  snapshot is taken inside the container and `docker cp`'d to
  `hashicorp_vault_backup_dir` on the host; old snapshots are pruned by age.
  Backups authenticate with a scoped periodic token (policy: `read` on
  `sys/storage/raft/snapshot`), minted on the leader and distributed 0400 to
  each node; the backup script self-heals the token if it's missing.
  HA clusters should point `hashicorp_vault_backup_dir` at a shared/NFS
  location (set `_backup_nfs_server` + `_nfs_export`) — the default is
  per-node local, which is not real DR.
- A policy name is inert until an auth object (LDAP group, userpass account,
  AppRole, JWT role, or Identity group) attaches it. Identity groups are a
  second RBAC layer, not a second login method.
- Enterprise licensing (`hashicorp_vault_license_enabled: true`) uses
  `VAULT_LICENSE_PATH` (set in compose), not the `VAULT_LICENSE` env var, to
  avoid leaking the key via `docker inspect`. `hashicorp_vault_license_validate`
  (default `true`) runs `vault license inspect` offline in a throwaway
  container before installing the blob, so a mangled or expired key fails the
  play before anything restarts.
- The `pki` phase enables `pki/` and upserts issuing roles; it does not
  generate or import the intermediate issuer — that lifecycle is out of scope
  for this role (see Out of scope).
- **Control Groups (Enterprise, dormant).** The `control_groups` phase writes
  ACL policies whose `control_group` stanza gates KV reads behind an Identity-
  group approval threshold: a gated read returns a response-wrapped accessor;
  an authorizer runs `vault write sys/control-group/authorize accessor=<x>`, and
  once the threshold is met the requester runs `vault unwrap <token>`. Define
  gated paths in `hashicorp_vault_control_groups` (`{policy_name, mount, paths,
  capabilities, controlled_capabilities, ttl, authorizers}` — see
  `defaults/main.yml` and `examples/add-on-control-groups.yml`). `mount` must be
  an existing tenant mount; each authorizer `group_name` must be an Identity
  group created by the identity phase; attach `policy_name` to requesters via
  the existing group/identity mechanisms. `controlled_capabilities` is rendered
  explicitly — leaving it unset would gate every capability on the path,
  including reads granted by other policies. A requester must not also hold an
  ungated policy for the same path + capability, or the gate is bypassed.
  **This phase is authored to the documented ACL syntax and statically
  validated, but never executed here (no Enterprise license).** A licensed
  maintainer must verify: (1) `vault policy write` accepts the rendered stanza,
  (2) the wrap → approve → unwrap flow with a real approver, (3) TTL expiry of
  an unapproved request, (4) that the bypass rule holds, (5) the `+ent`
  version assert matches their build's `vault status`.

## Out of scope

- Generating or rotating the PKI intermediate issuer certificate — this role
  only imports an already-escrowed issuing CA (`pki_issuer_import`) or issues
  from whatever CA is already mounted.
- Creating the FreeIPA groups that LDAP auth maps by name.

## Expected result

- `vault status` on every node reports `Sealed: false` and the expected
  `HA Mode` (`active` on one node, `standby` on the rest).
- `vault operator raft list-peers` matches `hashicorp_vault_nodes` exactly —
  `verify.yml` asserts this on every run and fails loudly with remediation
  commands when it doesn't.

## Tag safety

- `remove_peers` (`never`-tagged, opt-in): evicts every Raft member not in
  `hashicorp_vault_nodes`. Run it only after a deliberate shrink, while the
  cluster still has a leader.
- `backup_now` (`never`-tagged, opt-in): forces an on-demand Raft snapshot via
  the deployed `vault-container-backup.service` and fails non-zero on error.
  The unit must already be deployed (`--tags backup` once first).
- `restore` (`never`-tagged, opt-in, destructive): applies a Raft snapshot on
  the active leader with `-force`; Vault replicates it to followers
  automatically (no follower wipe/restart). Root token only, from the first
  node. Targets same-cluster rollback (identical membership + unseal keys) —
  restoring a snapshot from a different cluster is a separate workflow.
  Optionally set `-e hashicorp_vault_restore_snapshot=<path>` to pick a
  specific snapshot; default is `"latest"`, resolved across every node.
- `renew_drill` (`never`-tagged, opt-in): forces a real certmonger certificate
  re-issue to prove the renewal path — do not run against a healthy cluster
  without reason.
- `control_groups` (`never`-tagged, **double opt-in**, Enterprise): requires
  BOTH `hashicorp_vault_control_groups_enabled: true` AND `--tags
  control_groups` — the `'control_groups' in ansible_run_tags` guard means a
  co-tag or play-level tag cascade cannot select it, and a persisted `enabled:
  true` still no-ops on a bare converge. It asserts a licensed Enterprise build
  before any write. Authored to spec, never executed in this repo — see
  **Behaviour → Control Groups** for the licensed-verification checklist.
- **Do not `--limit` a subset during `deploy`/`init`.** Topology is derived
  from the play's hosts, so a limited run would misconfigure Raft. Target the
  whole group, or pin `hashicorp_vault_nodes` explicitly in inventory.
