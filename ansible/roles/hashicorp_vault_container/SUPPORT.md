# hashicorp_vault_container — operator support pack

**This role is unmaintained by its original author.** Everything you need to
deploy, operate, and recover lives in this directory. Prefer these files over
tribal knowledge.

| Document | Use when |
|---|---|
| **This file (`SUPPORT.md`)** | First day, incident, “what do I run?” |
| [`README.md`](README.md) | Full contract: variables, auth map, phases |
| [`examples/`](examples/) | Copy-paste inventory for Path A / B / CI |
| [`defaults/main.yml`](defaults/main.yml) | Every knob + comments |
| [`meta/argument_specs.yml`](meta/argument_specs.yml) | Typed contract for Ansible |
| [`tasks/main.yml`](tasks/main.yml) | Phase order + tags |

Playbook in this repo: `playbooks/vault_cluster.yml`  
Lab inventory reference: `inventories/vault/`

**Shipping bundle** — moving this role to another repo? Copy together:

| Item | Path |
|---|---|
| The role itself | `roles/hashicorp_vault_container/` (this directory, entire) |
| Reference playbook | `playbooks/vault_cluster.yml` (daisy-chains `storage` → `docker` → this role) |
| Inventory example | one `inventories/<env>/` skeleton (or the `inventories/vault/` lab reference) |

Everything else in this repo is optional context. The role depends on the
`storage` and `docker` roles at the **playbook** level (not as role dependencies);
bring those too, or supply your own disk-mount and Docker-install steps.

---

## 1. What this role owns (and does not)

**Owns**

- Docker Compose Vault on a **persistent second disk** (`hashicorp_vault_data_mount`)
- Raft (1 node standalone or odd N ≥ 3 HA)
- TLS, init/unseal, verify, backup timer, optional LDAP/policies/AppRole/JWT/PKI/license

**Does not own**

- Provisioning the VM or mounting the disk → `storage` / `vsphere_vm`
- Installing Docker → `docker`
- FreeIPA group creation → directory team / `freeipa_*`
- Cold-root intermediate CA ceremony → `vault_pki` + offline process
- Human GitLab OIDC SSO → not part of this role (human SSO = LDAP)

---

## 2. Day-1 greenfield (minimum that works)

**Prerequisites on every host**

1. Odd count of hosts in the play: **1**, **3**, **5**, … (never 2)
2. Disk mounted at `hashicorp_vault_data_mount` (default `/opt/vault`) — real mountpoint
3. Docker Engine + Compose plugin
4. From the **controller**: Ansible inventory, SSH, and (if vaulted vars) `ANSIBLE_VAULT_PASSWORD`

**Inventory (non-secret)**

```yaml
hashicorp_vault_data_mount: /opt/vault
# optional SANs
hashicorp_vault_tls_extra_sans:
  - "DNS:vault.example.com"
```

> **DECIDE `hashicorp_vault_key_shares` / `hashicorp_vault_key_threshold` BEFORE
> the first init.** They default to `1`/`1` (single-key, playbook-driven unseal);
> set e.g. `5`/`3` for shared-custody. These are baked in **permanently** at init —
> changing them afterwards needs a manual `vault operator rekey` ceremony, not a
> re-run. (Irrelevant when you unseal via `hashicorp_vault_seal_config` KMS/transit
> auto-unseal — there are no Shamir shares.)

**Do not enable** LDAP, license, Identity, JWT, transit, etc. until core is green.

```bash
# Full converge (storage + docker + this role if playbook daisy-chains them)
ansible-playbook -i inventories/<env>/hosts.yml \
  playbooks/vault_cluster.yml

# List tags
ansible-playbook ... playbooks/vault_cluster.yml --list-tags
```

**Success checks**

```bash
# On a node
docker ps --filter name=vault
curl -sk https://127.0.0.1:8200/v1/sys/health | jq .
# sealed=false, initialized=true on healthy unsealed node

# Root/init material (first node only) — protect this
ls -la /opt/vault/keys/
# vault_init.json, root_token.txt  (mode 0400)
```

---

## 3. Tag cheat sheet

