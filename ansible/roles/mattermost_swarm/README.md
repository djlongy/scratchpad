# mattermost_swarm

Thin wrapper over `swarm_stack` that deploys Mattermost (postgres + app)
to an existing Docker Swarm. Demonstrates the wrapper pattern — copy and
adapt for any application stack.

## What it does

- Builds a complete `swarm_stack_*` spec for the postgres + mattermost-app
  pair and hands it to `swarm_stack` via `include_role`.
- Optionally extends the spec with FreeIPA-backed LDAP/SSO (CA cert
  shipped as a docker config, bind password as a docker secret, env vars
  set on mm-app via env_template).
- Pulls all secret values from Ansible variable scope (typically a
  `vault.yml` encrypted with ansible-vault).

The actual deploy mechanics — secrets, configs, networks, NFS volumes,
service rollouts, prune, teardown — all live in `swarm_stack`. See its
README for the engine-level docs.

## Requires

- Role `swarm_stack` available on `roles_path` (this repo's `roles/`).
- An existing Docker Swarm reachable from the control node, with the
  Docker Python SDK installed on every candidate worker (see
  `swarm_stack` README for the one-time worker setup).
- A reachable NFS server providing the export path you point
  `mattermost_nfs_*` at.
- An ansible-vault'd YAML file with the secret values listed below
  (`inventories/swarm/group_vars/swarm_bootstrap/vault.yml.example` is
  the template).

## Quick start

```bash
# 1. Edit inventories/swarm/group_vars/swarm_bootstrap/main.yml — set
#    NFS host/server, swarm_overlay_subnets[mattermost], replica counts,
#    image tags, etc.
# 2. Copy vault.yml.example to vault.yml, fill in secrets, encrypt:
ansible-vault encrypt inventories/swarm/group_vars/swarm_bootstrap/vault.yml

# 3. Deploy:
ansible-playbook -i inventories/swarm/hosts.yml playbooks/mattermost_swarm.yml \
                 --ask-vault-pass

# 4. Idempotent re-runs: same command. Full destroy+recreate:
ansible-playbook -i inventories/swarm/hosts.yml playbooks/mattermost_swarm.yml \
                 --ask-vault-pass --tags redeploy
```

## Required vault secrets

Defined in `vault.yml.example`, must end up defined as Ansible variables
(via group_vars/host_vars/extra-vars) before the play runs:

| variable | meaning |
|---|---|
| `vault_mm_pg_password` | postgres password (read via `*_FILE` pattern) |
| `vault_mm_at_rest_key` | ≥ 32 chars; `MM_FILESETTINGS_*` AES-256 key |
| `vault_mm_public_link_salt` | ≥ 32 chars; mattermost public-link salt |
| `vault_mm_admin_password` | initial admin password (rotate after first login) |
| `vault_mm_ldap_bind_password` | bind password for `svc-mattermost` (LDAP only) |
| `vault_freeipa_admin_password` | for the FreeIPA prep playbook (LDAP only) |

## Knobs worth knowing

- `mattermost_pg_pinned_node` — postgres can't safely run multi-replica
  on shared NFS. Pin to a single worker.
- `mattermost_max_per_node: 1` + `update.order: stop-first` — combined,
  this lets a 3-replica / 3-worker stack roll without a deadlock
  (start-first would wait forever for a free slot).
- `vault_mm_at_rest_key` and `vault_mm_public_link_salt` MUST be ≥ 32
  chars or mattermost refuses to start.

## Mattermost secret model

Mattermost can't run with a read-only `config.json` mount (writes its
version stamp on first boot), so this wrapper drives everything via
`MM_*` env vars baked into a per-service env template that pulls from
`swarm_stack_secret_values`. Postgres is in the other camp — it reads
its password through the `*_FILE` pattern, so its secret is referenced
by name in the service's `secrets:` list.

## LDAP / SSO against FreeIPA (optional)

Mattermost's Enterprise Edition image (the FIPS-edition variant too)
runs as **Mattermost Entry** when no license is loaded — and Entry
covers LDAP, SAML, and OpenID, so SSO works without paying. The wrapper
ships a plug-in path for FreeIPA-backed LDAP login via
`MM_LDAPSETTINGS_*` env vars, gated by `mattermost_ldap_enabled`. The
defaults already match FreeIPA conventions (uid attribute,
person/groupOfNames classes, `cn=users,cn=accounts` user container).

Two-step deploy:

