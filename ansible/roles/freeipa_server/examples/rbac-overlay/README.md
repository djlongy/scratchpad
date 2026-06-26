# Thin RBAC overlay → FreeIPA (3 tenants × 3 environments)

A runnable, sanitised template for a **3-tenant × 3-environment** estate (acme / globex /
initech × dev / test / prod).

**The `freeipa_server` role knows nothing about this overlay.** The role consumes only the
native `freeipa_idam_*` dicts. The overlay is layered on top by **three external, decoupled
pieces** — drop them and the role runs from raw dicts unchanged:

1. **The filter plugin** `../../filter_plugins/freeipa_rbac.py` — the compiler, a Jinja
   filter loaded via `ansible.cfg`. (Generates **only** role groups, their nesting into the
   existing `ug-*` groups, and user→role-group membership — nothing else.)
2. **The group_vars** `group_vars/all/10_rbac.yml` — `freeipa_server_rbac_*`, the overlay
   data you author.
3. **A pre_task** — `freeipa_server_rbac_compile.yml`, one reusable tasks file the playbook includes from
   its `pre_tasks` (`import_tasks: freeipa_server_rbac_compile.yml`). It validates + compiles + merges the
   overlay into the native `freeipa_idam_*` dicts **before** the role runs. It reads the
   in-scope group_vars (nothing is passed in), and is null-safe + gated: no `role_sets`
   declared ⇒ every task no-ops ⇒ pure baseline. Include it from *every* playbook instead of
   copy-pasting the compile steps.

The native dicts in `group_vars/all/00_native.yml` (policy groups `ug-*`, their HBAC + sudo
rules, hostgroups, users) are the **source of truth** — exactly what `--tags export` drops out
of a live realm.

```
rbac-overlay/
├── inventory.yml                    # the IPA primary (one realm hosts all tenants)
├── ansible.cfg                      # loads the role + its filter_plugins (the compiler)
├── site.yml                         # pre_tasks import the compile, then the role
├── freeipa_server_rbac_compile.yml  # the reusable compile (set_facts merging overlay → native)
└── group_vars/all/
    ├── 00_native.yml                # NATIVE ug-* policy groups + HBAC/sudo/hostgroups + users
    └── 10_rbac.yml                  # the overlay data: freeipa_server_rbac_role_sets + assignments
```

> **Fall back to raw dicts any time:** drop `10_rbac.yml` and the `freeipa_server_rbac_compile.yml` include
> from `site.yml` and you have a plain native-dict deployment — the role is identical either way.

## The model

```
user ──member──▶ role-acme-prod-platform-admin   (role group — the overlay creates this)
                      └─nested into─▶ ug-acme-prod-gitlab-admins   (native policy group ◀ HBAC/sudo)
                      └─nested into─▶ ug-acme-prod-docker-operators
```

A user is a **direct** member of only the role group, and an **indirect** member of every
policy group it nests into — so the native HBAC/sudo rules that target `ug-*` apply unchanged.
Add/remove a person ⇒ one role assignment, not edits across many groups.

**Policy groups must already exist natively** (that is where the HBAC/sudo point). The overlay
only nests onto them — it never invents `ug-*` groups. `validate_rbac` fails the run (naming the
culprit) if a role nests into a `ug-*` not declared in `00_native.yml`, if an assignment names
an unknown role, or if a user is not in `freeipa_idam_users`.

## Roles in this example

| role | cells (tenant/env) | nests into |
|---|---|---|
| `platform-admin` | acme/prod | `ug-acme-prod-gitlab-admins`, `ug-acme-prod-docker-operators` |
| `viewer` | globex/dev, globex/test, globex/prod | `ug-globex-<env>-grafana-readers` |
| `db-admin` | initech/prod, acme/test | `ug-<t>-<e>-postgres-admins` |
| `ops` | acme/dev, globex/prod, initech/dev | `ug-<t>-<e>-monitoring-admins` |

A role name defined for one cell scopes there; defined for several cells (like `viewer` across
all three globex envs) it grants membership in each. `dana.li` holds `[ops, viewer]` → ops in
three tenants **and** the globex grafana viewer.

## Run it

```bash
# supply ONE admin credential source first (see inventory.yml):
#   -e freeipa_server_admin_password='...'   (best from -e @secrets.yml)   OR a Vault path
ansible-playbook -i inventory.yml site.yml --tags idam              # add --check --diff to preview
ansible-playbook -i inventory.yml site.yml --tags idam              # re-run → changed=0 (idempotent)
```

Verify on the primary:

```bash
ipa group-show ug-acme-prod-gitlab-admins        # Member groups: role-acme-prod-platform-admin
ipa user-show  alice.smith --all                 # Indirect Member of group: ug-acme-prod-*
ipa group-show role-acme-prod-platform-admin     # Member users: alice.smith
```

## Pruning (optional)

By default the role is **additive** — it never deletes. To make removals authoritative (a user
dropped from a role loses it; a `ug-*` dropped from `00_native.yml` is deleted), set
`freeipa_server_authoritative: true` and a `freeipa_idam_reconcile_scope`. Authoritative is
**realm-scoped** — only run it against the *complete* assembled desired state for the realm,
never a partial file. See the role README for the full pruning model.

## Not generated by the overlay

HBAC rules, sudo rules/commands, hostgroups, DNS, automember, IPA permissions/privileges/roles,
and password policies are all plain native entries (`00_native.yml`). The overlay's entire job
is the role-group abstraction over **users and groups** — nothing else.
