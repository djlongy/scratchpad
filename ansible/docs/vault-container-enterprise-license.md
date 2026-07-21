# Runbook — Enable a Vault Enterprise license on `hashicorp_vault_container`

**Audience:** an operator enabling Enterprise on an existing containerized Vault
deployment. **Role docs:**
[`../roles/hashicorp_vault_container/README.md`](../roles/hashicorp_vault_container/README.md)
(§ Enterprise license) and
[`SUPPORT.md`](../roles/hashicorp_vault_container/SUPPORT.md).
**Playbook:** [`../playbooks/vault_cluster.yml`](../playbooks/vault_cluster.yml).

This feature was validated statically against HashiCorp's autoloading docs, the
`vault-enterprise` Docker Hub registry, and the `vault license inspect` /
`sys/config/reload/license` APIs — but has **never been run with a real
license** (no trial license was available when it was written). Follow this
runbook in order: each step proves itself before the next.

---

## 0. Facts this depends on (verified 2026-07)

- `hashicorp/vault-enterprise` has **no bare version tags**. Every tag carries a
  suffix: `1.19.5-ent`, `1.19.5-ent.hsm`, `1.19.5-ent.hsm.fips1403`. Use the
  plain `-ent` variant unless you have HSM hardware. Images are multi-arch
  (amd64 + arm64).
- License autoload precedence: `VAULT_LICENSE` env → `VAULT_LICENSE_PATH` env →
  `license_path` in HCL. The role sets the last **two** (same file), never the
  raw-string env (it would leak via `docker inspect`).
- `vault license inspect` validates a license **offline** — signature, format,
  expiry — using only the Enterprise binary. No server needed. The role runs
  this automatically before installing anything
  (`hashicorp_vault_license_validate`, default `true`).
- The Vault **binary build date must be older than the license expiry**, or
  Vault refuses to start. Practical consequence: a brand-new image with an old
  (nearly expired) license can fail — keep image version and license purchase
  roughly contemporary.
- Every Raft node must load the **same** license key. The role installs it on
  all hosts in the play.
- A live cluster can hot-reload a changed license file via
  `sys/config/reload/license` **without sealing**. The role attempts this and
  falls back to a container restart (which does seal).

## 1. Prerequisites

1. A working Community deployment of this role (`--tags verify` is green).
2. The license blob (contents of the `.hclic` file HashiCorp sent) — one long
   base64-ish string on **one line**.
3. If your inventory vars are Ansible-Vault encrypted: the vault password
   available (e.g. `export ANSIBLE_VAULT_PASSWORD=$(cat ~/secrets/vault-password.txt)`).

## 2. Store the license (it is a credential)

Keep the blob out of plaintext git. Escrow it wherever your other secrets live
(a password manager or an existing secrets server), then reference it from an
**Ansible-Vault-encrypted** inventory var — the role reads the var, not a
secrets server, so enabling works even while the cluster being licensed is down:

```bash
ansible-vault encrypt_string --stdin-name vaulted_vault_enterprise_license < vault.hclic
```

Paste the `!vault` block into the target group_vars. **The blob must survive as
a single line** — an unquoted or folded (`>-`) YAML scalar inserts
newlines/spaces and produces a signature failure. The `!vault` encrypted string
or a single-quoted scalar are both safe.

## 3. Enable in inventory

```yaml
# inventories/<env>/group_vars/vault.yml (or your vaulted vars file)
hashicorp_vault_license_enabled: true
hashicorp_vault_image: "hashicorp/vault-enterprise:1.19.5-ent"   # -ent suffix is mandatory
hashicorp_vault_license: "{{ vaulted_vault_enterprise_license }}"
# alternative to the var: a controller-side file
# hashicorp_vault_license_src: "/secure/path/vault.hclic"
```

Keep the Enterprise version aligned with the previously running Community
version (e.g. `hashicorp/vault:1.19.5` → `1.19.5-ent`) for the first cutover;
upgrade versions as a separate, later step so failures are attributable.

