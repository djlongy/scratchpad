# Swarm-stack template

Reusable pattern for deploying any application stack onto an existing
Docker Swarm. Built on `community.docker.docker_swarm_service` with
externalised resources (encrypted overlay network with pinned subnet,
NFS-backed local-driver volumes pre-created on every candidate worker,
content-versioned docker secrets and configs) and a per-service
deploy loop. Designed to be resilient to Ansible's lazy variable
evaluation and to support full destroy → recreate via a single tag.

## Roles

- **`app_swarm_stack`** — generic. Owns the mechanics: NFS subpath
  ensure (delegated to the NFS host), encrypted overlay network with
  optional pinned subnet, NFS-backed volume pre-creation on every
  worker, docker secret/config create with `rolling_versions: true`,
  per-service deploy via `docker_swarm_service`, prune of obsolete
  labeled objects, and a teardown phase gated behind `--tags redeploy`
  or `--tags teardown`.

- **`app_mattermost_swarm`** — thin wrapper showing the pattern.
  Hands a complete `swarm_stack_*` spec to the generic role and
  references the registry for its overlay subnet.

## Layout

```
swarm-stack-template/
├── inventory/
│   ├── hosts.yml
│   └── group_vars/
│       └── swarm_bootstrap/
│           ├── main.yml             # tunables + swarm_overlay_subnets registry
│           └── vault.yml.example    # secret values (ansible-vault)
├── playbooks/
│   └── mattermost_swarm.yml         # deploy entry point
└── roles/
    ├── app_swarm_stack/             # generic
    └── app_mattermost_swarm/        # thin wrapper
```

## How a deploy flows

1. Caller play targets `swarm_bootstrap` (one swarm manager).
2. Wrapper role assembles `swarm_stack_*` variables and `include_role`s
   the generic role.
3. Generic role:
   - Asserts every entry in `swarm_stack_services` has a `name` and a
     valid `image_key` referencing `swarm_stack_images`.
   - mkdirs the NFS subpaths (delegate_to the NFS host).
   - Reads each declared secret value from Ansible's variable scope
     (loaded from `group_vars/.../vault.yml`), creates a docker secret
     per entry, name suffixed `_v1`, `_v2`, … on content change.
   - Renders + creates docker configs (same versioning).
   - Pre-creates the encrypted overlay network with the pinned subnet.
   - Pre-creates each NFS-backed local-driver volume on every candidate
     worker (so swarm can schedule there without on-demand mounts).
   - Iterates `swarm_stack_services` and calls `docker_swarm_service`
     per entry. Each service's env block can come from either:
     - `env_template` — a Jinja YAML file rendered LATE so it can
       reference `swarm_stack_secret_values` and `_config_names`, or
     - `env_static` — a literal dict for services with no late-binding.
   - Prunes labeled objects no longer referenced (skips in-use).

## Resilience to lazy var eval

The whole point of splitting `swarm_stack_services` (top-level scalars)
from `env_template` (file path resolved lazily) is to avoid a sharp
edge in Ansible's `include_role` semantics. The vars block on
`include_role` is evaluated EAGERLY when the role is included — so any
expression inside `swarm_stack_services` that references something the
role itself populates (`swarm_stack_secret_values.x`,
`swarm_stack_config_names.y`) blows up before the role even gets to
the secrets/configs phase. By moving those references into a separate
template file referenced by *path*, the role can render them at deploy
time when the secret/config registries are populated.

The role only ever reads top-level fields from each service entry
(`item.name`, `item.image_key`, `item.networks`, etc), and uses
`item.get('update', omit)` for the `update` key — `update` is a dict
method name and `item.update` resolves to the bound method, not the
dict value, which Ansible chokes on with "builtin_function_or_method
is not JSON serializable". Don't fall into that trap when adding new
fields named after dict methods (`update`, `keys`, `values`, `items`,
`get`, `pop`, `clear`, `copy`, `setdefault`).

## Image URL map

`swarm_stack_images` centralises every image URL the stack uses.
Services reference an entry by `image_key`. Swap registry or tag once
in the wrapper and every service that uses that key picks it up — no
search-and-replace across templates.

```yaml
swarm_stack_images:
  mm_postgres: "{{ mattermost_postgres_image }}"
  mm_app: "{{ mattermost_app_image }}"

swarm_stack_services:
  - name: mm-postgres
    image_key: mm_postgres
    ...
  - name: mm-app
    image_key: mm_app
    ...
```

## Overlay subnet registry

Pin every overlay's subnet via a single map in inventory:

```yaml
# inventory/group_vars/swarm_bootstrap/main.yml
swarm_overlay_subnets:
  mattermost: "10.40.10.0/24"
  splunk:     "10.40.11.0/24"
  # next free: 10.40.12.0/24
```

Each wrapper looks up its subnet by stack name:

```yaml
swarm_stack_networks:
  - name: mm_overlay
    attachable: true
    encrypted: true
    subnet: "{{ swarm_overlay_subnets[mattermost_stack_name] }}"
```

