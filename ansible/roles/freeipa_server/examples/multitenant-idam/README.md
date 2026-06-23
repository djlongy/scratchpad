# Multi-tenant IDAM for `freeipa_server` — a scalable reference template

One FreeIPA realm, many teams, each owning a small per-tenant file. Groups, roles,
and users are **generated from a single naming convention**, so every tenant is
consistent by construction and you never hand-type a group or role name.

This is a self-contained, runnable example. Copy it, replace the two `tenant_*.yml`
files with your teams, and point it at your realm.

```
multitenant-idam/
├── site.yml                         # apply play (with the load-guard assert)
└── inventory/
    ├── hosts.yml                    # ONE freeipa host; tenants composed onto it
    └── group_vars/all/              # a DIRECTORY → every file loads for the host
        ├── 00_naming.yml            # the convention: env, domain, patterns, tiers
        ├── 10_aggregate.yml         # discovery + generators → freeipa_idam_*
        ├── tenant_acme.yml          # Tenant ACME — data only
        └── tenant_globex.yml        # Tenant GLOBEX — data only (tier override)
```

## Why this works — two Ansible facts you must respect

1. **Ansible does NOT merge same-named vars across group_vars.** If two team files
   both set `freeipa_idam_users`, the highest-precedence one wins and the rest are
   **silently lost**. → Each tenant uses a distinct `tenant_<code>_spec`; the
   `varnames` lookup in `10_aggregate.yml` discovers them all and renders the
   convention into the single `freeipa_idam_*` lists the role consumes. Files live in
   `group_vars/all/` (a *directory*) so each loads automatically — drop in a new
   `tenant_x.yml` and it is picked up. **No role change is required.**

2. **The role's prune step is global.** `freeipa_server` reconciles ONE marker group
   (`freeipa_idam_managed_group`) and deletes any managed user not in the desired
   list. So run **once with the union of all tenants** (what this produces) — *not*
   per-tenant with `--limit`, which would treat the other teams' users as removed.
   `site.yml` asserts the tenant count first, so a tenant file that fails to load
   aborts the run instead of pruning that team.

## The three layers (smallest grain up)

| Layer | Pattern | Generated example | Defined in |
|-------|---------|-------------------|-----------|
| **group** (atomic permission) | `<tenant>-<app>-<tier>` | `acme-payments-admin` | `apps × tiers` |
| **role** (persona; bundles groups) | `<tenant>-<persona>` | `acme-lead` → `[acme-payments-admin, acme-ledger-admin]` | tenant `roles:` |
| **user** (assigned roles/groups) | — | `ann.lee` → role `acme-lead` | tenant `members:` |

Plus `hostgroup` (`<tenant>-<app>`), host FQDN (`<env>-<tenant>-<app>-<NN>.<domain>`),
and service URL (`<app>.<tenant>.<env>.<domain>`). All patterns live in `00_naming.yml`.

A **role is a named bundle of groups**; a **user references roles** (preferred) and/or
a direct group. The role expands `user.roles` → the union of their groups at apply
time, so a roles-only user is valid. A team adds a person by adding one `members:`
line referencing a persona — no group math.

## What this example produces (verified with `ansible-playbook`)

```
tenants: [acme, globex]
groups (8): acme-payments-{admin,operator,viewer}, acme-ledger-{admin,operator,viewer},
            globex-search-{admin,viewer}                 # globex narrowed its tiers
roles:      acme-developer  -> [acme-payments-operator, acme-ledger-viewer]
            acme-lead       -> [acme-payments-admin,    acme-ledger-admin]
            globex-analyst  -> [globex-search-viewer]
            globex-owner    -> [globex-search-admin]
users:      ann.lee -> role acme-lead        bo.ng  -> role acme-developer
            cj.park -> group acme-payments-viewer (direct, no role)
            dee.fox -> role globex-owner      eli.mak -> role globex-analyst
```

## Run it

```bash
# Render-only sanity check (no FreeIPA needed): debug the generated vars.
ansible-playbook -i inventory/hosts.yml site.yml --tags idam --check

# Apply for real (needs realm admin/DM creds via Vault — see role README).
ansible-playbook -i inventory/hosts.yml site.yml --tags idam
```

## Scaling checklist

- **Add a team** → drop `tenant_<code>.yml`, bump `identity_expected_tenant_count`.
- **Add an app** → one line in the tenant's `apps:`; its groups/hostgroup appear.
- **Add a persona** → one entry in the tenant's `roles:`.
- **Add a person** → one `members:` line referencing a role.
- **Add an environment** → a sibling inventory reusing the same `tenant_*.yml`,
  changing only `env` in `00_naming.yml`.
- **Tighten tiers for a team** → set `tiers:` in that tenant's spec.

> Sanitised example — fictional tenants (ACME/GLOBEX), `example.com`, and RFC1918
> addresses. Replace with your own before use.
