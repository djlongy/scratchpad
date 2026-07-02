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
    ├── acme.yml                  # a tenant, LITERAL data (what --tags export emits) + its RBAC slice
    └── globex.yml                # the same, TEMPLATED (self-ref + group_var pull) + its RBAC slice
```

## The model

`freeipa_idam_tenants_dir` (set in `realm.yml` to `{{ inventory_dir }}/tenants`) makes the role
read **every** `tenants/*.yml`, flatten them into the native `freeipa_idam_*` lists, and stamp
each user/group with its owning `tenant` (and `shared`). Seeing all tenants in one run is what
lets a single declarative reconcile manage membership of even a **shared/built-in** group
correctly — `acme.dave` is declared once with `[acme-admins, admins]`, and the run reconciles
both his tenant group and the global `admins` (owned in `global.yml`) together.

Each file may carry a tenant's **whole** config — users, groups, hostgroups, HBAC rules, sudo
rules, roles, automember, DNS records, … Every tenant file here uses the **native root keys
exactly as `--tags export` emits them** (`freeipa_idam_usergroups`, `freeipa_idam_users`,
`freeipa_idam_hbac_rules`, `freeipa_server_dns_records`, …) with the export's entry fields
(`givenname`/`sn`/`email`), so an exported realm drops into a tenant file unchanged. The loader
also accepts short hand-friendly aliases (`users`, `groups`, `hbac_rules`, …) if you prefer to
hand-author, but the export-consistent form is what these examples show.

### Two ways to write a tenant file — both are plain `.yml`

A tenant file is loaded with `include_vars`, so it behaves **exactly like any inventory vars
YAML**. There is no special extension.

- **`acme.yml`** — literal data. The natural form for an exported tenant: just names and values.
- **`globex.yml`** — fully header-driven. Every tenant/env-derived name, description, automember
  regex and DNS name is templated from the header block, so **duplicating the tenant for a new
  tenancy or environment means changing only the header vars** — the whole file follows. It also
  shows `freeipa_idam_sudo_commands: "{{ shared_sudo_commands }}"` pulling a whole list **in
  from `group_vars`** (it stays a native list). Ordinary quoted Jinja, like any vars file.

### Header vars — `tenant`, `shared`, and your own naming tokens

A tenant file is a normal vars file, so **any scalar declared in it can be referenced by every
other value in the same file**. Only two keys mean something to the loader itself:

| Key | Purpose / effect |
|---|---|
| `tenant` | Ownership stamp — records who owns each user/group (feeds scoping/reporting). Also handy as `{{ tenant }}` in names. |
| `shared` | Marks this slice's groups as grantable from any tenant (see `global.yml`). |

Everything else is **yours to invent** — `globex.yml` demonstrates the two substitution styles:

```yaml
tenant: globex
env_local: dev                                    # a per-file naming token (not a realm switch)
ug_prefix: "ug-{{ tenant }}-{{ env_local }}"      # COMPOUND prefix: built once, used everywhere
```

- **Individual tokens** — `name: "{{ tenant }}-admins"`, `description: "... [{{ tenant }}/{{ env_local }}]"`
- **Compound prefixes** — `name: "{{ ug_prefix }}-docker-operators"`: one header line defines
  the whole naming scheme; HBAC rules reference the same `{{ ug_prefix }}`/`{{ hg_prefix }}`
  so rule, member group and hostgroup can never drift apart.

These feed **naming only** — no header var switches the target realm/host, changes the reconcile
scope, or alters service FQDNs (`{{ env }}` still resolves to the inventory-wide `env` if you
use it). The `-<tenant>-<env>-` segment they produce is what a
`freeipa_idam_reconcile_scope: "<tenant>-<env>-"` later matches on.

### RBAC overlay — each tenant file carries its own slice, merged realm-wide

The optional RBAC overlay (`freeipa_server_rbac_roles`, the WYSIWYG flat list — see
[`../rbac-overlay/`](../rbac-overlay/) for the model) **rides the tenant loader like any other
`freeipa_*` list**: every `tenants/*.yml` may declare its own slice, the loader **concatenates**
them across files, and the role compiles + validates the merged list **after** the tenant load.
In this example:

- **`acme.yml`** declares `role-acme-dev-platform-admin` + `role-acme-test-observer`, nesting
  into the `ug-acme-*` policy groups declared **in the same file**, and grants `ops.editor` —
  a user owned by `global.yml`.
- **`globex.yml`** declares its own roles and makes a **cross-tenant grant**: `acme.dave`
  (owned by `acme.yml`) is a member of `role-globex-test-observer`.

Both resolve because validation runs on the **realm-wide merged** `freeipa_idam_usergroups` /
`freeipa_idam_users` — a role's `policy_groups` and `members` may reference anything declared
by *any* tenant file in the run. Policy groups must still exist natively somewhere (typo trap
stays armed); the overlay only ever creates the `role-*` groups themselves.

**Declare the overlay in ONE place**: tenant files *or* `group_vars` — a tenant-declared
`freeipa_server_rbac_roles` **replaces** a `group_vars` value (Ansible var precedence), it does
not merge with it. Splitting slices across tenant files (as here) is the per-tenant way.

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
# RBAC overlay, merged across tenant files (cross-tenant grant included):
ipa group-show role-globex-test-observer     # Member users: globex.dave, acme.dave
ipa group-show ug-acme-test-grafana-readers  # Member groups: role-acme-test-observer
ipa user-show ops.editor --all               # Indirect member of: ug-acme-test-grafana-readers
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
