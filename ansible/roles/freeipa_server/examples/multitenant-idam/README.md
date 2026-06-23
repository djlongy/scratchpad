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
| **role** (persona; bundles groups) | `role-<tenant>-<persona>` | `role-acme-lead` → `[acme-payments-admin, acme-ledger-admin]` | tenant `roles:` |
| **user** (assigned roles/groups) | — | `ann.lee` → role `role-acme-lead` | tenant `members:` |

**Type marker:** roles are prefixed `role-` so the FIRST token tells you the kind.
Scan any list and `role-*` = a role, everything else = a plain permission group —
no parsing segments to guess whether `admin` is an app tier or a role. (`role-*`
also matches the export tool's default exclude, so roles are never re-mined into
groups.) Permission groups keep the plain `<tenant>-<app>-<tier>` name.

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
roles:      role-acme-developer  -> [acme-payments-operator, acme-ledger-viewer]
            role-acme-lead       -> [acme-payments-admin,    acme-ledger-admin]
            role-globex-analyst  -> [globex-search-viewer]
            role-globex-owner    -> [globex-search-admin]
users:      ann.lee -> role-acme-lead        bo.ng  -> role-acme-developer
            cj.park -> group acme-payments-viewer (direct, no role)
            dee.fox -> role-globex-owner     eli.mak -> role-globex-analyst
```

## Run it

```bash
# Render-only sanity check (no FreeIPA needed): debug the generated vars.
ansible-playbook -i inventory/hosts.yml site.yml --tags idam --check

# Apply for real (needs realm admin/DM creds via Vault — see role README).
ansible-playbook -i inventory/hosts.yml site.yml --tags idam
```

## Fast, isolated per-team runs (the selector)

A monolithic users/groups list is slow because the role applies groups,
hostgroups, and memberships **one item per API round-trip, every run** — so a
full reconcile scales with the *whole estate* even when nothing changed. (Note
`--limit` does NOT help: it limits hosts, and there is one FreeIPA host.)

Scope a run to one team with `idam_tenants`, a vars-level filter:

```bash
# Someone joined team ACME — apply ONLY acme. Seconds, not minutes.
ansible-playbook -i inventory/hosts.yml site.yml --tags idam -e idam_tenants=acme
# A few teams at once (JSON list):
ansible-playbook -i inventory/hosts.yml site.yml --tags idam -e '{"idam_tenants":["acme","globex"]}'
# No selector → all tenants (the full reconcile).
```

The role then iterates only ACME's items; GLOBEX is never composed, queried, or
pruned.

**Why this is safe — per-tenant marker groups.** The role reconciles ONE marker
group and deletes managed users not in the desired set. With a single global
marker, running only ACME would see GLOBEX's users as "removed" and delete them.
So `10_aggregate.yml` derives `freeipa_idam_managed_group` per selected tenant:

| Run | Selected | Marker group | Touches |
|-----|----------|--------------|---------|
| `(* none *)` | acme, globex | `idam-managed-users` | everything (full reconcile) |
| `-e idam_tenants=acme` | acme | `idam-managed-acme` | ACME only — GLOBEX untouched |
| `-e idam_tenants=globex` | globex | `idam-managed-globex` | GLOBEX only |

So an ACME run reconciles only `idam-managed-acme`; GLOBEX's marker is never read.

**Full run over all tenants:** a single play carries one marker, so the global
`idam-managed-users` reconcile is fine when the desired set IS everyone. If you
want per-tenant marker isolation on the full run too, loop the role once per
tenant (each iteration `-e idam_tenants=<code>`) instead of one big pass — e.g. a
wrapper that runs the targeted command for each `tenant_*` file. Per-item API
cost is inherent to ansible_freeipa; the win is not iterating teams that didn't
change.

> One-time migration note: moving an existing realm from a single
> `idam-managed-users` marker to per-tenant markers is graceful — the first
> targeted run finds an empty per-tenant marker (nothing to prune) and populates
> it. Run each team once to seed its marker, then retire the global one.

## Scaling checklist

- **Add a team** → drop a new `tenant_<code>.yml`. Nothing else to update — it is
  auto-discovered; there is no count or registry to maintain.
- **Add an app** → one line in the tenant's `apps:`; its groups/hostgroup appear.
- **Add a persona** → one entry in the tenant's `roles:`.
- **Add a person** → one `members:` line referencing a role, then apply just that
  team: `-e idam_tenants=<code>` (seconds; other teams untouched).
- **Add an environment** → a sibling inventory reusing the same `tenant_*.yml`,
  changing only `env` in `00_naming.yml`.
- **Tighten tiers for a team** → set `tiers:` in that tenant's spec.

> Sanitised example — fictional tenants (ACME/GLOBEX), `example.com`, and RFC1918
> addresses. Replace with your own before use.
