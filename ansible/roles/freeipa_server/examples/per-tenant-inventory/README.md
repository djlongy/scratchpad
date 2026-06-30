# Per-tenant inventory (unified per-realm identity)

A runnable, sanitised template for the **unified per-realm identity model**: one FreeIPA realm,
its whole identity split into **per-tenant files** that the role loads and reconciles in **one
run**. Point it at your IPA primary, supply one admin credential, and run `--tags idam`.

```
per-tenant-inventory/
├── inventory.yml                 # the IPA primary (one realm hosts every tenant)
├── ansible.cfg                   # loads the role + its filter_plugins
├── site.yml                      # applies the freeipa_server role
├── group_vars/all/
│   └── realm.yml                 # realm-level connection + tenants_dir + shared baselines
└── tenants/                      # ← freeipa_idam_tenants_dir points here
    ├── global.yml                # shared/built-in groups + ops users (shared: true)
    ├── acme.yml                  # a tenant, LITERAL data (what --tags export emits)
    └── globex.yml                # the same, written with TEMPLATING (self-ref + group_var pull)
```

## The model

`freeipa_idam_tenants_dir` (set in `realm.yml` to `{{ inventory_dir }}/tenants`) makes the role
read **every** `tenants/*.yml`, flatten them into the native `freeipa_idam_*` lists, and stamp
each user/group with its owning `tenant` (and `shared`). Seeing all tenants in one run is what
lets a single declarative reconcile manage membership of even a **shared/built-in** group
correctly — `acme.dave` is declared once with `[acme-admins, admins]`, and the run reconciles
both his tenant group and the global `admins` (owned in `global.yml`) together.

Each file may carry a tenant's **whole** config — not just `users`/`groups` but `hostgroups`,
`hbac_rules`, `sudo_rules`, `roles`, `automember`, `dns_records`, … Use the short hand-friendly
key (`users`, `groups`, `hbac_rules`, `dns_records`, …) or the full `freeipa_idam_*` /
`freeipa_server_*` var (e.g. straight from an export snapshot).

### Two ways to write a tenant file — both are plain `.yml`

A tenant file is loaded with `include_vars`, so it behaves **exactly like any inventory vars
YAML**. There is no special extension.

- **`acme.yml`** — literal data. The natural form for an exported tenant: just names and values.
- **`globex.yml`** — templated. `{{ tenant }}` / `{{ prefix }}` self-reference the file's own
  header keys, and `freeipa_idam_sudo_commands: "{{ shared_sudo_commands }}"` pulls a whole list
  **in from `group_vars`** (it stays a native list). Ordinary quoted Jinja, like any vars file.

## Run it

```bash
# supply ONE admin credential source first (see group_vars/all/realm.yml):
#   -e freeipa_server_admin_password='...'   (best from -e @secrets.yml)   OR a Vault path
ansible-playbook -i inventory.yml site.yml --tags idam              # add --check --diff to preview
ansible-playbook -i inventory.yml site.yml --tags idam              # re-run -> changed=0 (idempotent)
```

Verify on the primary:

```bash
ipa user-show acme.dave --all          # Member of group: acme-admins, admins
ipa group-show ug-globex-admins        # Member users: globex.sam
ipa dnsrecord-find ipa.example.com.    # app1, app2 A records
```

## Pruning (optional)

By default the role is **additive** — it never deletes. To make removals authoritative (a user
dropped from a tenant file loses the membership; a group dropped from a file is removed), set
`freeipa_server_authoritative: true` and a `freeipa_idam_reconcile_scope`. Authoritative is
**realm-scoped** — only run it against the *complete* assembled desired state for the realm (all
tenant files together), never a partial subset. See the role README for the full pruning model.

## See also

- `../rbac-overlay/` — the optional thin RBAC overlay (assign abstract roles instead of many
  groups), layered on top of native dicts. Composes with this model.