| Tag | When to use |
|---|---|
| *(none / full)* | Greenfield or full converge |
| `preflight` | Topology / mount checks only |
| `deploy` | Config + compose + start |
| `init` | First-time init + join |
| `unseal` | After reboot/restart (does not re-init) |
| `verify` | Health + Raft membership |
| `backup` / `backup_now` | Timer setup / force snapshot |
| `restore` | **Destructive** rollback (`never` tag) |
| `remove_peers` | After shrink, before powering off old nodes (`never`) |
| `policies` / `ldap` / `identity` / `userpass` / `approle` / `gitlab_jwt` | Auth layers |
| `license` | Enterprise license only when purchased |
| `pki` / `pki_issuer` / `transit` / `audit` | Engines |

**Rules**

- Never `--limit` a subset of the cluster during **deploy/init** unless `hashicorp_vault_nodes` is pinned to the full set.
- Keep the **first** host in `hashicorp_vault_nodes` the original init node when growing.

---

## 4. Where state lives on disk

All under `hashicorp_vault_data_mount` (default `/opt/vault`):

| Path | Content |
|---|---|
| `config/vault.hcl` | Server config |
| `config/license.hclic` | Enterprise license file (only if license enabled) |
| `data/` | Raft storage (the cluster) |
| `certs/` | TLS material |
| `keys/vault_init.json` | Unseal keys + root (first node) |
| `keys/root_token.txt` | Root token for automation phases |
| `keys/backup_token` | Scoped backup token |
| `logs/` | Server + audit logs |
| `backups/` | Raft snapshots (or NFS mountpoint) |
| `docker-compose.yml` | Compose unit at mount root |

**Backup the keys dir and snapshots off-node.** Losing `keys/` without a recovery plan means sealed forever or break-glass re-init.

---

## 5. Auth — pick one human path

