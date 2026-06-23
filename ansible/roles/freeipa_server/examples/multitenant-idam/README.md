# Multi-tenant IDAM for `freeipa_server` — a scalable reference template

One FreeIPA realm, many teams. The **logic is global** (the naming convention +
generators sit next to the playbook, auto-loaded every run), the **host** is one
tiny shared inventory, and **each team is its own inventory directory** holding
only that team's data. You change a team by running *that team's inventory* —
nothing else is loaded, queried, or pruned.

Groups, roles, and users are **generated from a single naming convention**, so every
tenant is consistent by construction and you never hand-type a group or role name.

```
multitenant-idam/
├── apply.sh                         # ./apply.sh acme [globex ...]  — runs the right -i set
├── site.yml                         # the play
├── group_vars/all/                  # GLOBAL logic — adjacent to the playbook, ALWAYS loaded
│   ├── 00_naming.yml                #   env, domain, name patterns, access tiers
│   └── 10_generate.yml              #   tenant_*_spec → freeipa_idam_* + per-tenant marker
├── inventories/
│   ├── _common/hosts.yml            # JUST the realm host (idm01) — the one thing that must be -i
│   ├── acme/  group_vars/all/tenant.yml     # team ACME — data only (tenant_acme_spec)
│   └── globex/group_vars/all/tenant.yml     # team GLOBEX — data only (tenant_globex_spec)
└── alt-single-inventory/            # the same generators, one-inventory layout (comparison)
```

**Why the split is host vs. logic:** Ansible auto-loads `group_vars`/`host_vars`
that sit next to the *playbook*, on every run, with no `-i` — so the convention and
generators live there and are truly global. But that auto-loading is for
*variables*, **not host definitions** — a host can't be declared in playbook
`group_vars`. So one minimal `inventories/_common/hosts.yml` still defines the realm
host and is passed as `-i`. (To drop even that explicit `-i _common`: set it as the
`ansible.cfg` default inventory, or duplicate the one host block into each tenant
dir — trading a flag for repeating the host.)

## How you run it — selection is the inventory, not a flag

```bash
# Someone joined team ACME. Apply ONLY acme — seconds, GLOBEX never touched.
./apply.sh acme
#   = ansible-playbook -i inventories/_common -i inventories/acme site.yml --tags idam

# Full-realm union / audit: reconcile several teams at once.
./apply.sh acme globex

# Dry run / pass-through args after --
./apply.sh acme -- --check
```

Ansible natively merges multiple `-i` sources, so `_common` supplies the host +
convention and each tenant dir supplies only its data. **Which tenants you pass =
which tenants exist for that run.** Other teams aren't in the inventory, so they
can't be iterated (fast) or deleted (safe).

## Why this is fast — and why the prune is safe

A monolithic users/groups list is slow because the role applies groups, hostgroups,
and memberships **one item per API round-trip, every run** — so a full reconcile
scales with the *whole estate* even when nothing changed. (`--limit` doesn't help:
it limits hosts, and there is one FreeIPA host.) Running just one team's inventory
means the role only iterates that team's handful of items → seconds, not minutes.

The role reconciles **one marker group** and deletes managed users not in the
desired set — so isolation must be real, or a one-team run would delete the other
teams. `_common/10_generate.yml` derives the marker from what's loaded:

| Run | Loaded | Marker group | Touches |
|-----|--------|--------------|---------|
| `./apply.sh acme` | acme | `idam-managed-acme` | ACME only — GLOBEX never read |
| `./apply.sh globex` | globex | `idam-managed-globex` | GLOBEX only |
| `./apply.sh acme globex` | both | `idam-managed-users` | union (correct: desired set *is* everyone) |

So a single-team run reconciles only that team's marker; the others are untouched.

## The three layers (smallest grain up)

| Layer | Pattern | Generated example | Defined in |
|-------|---------|-------------------|-----------|
| **group** (atomic permission) | `<tenant>-<app>-<tier>` | `acme-payments-admin` | `apps × tiers` |
| **role** (persona; bundles groups) | `role-<tenant>-<persona>` | `role-acme-lead` → `[acme-payments-admin, acme-ledger-admin]` | tenant `roles:` |
| **user** (assigned roles/groups) | — | `ann.lee` → `role-acme-lead` | tenant `members:` |

**Type marker:** roles are prefixed `role-` so the FIRST token tells you the kind —
`role-*` is a role, everything else is a plain permission group, no segment-parsing
to guess whether `admin` is an app tier or a role. A role bundles groups; a user
references roles (preferred) and/or a direct group; the role expands `user.roles`
→ groups at apply time, so a roles-only user is valid.

Plus `hostgroup` (`<tenant>-<app>`), host FQDN (`<env>-<tenant>-<app>-<NN>.<domain>`),
and service URL (`<app>.<tenant>.<env>.<domain>`). All patterns live in `00_naming.yml`.

## What this produces (verified with `ansible-playbook`)

```
./apply.sh acme        → marker idam-managed-acme
  groups(6): acme-payments-{admin,operator,viewer}, acme-ledger-{admin,operator,viewer}
  roles:     role-acme-developer, role-acme-lead
  users(3):  ann.lee→role-acme-lead, bo.ng→role-acme-developer, cj.park→group acme-payments-viewer

./apply.sh acme globex → marker idam-managed-users  (union)
  groups(8): + globex-search-{admin,viewer}      # globex narrowed its tiers
  roles:     + role-globex-analyst, role-globex-owner
  users(5):  + dee.fox→role-globex-owner, eli.mak→role-globex-analyst
```

## Scaling checklist

- **Add a team** → `cp -r inventories/acme inventories/<code>`, edit its `tenant.yml`,
  run `./apply.sh <code>`. Nothing else to touch — no count, no registry, no selector.
- **Add an app** → one line in the tenant's `apps:`; its groups/hostgroup appear.
- **Add a persona** → one entry in the tenant's `roles:`.
- **Add a person** → one `members:` line referencing a role, then `./apply.sh <code>`
  (seconds; other teams untouched).
- **Add an environment** → a sibling `_common.<env>` changing only `env` + host;
  reuse the same tenant dirs.
- **Tighten tiers for a team** → set `tiers:` in that tenant's spec.

## Alternative layout

`alt-single-inventory/` keeps **all** tenants in one inventory and isolates a run
with `-e idam_tenants=acme` instead of by which `-i` you pass — same generators,
same layers. See its README for the trade-offs; prefer the per-tenant layout above.

> Sanitised example — fictional tenants (ACME/GLOBEX), `example.com`, and RFC1918
> addresses. Replace with your own before use.