## 4. Apply

```bash
# license phase + deploy (image swap => container recreate)
ansible-playbook -i inventories/<env>/hosts.yml \
  playbooks/vault_cluster.yml --tags license,deploy

# the recreate seals every node — unseal:
ansible-playbook -i inventories/<env>/hosts.yml \
  playbooks/vault_cluster.yml --tags unseal

# prove it: verify now asserts `vault license get` reports an autoloaded
# license and prints its expiry
ansible-playbook -i inventories/<env>/hosts.yml \
  playbooks/vault_cluster.yml --tags verify
```

What the role does, in order — each step gates the next:

1. Resolves the blob (var first, else `license_src` file); fails if empty.
2. Asserts the image ref contains `enterprise`.
3. **Validates the blob offline** — `vault license inspect` in a throwaway
   container of the target image. A mangled paste, expired key, or unpullable
   image fails *here*, before anything on disk changes.
4. Installs `<data_mount>/config/license.hclic` (root:root 0644) on every node.
5. If the API is up and the file changed: hot-reload per node
   (`sys/config/reload/license`, rolling). Otherwise queues a container restart.
6. Deploy templates `VAULT_LICENSE_PATH` into compose and `license_path` into
   `vault.hcl`, recreating the container on the new image.
7. Verify asserts the **running binary** reports an autoloaded license.

## 5. Manual success evidence (on a node)

```bash
docker exec vault vault status                    # Sealed: false
docker exec -e VAULT_TOKEN="$(sudo cat /opt/vault/keys/root_token.txt)" \
  vault vault license get                         # shows autoloaded license + expiry
docker logs vault 2>&1 | grep -i license          # "license is valid" / autoload lines
```

## 6. Renewal (new .hclic before expiry)

Replace the blob in the vaulted var (and re-escrow it), then:

```bash
ansible-playbook -i inventories/<env>/hosts.yml \
  playbooks/vault_cluster.yml --tags license
```

The cluster is up, so the role hot-reloads node by node — **no seal, no
restart**. Confirm with `--tags verify` (the expiry date in the output must
move).

## 7. Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `manifest unknown` / pull error on deploy | Bare tag (`:1.19.5`) — Enterprise tags always end in `-ent` | Use `hashicorp/vault-enterprise:<ver>-ent` |
| Play fails at "Assert the license passed offline validation" | Mangled blob (multi-line YAML), expired key, or wrong image | Re-check step 2's single-line rule; reproduce per the task's fail message |
| Container crash-loops, logs say license invalid/expired/missing | File landed but bad, or binary build date newer than license expiry | `docker logs vault`; `vault license inspect` against the file; match image version to license period |
| Verify fails "no autoloaded license" but container runs | Still on the Community image (deploy tag not run) or container never restarted | `--tags deploy` then `--tags unseal`; check `docker inspect -f '{{.Config.Image}}' vault` |
| Nodes sealed after the enable run | Expected — recreate/restart seals Shamir-sealed Vault | `--tags unseal` (or `hashicorp_vault_auto_unseal: true`) |
| Hot-reload "failed" message during renewal | API down on that node, or license_path not yet in config | Role auto-falls back to restart; just run `--tags unseal` after |
| One node licensed, others not | `--limit` during the license run | Re-run against the whole group (never `--limit` this role) |

## 8. Rollback

- **Before the Enterprise image ever served traffic:** set
  `hashicorp_vault_license_enabled: false`, restore the Community image pin,
  run `--tags license,deploy` then `--tags unseal`. The license phase is a
  no-op when disabled; deploy drops `license_path`/`VAULT_LICENSE_PATH` and
  recreates on Community.
- **After running Enterprise with real data:** HashiCorp does **not** support
  downgrading Enterprise → Community on the same storage. Take a Raft snapshot
  first (`--tags backup_now`); rollback = restore a **pre-Enterprise** snapshot
  onto the Community image (`--tags restore`), accepting loss of anything
  written since.