The registry is the single source of truth — collisions between
overlays show up as a diff to one file at PR review, not as silent
east-west traffic black-holes between two stacks that happened to grab
overlapping ranges from swarm's default pool.

## Redeploy + teardown

Three tag-gated flows:

- **default (no tags)** — full create/update chain (idempotent).
- **`--tags redeploy`** — destroy everything this stack owns
  (services, then label-scoped secrets/configs/networks/volumes), then
  re-run the full create chain. NFS data on disk is preserved.
- **`--tags teardown`** — destroy only, leave nothing running.
- **`--tags wipe-data`** (combined with redeploy or teardown) — also
  rm -rf the NFS data directories. Destructive.

```bash
ansible-playbook -i inventory playbooks/mattermost_swarm.yml                      # idempotent update
ansible-playbook -i inventory playbooks/mattermost_swarm.yml --tags redeploy      # full destroy + recreate, data preserved
ansible-playbook -i inventory playbooks/mattermost_swarm.yml --tags teardown      # destroy only
ansible-playbook -i inventory playbooks/mattermost_swarm.yml --tags redeploy,wipe-data  # destroy + wipe data + recreate
```

Service discovery for teardown filters on both `app_swarm_stack=<name>`
label (services this role created) AND name prefix `<stack>_`
(services left over from a legacy `docker stack deploy` that didn't
carry the role's label) so cutover from a stack-deployed predecessor
works.

## Secret pattern

Two kinds of secrets fit cleanly:

- **File-as-secret** (postgres, redis, anything that reads `*_FILE`).
  Listed in `swarm_stack_secrets`, then referenced by name in a
  service's `secrets:` list. Role mounts the file at
  `/run/secrets/<name>` and resolves the hashed name automatically.
  Use `POSTGRES_PASSWORD_FILE: /run/secrets/mm_pg_password` in the
  service env.

- **Bake-into-env** (apps that don't read `/run/secrets/`). Same
  `swarm_stack_secrets` entry, but the env_template references
  `swarm_stack_secret_values.<name>` to bake the value into the env
  dict. Plaintext lives only in raft (encrypted) inside the service
  spec.

Mattermost is in the second camp for most of its secrets — it can't
run with a read-only config.json mount (writes its version stamp on
first boot), so this template uses `MM_*` env vars in a per-service
env template that pulls from `swarm_stack_secret_values`.

## Worker node setup (one-time)

The `community.docker.docker_*` modules need the Docker Python SDK on
both the manager AND every worker (the `delegate_to` volume creation
runs the module against each worker). On AlmaLinux/Rocky/RHEL 9:

```bash
sudo dnf install -y python3-docker python3-requests python3-jsondiff
```

(`python3-jsondiff` lives in EPEL.)

## Encrypted overlay firewall (one-time)

Encrypted overlays wrap VXLAN in ESP (protocol 50) and NAT-T (UDP
4500). Without firewall rules for both, east-west traffic between
swarm hosts on the encrypted overlay times out at the TCP layer with
no obvious error. On every swarm host:

```bash
sudo firewall-cmd --permanent --add-protocol=esp
sudo firewall-cmd --permanent --add-port=4500/udp
sudo firewall-cmd --reload
```

## Adding a new app

1. Copy `roles/app_mattermost_swarm/` to `roles/app_<svc>_swarm/`.
2. Edit `defaults/main.yml` for the new app's tunables (NFS paths,
   image URLs, replica counts, port, vault path placeholders).
3. Edit `tasks/main.yml` — change the `swarm_stack_*` block:
   - `swarm_stack_images` map for the new app's images.
   - `swarm_stack_networks` referencing
     `swarm_overlay_subnets[<stack_name>]`.
   - `swarm_stack_volumes` for NFS subpaths.
   - `swarm_stack_secrets` referencing your vault variables.
   - `swarm_stack_services` with one entry per swarm service —
     `image_key`, `networks`, `mounts`, `publish`, `secrets`,
     `placement`, etc.
4. Add per-service env templates under `templates/<service>.env.yml.j2`
   for any service whose env needs late-bound secret/config values.
5. Add a playbook in `playbooks/` that invokes the new wrapper role.
6. Add `<stack_name>: "<subnet>"` to `swarm_overlay_subnets` in
   `inventory/group_vars/swarm_bootstrap/main.yml`.
7. Add the app's secret variables to your encrypted vault file.

## Vault file

Copy `vault.yml.example` to `vault.yml`, populate values, then encrypt:

```bash
ansible-vault encrypt inventory/group_vars/swarm_bootstrap/vault.yml
```

Run plays with `--ask-vault-pass` (or configure `vault_password_file`
in `ansible.cfg`).

When you adopt HashiCorp Vault later, swap `tasks/create_secrets.yml`
to read via `vault kv get` (or
`community.hashi_vault.vault_kv2_get`) instead of `vars[item.var]` —
the rest of the role doesn't change.

## Rolling-version mechanics

`docker_secret` / `docker_config` with `rolling_versions: true`
inspects existing objects matching `<name>_v[0-9]+`. If the latest
version's content matches what you're submitting, it's a no-op. If
different, it creates `<name>_v(N+1)` and the next deploy uses the new
hashed name → swarm rolls. Prune deletes labeled objects no longer
referenced by the current spec; in-use objects are skipped.

## Knobs worth knowing

- `mattermost_pg_pinned_node` — postgres can't safely run multi-replica
  on shared NFS. Pin to a single worker.
- `mattermost_max_per_node: 1` + `update.order: stop-first` —
  combined, this lets a 3-replica / 3-worker stack roll without a
  deadlock (start-first would wait forever for a free slot).
- `vault_mm_at_rest_key` and `vault_mm_public_link_salt` MUST be ≥ 32
  chars or mattermost refuses to start.

## LDAP / SSO against FreeIPA (optional)

Mattermost's Enterprise Edition image (the FIPS-edition variant too)
runs as **Mattermost Entry** when no license is loaded — and Entry
covers LDAP, SAML, and OpenID, so SSO works without paying. The
template ships a plug-in path for FreeIPA-backed LDAP login via
`MM_LDAPSETTINGS_*` env vars, gated by `mattermost_ldap_enabled`. The
defaults already match FreeIPA conventions (uid attribute,
person/groupOfNames classes, `cn=users,cn=accounts` user container).

Two-step deploy:

```bash
# 1. Provision the svc-mattermost user in FreeIPA + set its bind password
ansible-playbook -i inventory/hosts.yml \
                 playbooks/mattermost_freeipa_prep.yml --ask-vault-pass

# 2. Deploy / redeploy the stack with LDAP wired
ansible-playbook -i inventory/hosts.yml \
                 playbooks/mattermost_swarm.yml --ask-vault-pass --tags redeploy
```

What you fill in:

- `inventory/group_vars/swarm_bootstrap/main.yml` — flip
  `mattermost_ldap_enabled` to `true`, set `mattermost_ldap_host` to
  your FreeIPA server's FQDN. The base-DN, bind username, and
  attribute names default to FreeIPA's conventions; override only if
  you've customised your directory.
- `inventory/group_vars/swarm_bootstrap/vault.yml` — add two values:
    - `vault_freeipa_admin_password` — for the prep play's `ipauser`
      call
    - `vault_mm_ldap_bind_password` — same value gets set on the
      FreeIPA user AND used for the LDAPS bind from Mattermost
  The CA cert is **not** vaulted — the role slurps it from
  `/etc/ipa/ca.crt` on `groups['freeipa'][0]` at deploy time. It's
  public, so vaulting it would just be ceremony.
- `inventory/hosts.yml` — add a `freeipa` group containing at least
  one host that's enrolled in your FreeIPA realm and has
  `freeipa.ansible_freeipa` installed (every FreeIPA server does).
  The deploy controller needs SSH to that host (the slurp uses
  `delegate_to`).

How it composes:

- The wrapper role conditionally extends `swarm_stack_secrets` with
  `mm_ldap_bind_password` (sourced from `vault_mm_ldap_bind_password`)
  and turns `swarm_stack_configs` from `[]` into a one-entry list
  containing `mm_freeipa_ca_cert`. The CA cert is slurped from
  `mattermost_freeipa_ca_cert_src` on `groups['freeipa'][0]` and
  stored in a fact the template references — no vaulting, no
  copy/paste. mm-app picks up the cert via its `configs:` list,
  mounted at `mattermost_ldap_ca_cert_path`.
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
directory-data issue: the FreeIPA user has no `mail` attribute. Fix
in FreeIPA (`ipa user-mod <uid> --email=<uid>@<domain>`), don't
chase it in Mattermost config.

### TLS verification against a self-signed CA

`MM_LDAPSETTINGS_PUBLICCERTIFICATEFILE` (which the role wires up
through the docker config we ship) is the path to the **mTLS client**
cert Mattermost would present to an LDAP server that requires client
auth — NOT a "trust this CA when verifying the server cert" override.
Go's `crypto/tls` only consults the system CA pool for server
verification, and the FIPS image's pool doesn't include your
self-signed FreeIPA CA. There's no other `MM_LDAPSETTINGS_*` knob to
override that — your two real options are:

1. Rebuild `mm-app` with the FreeIPA CA dropped into `/etc/ssl/certs`
   and `update-ca-certificates` run during build; OR
2. Switch the FreeIPA LDAP service to a publicly-trusted cert (Let's
   Encrypt, etc.).

Until one of those lands, set
`mattermost_ldap_skip_certificate_verification: true` in inventory.
The CA cert is still shipped as a docker config so the future image
rebuild can wire up trust without changing the role.

## Notes for porting

This template was extracted from a working homelab deployment. The
homelab version reads secrets from HashiCorp Vault (CLI-driven) and
auto-generates missing values via a passphrase generator. This
template strips that out in favour of an ansible-vault YAML file —
appropriate when you don't have HCV stood up yet. The contract at the
wrapper boundary stays the same `{name, var}` shape, so the swap to
HCV later is local to `tasks/create_secrets.yml`.
