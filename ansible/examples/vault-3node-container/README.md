# vault-3node-container

Reusable Ansible role + sample inventory/playbooks for deploying a
**3-node HashiCorp Vault Raft HA cluster as a Docker container**, with
self-signed TLS for testing and an example FreeIPA LDAP auth backend.

Validated against a 3-VM AlmaLinux 9 lab (1.19.5, Raft HA, HTTPS via a
shared self-signed CA, login as a FreeIPA user verified end-to-end).

## What you get

- **`vault_container` role** — installs Docker, the Vault CLI binary
  (host) + image (container), generates a shared self-signed CA + cert
  with SANs for every cluster node, renders the systemd unit + HCL,
  starts vault, initialises raft on the leader, and joins followers.
- **Optional LDAP auth task** — example wiring for FreeIPA. Enable +
  configure with one variable; map an LDAP group to a Vault policy.
- **Sample inventory + playbooks** showing the expected order.

Cert issuance, audit logging, backup timers, JWT/OIDC, K8s auth, and
ESO integration are deliberately **out of scope** — bring your own
once the cluster is up.

## Layout

```
vault-3node-container/
├── inventory/
│   ├── hosts.yml
│   └── group_vars/
│       └── vault/
│           ├── main.yml          # non-secret tunables
│           └── vault.yml.example # ansible-vault'd
├── playbooks/
│   └── site.yml                  # orchestrates phases in order
└── roles/
    └── vault_container/
        ├── defaults/
        ├── tasks/
        │   ├── main.yml
        │   ├── install.yml
        │   ├── selfsigned_tls.yml
        │   ├── config.yml
        │   ├── service.yml
        │   ├── cluster_init.yml
        │   ├── auto_unseal.yml
        │   ├── kv_mounts.yml
        │   ├── policies.yml
        │   ├── auth_userpass.yml
        │   ├── auth_approle.yml
        │   └── ldap_auth.yml
        ├── templates/
        │   ├── vault.hcl.j2
        │   ├── vault.service.j2
        │   ├── unseal-vault.sh.j2
        │   ├── unseal-vault.service.j2
        │   └── policies/
        │       ├── ops-superadmin.hcl.j2
        │       ├── ops-admin.hcl.j2
        │       └── svc-automation.hcl.j2
        └── handlers/
```

## Phase order (matters)

1. **install** — packages, vault user, Docker, CLI binary, image pull.
2. **selfsigned_tls** — control-node generates a CA + ONE shared cert
   with SANs for every node, ships them to `/etc/vault.d/certs/`, and
   installs the CA into the host trust store.