Full map: [README Behaviour](README.md#behaviour) (Auth & secrets phases).

| Path | Enable | Policies stick at |
|---|---|---|
| **A (default start)** | `ldap_enabled` + tenants | `auth/ldap/groups/<freeipa-cn>` |
| **B (nesting)** | LDAP + `identity_groups` for **extra** FreeIPA CNs | Identity groups + aliases |
| Break-glass | `userpass_accounts` | `auth/userpass` |
| Automation | `approles` | AppRole |
| CI | `gitlab_jwt_enabled` | `auth/jwt` |

Copy-paste: `examples/path-a-ldap-groups.yml`, `path-b-identity-nesting.yml`, `add-on-ci-and-automation.yml`.

**Dict key names (must match exactly)**

| Object | Key |
|---|---|
| Extra policy | `policy_name` |
| LDAP user | `ldap_username` |
| LDAP group map | `ldap_group` |
| AppRole | `role_name` |
| Identity group | `identity_group_name` |

Wrong keys (`name`, `user`) silently fail to configure what you expect.

---

## 6. Enterprise license (optional)

Default **off** — safe without a purchase.

When you have a license:

```yaml
hashicorp_vault_license_enabled: true
hashicorp_vault_image: "hashicorp/vault-enterprise:<version>-ent"
hashicorp_vault_license: "{{ vaulted_license_blob }}"
```

```bash
ansible-playbook ... --tags license,deploy
ansible-playbook ... --tags unseal   # if restart sealed nodes
```

Autoload order (HashiCorp): `VAULT_LICENSE` → `VAULT_LICENSE_PATH` → `license_path`.  
This role uses **file + path** (not raw env string).  
Docs: https://developer.hashicorp.com/vault/docs/license/autoloading

**Store the blob as a credential.** Escrow it in your secrets manager, then
reference it from an **Ansible-Vault-encrypted** inventory var (the role reads the
var, not a live secrets server, so it works even while the cluster is down):

```bash
ansible-vault encrypt_string --stdin-name vaulted_vault_enterprise_license < vault.hclic
```

The blob **must survive as a single line** — a folded (`>-`) or unquoted YAML
scalar inserts newlines and produces a signature failure. Enterprise tags always
carry a suffix (`1.19.5-ent`); bare version tags do not exist. Keep the Enterprise
version equal to the running Community version on the first cutover; upgrade
versions as a separate, later step so failures are attributable.

**First enable, renewal, rollback:**

| Action | Steps |
|---|---|
| **First enable** | Set the three vars above → `--tags license,deploy` (image swap recreates the container) → `--tags unseal` (recreate seals Shamir nodes) → `--tags verify` (asserts `vault license get` reports an **autoloaded** license + prints expiry). The role validates the blob offline (`vault license inspect` in a throwaway container) *before* installing, so a mangled/expired key fails before anything changes. |
| **Renewal** | Replace the blob in the vaulted var (re-escrow) → `--tags license` only. Cluster is up, so the role hot-reloads node-by-node via `sys/config/reload/license` — **no seal, no restart**. Confirm `--tags verify` — the expiry date must move. |
| **Rollback (pre-traffic)** | Before the Enterprise image served traffic: `hashicorp_vault_license_enabled: false`, restore the Community image pin → `--tags license,deploy` → `--tags unseal`. Deploy drops `license_path`/`VAULT_LICENSE_PATH` and recreates on Community. |
| **Rollback (post-traffic)** | HashiCorp does **not** support Enterprise → Community downgrade on the same storage. Snapshot first (`--tags backup_now`); rollback = restore a **pre-Enterprise** snapshot onto the Community image (`--tags restore`), accepting loss of anything written since. |

Every Raft node must load the **same** license (the role installs it on all hosts;
never `--limit` a license run). The Vault binary build date must be older than the
license expiry, or Vault refuses to start — keep image version and license period
roughly contemporary.

---

## 7. Troubleshooting (symptom → check → fix)

### Preflight fails: even node count

- **Cause:** 2/4/6 hosts in play or bad `--limit`.
- **Fix:** Use 1 or odd ≥ 3. Pin `hashicorp_vault_nodes` to full membership.

### Preflight fails: data_mount not a mountpoint

- **Cause:** `/opt/vault` is a plain directory or missing.
- **Fix:** Run `storage` (or mount the disk). `findmnt /opt/vault`.

### Container starts then restarts / API never up

```bash
docker logs vault --tail 100
cat /opt/vault/config/vault.hcl
```

- **Address already in use:** duplicate `-config` (role avoids this; check custom edits).
- **Permission denied on data:** SELinux / ownership — role uses `:Z` when `selinux_relabel: true`.
- **License / enterprise:** Community image + `license_path` or missing/expired license.

### Node sealed after reboot

- **Expected** when `auto_unseal: false`.
- **Fix:** `ansible-playbook ... --tags unseal`  
  Or enable `hashicorp_vault_auto_unseal: true` (unseal key on every node).

### Verify fails: ghost / stale Raft peers

- **Cause:** Hosts removed from inventory without `remove_peers`.
- **Fix:** While cluster still has quorum:  
  `ansible-playbook ... --tags remove_peers`  
  **Before** powering off old nodes.

### Verify fails: missing peers

- **Cause:** New node not joined, sealed, or network 8200/8201 blocked.
- **Fix:** Full group deploy/unseal; check firewall and `advertise_addr`.

### LDAP login fails

- Bind password empty or trailing newline (role sets `stdin_add_newline: false`).
- FreeIPA group CN must match maps (`vault-<tenant>-<env>`).
- Group filter: posix `memberUid` vs `groupOfNames`+`member` — set `ldap_groupfilter` to match FreeIPA.
- Role does **not** create FreeIPA groups.
- **TLS:** `hashicorp_vault_ldap_insecure_tls` defaults to **false** (verifies the
  directory's LDAPS cert). A lab with a self-signed directory CA sets it `true` in
  inventory.
- **Active Directory instead of FreeIPA:** set `hashicorp_vault_ldap_userattr:
  sAMAccountName`, keep `hashicorp_vault_ldap_groupattr: cn`, and use a member-DN
  group filter — `(&(objectClass=group)(member={{ '{{.UserDN}}' }}))` — instead of
  the POSIX `memberUid` default.
- **FreeIPA — keep `userdn` NARROW** (`cn=users,cn=accounts,dc=...`). The
  schema-compat plugin duplicates every user under `cn=compat`, and Vault ≥ 1.19
  **errors when the user search returns > 1 entry** — a broad base DN matches both
  copies and every login fails.
- **FreeIPA — user lockout masks the real error.** After ~5 failed logins Vault's
  built-in user lockout kicks in and returns a generic failure. While debugging:
  `docker exec vault vault auth tune -user-lockout-disable=true ldap/` (re-enable
  when done).

### Init ran twice / two clusters

- **Cause:** First host in `hashicorp_vault_nodes` was uninitialised during a grow.
- **Fix:** Always keep original init node **first**. Recovery is advanced (Raft/peers.json); restore from snapshot if available.

### Restore

- Tag `restore` is `never`-gated and **destructive**.
- Uses root token; snapshot must be same cluster membership/unseal keys for normal rollback.

### Backup token missing after restore

- Re-run `--tags backup` to re-mint scoped backup token.

---

## 8. Safe day-2 operations

| Event | Command / action |
|---|---|
| Reboot | `--tags unseal` (or auto-unseal) |
| Config tweak | `--tags deploy` then unseal if restarted |
| Grow 1→3 | Add hosts **after** original first; full playbook |
| Shrink 3→1 | Drop hosts from inventory → `--tags remove_peers` → decommission |
| Nightly DR proof | `--tags backup_now` |
| Rollback | `--tags restore` with care |

---

## 9. External references

- Vault configuration: https://developer.hashicorp.com/vault/docs/configuration  
- Raft storage: https://developer.hashicorp.com/vault/docs/configuration/storage/raft  
- License autoload: https://developer.hashicorp.com/vault/docs/license/autoloading  
- License reload API: https://developer.hashicorp.com/vault/api-docs/system/config-reload  
- Enterprise image: https://hub.docker.com/r/hashicorp/vault-enterprise  

---

## 10. When something is “impossible” from the role alone

| Need | Go elsewhere |
|---|---|
| New FreeIPA groups/users | FreeIPA / IdM automation |
| Disk / LVM / mount | Host disk provisioning (e.g. second disk + mount at `data_mount`) |
| Docker install | Docker Engine + Compose on the host before this role runs |
| Intermediate CA from cold root | Separate PKI / offline ceremony process |

---

## 11. Self-test after any change

```bash
ansible-playbook -i inventories/<env>/hosts.yml \
  playbooks/vault_cluster.yml --tags verify

# Optional: force a snapshot and confirm exit 0
ansible-playbook ... --tags backup_now
```

If verify is green, the cluster membership and seal state match the inventory.
Everything else (LDAP, policies, license) has its own tags for isolated re-runs.

**`--check` proves rendering only.** A dry-run does not exercise the Vault-API
phases — the `docker exec` / `command` tasks (init, unseal, policies, LDAP, PKI,
backup token…) skip in check mode — so a green `--check` confirms templating and
file state, not that Vault accepted anything.

---

## 12. TLS at work — shared CA

By default `hashicorp_vault_tls_generate: true` self-signs a CA on **whichever
controller runs the play**, so the trust anchor differs per operator. For a team,
one CA identity must own the trust:

1. **First run** generates the CA under `hashicorp_vault_tls_local_dir` on that
   controller.
2. **Escrow its private key.** Encrypt the generated `ca.key` as an inventory var:

   ```bash
   ansible-vault encrypt_string --stdin-name hashicorp_vault_tls_ca_key_content < ca.key
   ```

3. **Publish its certificate.** `ca.crt` is public — supply it either as a flat
   inventory var `hashicorp_vault_tls_ca_content`, **or** upload it to an
   Artifactory generic repo and point `hashicorp_vault_tls_ca_url` at it (with an
   optional `hashicorp_vault_tls_ca_url_checksum: "sha256:<hex>"`).
4. **Every subsequent run** on any controller reuses that one CA: with
   `_ca_key_content` + `_ca_content` set, the role signs server certs from the
   shared CA instead of minting a fresh per-operator one. When a `_ca_url` is also
   set, the role **asserts the fetched CA fingerprint matches the signing CA**, so
   a wrong upload fails the run rather than the TLS handshake.

CA-cert distribution precedence for node trust: `_ca_url` > `_ca_content` >
generated/provided file.

---

## 13. Enterprise auto-unseal (seal stanza)

`hashicorp_vault_seal_config` delegates unsealing to an external KMS/transit key
instead of Shamir shares. Shape `{type, config}` renders a `seal "<type>" { … }`
block into `vault.hcl`:

```yaml
hashicorp_vault_seal_config:
  type: awskms          # awskms | azurekeyvault | gcpckms | transit | pkcs11
  config:
    region: ap-southeast-2
    kms_key_id: "arn:aws:kms:…"
```

- **The role skips its unseal phase entirely** — the external seal unseals Vault on
  start. It is **mutually exclusive** with `hashicorp_vault_auto_unseal` (the role
  asserts you set at most one).
- **Prefer ambient credentials** (instance profile / workload identity) over baking
  secrets into `config` — every `config` value lands verbatim in `vault.hcl` on disk.
- **Changing the seal on an already-initialized cluster is a manual migration.**
  Switching Shamir → KMS (or between seal types) on live data requires a
  `vault operator unseal -migrate` procedure that this role does **not** automate.
  Set the seal correctly at first init, or run the migration by hand.