```bash
# 1. Provision the svc-mattermost user in FreeIPA + set its bind password
ansible-playbook -i inventories/swarm/hosts.yml \
                 playbooks/mattermost_freeipa_prep.yml --ask-vault-pass

# 2. Deploy / redeploy the stack with LDAP wired
ansible-playbook -i inventories/swarm/hosts.yml \
                 playbooks/mattermost_swarm.yml --ask-vault-pass --tags redeploy
```

What you fill in:

- `inventories/swarm/group_vars/swarm_bootstrap/main.yml` — flip
  `mattermost_ldap_enabled` to `true`, set `mattermost_ldap_host` to your
  FreeIPA server's FQDN. The base-DN, bind username, and attribute names
  default to FreeIPA's conventions; override only if you've customised
  your directory.
- `inventories/swarm/group_vars/swarm_bootstrap/vault.yml` — add two
  values:
    - `vault_freeipa_admin_password` — for the prep play's `ipauser` call
    - `vault_mm_ldap_bind_password` — same value gets set on the FreeIPA
      user AND used for the LDAPS bind from Mattermost
  The CA cert is **not** vaulted — the role slurps it from
  `/etc/ipa/ca.crt` on `groups['freeipa'][0]` at deploy time. It's
  public, so vaulting it would just be ceremony.
- `inventories/swarm/hosts.yml` — add a `freeipa` group containing at
  least one host that's enrolled in your FreeIPA realm and has
  `freeipa.ansible_freeipa` installed (every FreeIPA server does). The
  deploy controller needs SSH to that host (the slurp uses
  `delegate_to`).

How it composes:

- The wrapper conditionally extends `swarm_stack_secrets` with
  `mm_ldap_bind_password` (sourced from `vault_mm_ldap_bind_password`)
  and turns `swarm_stack_configs` from `[]` into a one-entry list
  containing `mm_freeipa_ca_cert`. The CA cert is slurped from
  `mattermost_freeipa_ca_cert_src` on `groups['freeipa'][0]` and stored
  in a fact the template references — no vaulting, no copy/paste. mm-app
  picks up the cert via its `configs:` list, mounted at
  `mattermost_ldap_ca_cert_path`.
- The env template grows a `MM_LDAPSETTINGS_*` block under an
  `{% if mattermost_ldap_enabled %}` guard, including
  `MM_LDAPSETTINGS_BINDPASSWORD` baked from `swarm_stack_secret_values`
  and `MM_LDAPSETTINGS_PUBLICCERTIFICATEFILE` pointing at the docker
  config mount.

Verification (post-deploy):

```bash
# Login as the first admin user (you create this via the web UI or API)
curl -k -X POST -d '{"login_id":"admin","password":"..."}' \
     https://mattermost.example.com/api/v4/users/login -i

# Then with the returned token:
curl -k -X POST -H "Authorization: Bearer <token>" \
     https://mattermost.example.com/api/v4/ldap/test
# {"status":"OK"}

curl -k -X POST -H "Authorization: Bearer <token>" \
     https://mattermost.example.com/api/v4/ldap/sync
# {"status":"OK"}
```

If real LDAP login fails with `User.IsValid: Invalid email`, that's a
directory-data issue: the FreeIPA user has no `mail` attribute. Fix in
FreeIPA (`ipa user-mod <uid> --email=<uid>@<domain>`), don't chase it in
Mattermost config.

### TLS verification against a self-signed CA

`MM_LDAPSETTINGS_PUBLICCERTIFICATEFILE` (which the role wires up through
the docker config we ship) is the path to the **mTLS client** cert
Mattermost would present to an LDAP server that requires client auth —
NOT a "trust this CA when verifying the server cert" override. Go's
`crypto/tls` only consults the system CA pool for server verification,
and the FIPS image's pool doesn't include your self-signed FreeIPA CA.
There's no other `MM_LDAPSETTINGS_*` knob to override that — your two
real options are:

1. Rebuild `mm-app` with the FreeIPA CA dropped into `/etc/ssl/certs`
   and `update-ca-certificates` run during build; OR
2. Switch the FreeIPA LDAP service to a publicly-trusted cert (Let's
   Encrypt, etc.).

Until one of those lands, set
`mattermost_ldap_skip_certificate_verification: true` in inventory. The
CA cert is still shipped as a docker config so the future image rebuild
can wire up trust without changing the role.

## Variables

See `defaults/main.yml` for the full list of `mattermost_*` tunables.
