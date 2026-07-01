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

### Header keys — `tenant`, `prefix`, `env_override` (naming aids only)

A tenant file may set a few scalar header keys that the loader exposes to the file's **own**
Jinja, so object names can self-reference them:

| Key | `{{ … }}` in the file | Purpose / effect |
|---|---|---|
| `tenant` | `{{ tenant }}` | Per-tenant token **and** an ownership stamp (records who owns each user/group). |
| `prefix` | `{{ prefix }}` | A naming prefix token, e.g. `ug`. |
| `env_override` | `{{ env }}` | **Optional** per-file override of the inventory-wide `env`. Omit it and `{{ env }}` inherits the realm's `env` from inventory; set `env_override: dev` only when *this file's* object names must differ. |

These feed **naming only** — none of them switches the target realm/host, changes the reconcile
scope, or alters service FQDNs. The header key is `env_override` (not `env`) precisely so it
can't be mistaken for an environment switch. `{{ env }}` in a name becomes the `-prod-`/`-dev-`
segment that a `freeipa_idam_reconcile_scope: "<tenant>-<env>-"` later matches on.

### Testing destructive plays: QA realm first, then live

Running the *same* identity against a **QA FreeIPA** before the **live** one is an **inventory**
concern, not a tenant-file one. Keep the tenant files identical and select the realm by inventory:

```
inventories/
  qa/    { inventory.yml (QA IPA host), group_vars/all/realm.yml, tenants/ }
  live/  { inventory.yml (live IPA host), group_vars/all/realm.yml, tenants/ }
```

Only each `realm.yml` differs — it points at that instance:

```yaml
# qa/group_vars/all/realm.yml           # live/group_vars/all/realm.yml
env: qa                                 # env: prod
freeipa_server_domain: ipa.qa.example.com   # freeipa_server_domain: ipa.example.com
freeipa_server_realm:  IPA.QA.EXAMPLE.COM   # freeipa_server_realm:  IPA.EXAMPLE.COM
```

Share the identity by pointing both inventories' `freeipa_idam_tenants_dir` at one common
`tenants/` (a shared path or a symlink) so there is a single source of truth. Then:

```bash
# 1) Rehearse the destructive run on QA (scope + authoritative), preview first:
ansible-playbook -i inventories/qa/inventory.yml site.yml --tags idam \
  -e freeipa_server_authoritative=true -e freeipa_idam_reconcile_scope=acme- --check --diff
ansible-playbook -i inventories/qa/inventory.yml site.yml --tags idam \
  -e freeipa_server_authoritative=true -e freeipa_idam_reconcile_scope=acme-

# 2) Confident? Same command, live inventory:
ansible-playbook -i inventories/live/inventory.yml site.yml --tags idam \
  -e freeipa_server_authoritative=true -e freeipa_idam_reconcile_scope=acme-
```

Because `env` (and the domain/realm/host) come from the selected inventory, the identical tenant
files apply faithfully to whichever instance you target — QA validates exactly what live will do.

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
