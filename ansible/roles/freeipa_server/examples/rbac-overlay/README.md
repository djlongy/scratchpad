# Thin RBAC overlay → FreeIPA (3 tenants × 3 environments)

A runnable, sanitised template for a **3-tenant × 3-environment** estate (acme / globex /
initech × dev / test / prod).

**The overlay is a purely optional add-on, compiled by the role itself** (`tasks/rbac.yml`,
inside the desired phase). The role consumes only the native `freeipa_idam_*` dicts; when
`freeipa_server_rbac_roles` is declared it validates + compiles + merges the overlay onto them
(generating **only** role groups, their nesting into the existing `ug-*` groups, and
user→role-group membership — nothing else). No playbook `pre_tasks` are needed, and with no
overlay vars declared — or empty/null ones — every overlay task no-ops: a pure-baseline realm
runs untouched. It also composes with the per-tenant `freeipa_idam_tenants_dir` mode (the
overlay compiles after the tenant load). You author one file: `group_vars/all/10_rbac.yml`.

The native dicts in `group_vars/all/00_native.yml` (policy groups `ug-*`, their HBAC + sudo
rules, hostgroups, users) are the **source of truth** — exactly what `--tags export` drops out
of a live realm.

```
rbac-overlay/
├── inventory.yml                    # the IPA primary (one realm hosts all tenants)
├── ansible.cfg                      # loads the role + its filter_plugins (the compiler)
├── site.yml                         # just the role — it compiles the overlay itself
└── group_vars/all/
    ├── 00_native.yml                # NATIVE ug-* policy groups + HBAC/sudo/hostgroups + users
    └── 10_rbac.yml                  # the overlay data: freeipa_server_rbac_roles flat list (WYSIWYG)
```

> **Fall back to raw dicts any time:** drop `10_rbac.yml` and you have a plain native-dict
> deployment — the role is identical either way.

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
only nests onto them — it never invents `ug-*` groups. `freeipa_rbac_validate` fails the run
(naming the culprit) on a policy group not declared in `00_native.yml` (the typo trap for pasted
names), a member not in `freeipa_idam_users`, a duplicate/protected name, an unknown key
(`member` vs `members`), or a role name that is also a policy group.

## Roles in this example

| role group (literal name) | nests into | members |
|---|---|---|
| `role-acme-prod-platform-admin` | `ug-acme-prod-gitlab-admins`, `ug-acme-prod-docker-operators` | alice.smith |
| `role-acme-test-db-admin` | `ug-acme-test-postgres-admins` | — |
| `role-acme-dev-ops` | `ug-acme-dev-monitoring-admins` | — |
| `role-initech-prod-db-admin` | `ug-initech-prod-postgres-admins` | carol.fox |
| `role-initech-dev-ops` | `ug-initech-dev-monitoring-admins` | dana.li |
| `role-globex-{dev,test,prod}-viewer` | `ug-globex-<env>-grafana-readers` | bob.ng (+dana.li in prod) |
| `role-globex-prod-ops` | `ug-globex-prod-monitoring-admins` | dana.li |

**WYSIWYG, so isolation is self-evident.** Every role is its own literal group —
`role-initech-prod-db-admin` and `role-acme-test-db-admin` are two different names, so a grant
can never fan out across tenants or environments. `carol.fox` holds initech's db-admin **only**;
to span environments you list the user in each role entry explicitly (`bob.ng` appears in all
three globex viewer entries). Granting a role is a one-line diff on that entry's `members:`.

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
