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

Full map: [README Auth & RBAC](README.md#auth--rbac-map-read-this-before-enabling-flags).

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

**Step-by-step first enable, renewal, and rollback:**
[`../../docs/vault-container-enterprise-license.md`](../../docs/vault-container-enterprise-license.md)

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
| Disk / LVM / mount | `storage` role |
| Docker install | `docker` role |
| Intermediate CA from cold root | `vault_pki` + offline ceremony |
| Prod native package Vault | `hashicorp_vault` role (legacy path until cutover) |
| K8s auth for ESO | `hashicorp_vault_k8s_auth` |

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