3. **config** — render `vault.hcl` with raft storage + retry_join +
   listener TLS config (using the shared cert) + `leader_ca_cert_file`
   on every retry_join (the container doesn't see the host trust
   store; the CA file path inside the bind-mount is what the joining
   peers use to validate the leader's cert).
4. **service** — render systemd unit + start vault. Container runs
   `--user <host vault uid:gid>` so bind-mount perms work, plus
   `SKIP_SETCAP=1 SKIP_CHOWN=1 + --cap-add IPC_LOCK` (see "Container
   user gotcha" below).
5. **cluster_init** — `vault operator init` on the leader (Shamir,
   single key for the lab; bump `key_shares`/`key_threshold` for prod),
   write the unseal key + root token to `/opt/vault/keys/`, unseal the
   leader, then unseal each follower.
6. **auto_unseal** — deploy `unseal-vault.sh` + `unseal-vault.service`
   (Type=oneshot, `After=vault.service`, `WantedBy=multi-user.target`)
   so the cluster auto-unseals after every host reboot or container
   restart. The script polls `/v1/sys/health` until the API is up,
   then runs `vault operator unseal` until `Sealed=false`. Lab-grade —
   reads the single Shamir share from `/opt/vault/keys/`. Replace with
   KMS-backed seal for prod and this step becomes unnecessary.
7. **post-setup phases** (each tag-targetable):
   - **kv** — create the KV v2 mounts listed in `vault_kv_mounts`
     (default: `kv-default`; add `kv-prod`, `kv-stage`, `kv-dev` etc.).
   - **policies** — render + apply ACL policies. Each entry in
     `vault_policies` maps to `templates/policies/<name>.hcl.j2`.
     Three sample policies ship with the role:
     - `ops-superadmin` — break-glass, 1h non-renewable.
     - `ops-admin` — daily operator, 24h.
     - `svc-automation` — read-only KV + child tokens for IaC tooling.
     Templates reference `vault_kv_mounts` so adding a mount
     automatically grants the right paths to ops-* policies.
   - **userpass** — enable userpass auth + create a break-glass admin
     bound to `ops-superadmin`. Password from the encrypted
     `vault.yml`.
   - **approle** — enable approle auth + create one role per entry in
     `vault_approles` (samples: `svc-terraform`, `svc-ansible` bound
     to `svc-automation`). Use `vault read auth/approle/role/<name>/role-id`
     and `vault write -f auth/approle/role/<name>/secret-id` to mint
     credentials for tooling.
8. **ldap_auth** *(optional)* — example FreeIPA LDAP auth backend +
   group→policy mapping.

### Targeting individual phases

Every phase has a tag — useful when iterating:

```bash
# Re-render + reapply just the policies after editing a template:
ansible-playbook -i inventory/hosts.yml playbooks/site.yml --tags policies

# Add a KV mount you defined in inventory:
ansible-playbook -i inventory/hosts.yml playbooks/site.yml --tags kv

# Just the resilience bits (after switching seal types, say):
ansible-playbook -i inventory/hosts.yml playbooks/site.yml --tags auto_unseal
```

`playbooks/site.yml` runs all of the above in order.

## Container user gotcha

The Vault image's entrypoint
([source](https://github.com/hashicorp/vault/blob/main/scripts/docker/docker-entrypoint.sh))
does two things that bite the
"--user <host_vault_uid>:<gid>" mapping pattern:

- runs `setcap` on the vault binary (needs root + SETFCAP capability)
- if running as root, drops to the image's internal `vault` user
  (uid 100) before exec-ing vault server

So if you pass `--user 0:0`, the entrypoint silently switches the
running uid to 100 — and any bind-mounted files owned by your host's
vault uid (e.g. 995) become unreadable.

The pattern that works across the host↔container boundary:

```yaml
vault_container_extra_args:
  - "--cap-add IPC_LOCK"   # vault expects to mlock; harmless with disable_mlock=true
  - "-e SKIP_SETCAP=1"     # we'll set capabilities outside the container if needed
  - "-e SKIP_CHOWN=1"      # bind-mounted dirs are pre-chowned
```

Combined with the role's `--user <host_vault_uid>:<host_vault_gid>`,
the container processes appear as your host's vault user end-to-end.

## retry_join + self-signed certs

When using a private CA, every `retry_join` block needs
`leader_ca_cert_file = "/etc/vault.d/certs/ca.crt"`. The role's
template injects this automatically; if you drop in a Let's Encrypt
cert later you can remove it (system trust store covers LE).

## LDAP via FreeIPA

`tasks/ldap_auth.yml` is opinionated for FreeIPA. To use:

1. Set `vault_ldap_enabled: true` in inventory.
2. Provide `vault_ldap_url`, `vault_ldap_userdn`, `vault_ldap_groupdn`,
   and the bind credential variable (typically encrypted in vault.yml).
3. Define `vault_ldap_group_policies` as a dict of
   `<freeipa group>: [<vault policy>, ...]`.

The task enables `auth/ldap`, writes the config, and creates a
`groups/<name>` mapping per entry. After provisioning, log in with:

```
vault login -method=ldap username=<freeipa user>
```

Token policies will be the union of `default` + the policies bound to
each FreeIPA group the user belongs to.

## Production checklist (when adopting)

This template is a STARTING POINT. Before running prod:

- Replace self-signed certs with a real CA (Let's Encrypt via DNS-01,
  internal PKI, etc.) and drop the `selfsigned_tls` step.
- Bump `key_shares`/`key_threshold` (single-key Shamir is fine for
  lab, never prod).
- Replace the file-on-disk auto-unseal helper with KMS-backed seal
  (AWS KMS, GCP KMS, Transit, etc.) — root token + unseal key in
  `/opt/vault/keys/` is a dev convenience.
- Add audit logging (`vault audit enable file path=/var/log/vault/audit.log`).
- Add backup timers (raft snapshots).
- Tighten the listener block's TLS cipher list.
- Pin the Vault image to a specific tag, not a moving label.
