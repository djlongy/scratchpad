# Multi-tenant IDAM for `freeipa_server` — a scalable reference template

One FreeIPA realm (or one *per* environment), many teams. The **logic is global**
(the naming convention + generators sit next to the playbook, auto-loaded every
run), and **each team is its own self-contained inventory directory** — its own
FreeIPA host plus only that team's data. You change a team by running *that team's
inventory* — nothing else is loaded, queried, or pruned.

Groups, roles, and users are **generated from a single naming convention**, so every
tenant is consistent by construction and you never hand-type a group or role name.

```
multitenant-idam/
├── site.yml                         # the play
├── group_vars/all/                  # GLOBAL, env-AGNOSTIC logic — adjacent to the playbook
│   ├── 00_naming.yml                #   access tiers + the documented name patterns
│   └── 10_generate.yml              #   tenant_*_spec → freeipa_idam_* + per-tenant marker
├── inventories/
│   ├── acme/                        # team ACME — self-contained
│   │   ├── hosts.yml                #   its FreeIPA host (its own IP per env/tenancy)
│   │   └── group_vars/all/
│   │       ├── 00_env.yml           #   env + domain (ENVIRONMENT-specific → lives here)
│   │       └── tenant.yml           #   its data (tenant_acme_spec)
│   └── globex/                      # team GLOBEX — self-contained (same shape)
│       ├── hosts.yml
│       └── group_vars/all/{00_env.yml, tenant.yml}
└── alt-single-inventory/            # the same generators, one-inventory layout (comparison)
```

**Why host- and env-per-inventory:** Ansible auto-loads `group_vars`/`host_vars` next
to the *playbook* on every run, so the convention + generators are global and never
passed on the command line. But that location also **outranks** inventory
`group_vars/all` in precedence — so anything env-specific put there (like `env` or
`domain`) would override what a test inventory sets, forcing prod onto test. So
`env`/`domain` live **per inventory** (`00_env.yml`), and a *host* can only come from
an inventory anyway — each tenant inventory carries its own `hosts.yml`. A dev/test
tenancy is just another inventory dir with its own host IP + `env`/`domain`, reusing
the identical naming + generators. Only env-agnostic things stay global.

## How you run it — plain `ansible-playbook`, no wrapper

```bash
# Identity only (groups, roles, users, hostgroups) — the routine change:
ansible-playbook -i inventories/acme site.yml --tags idam

# The full role (also install/configure the server) — e.g. first build:
ansible-playbook -i inventories/acme site.yml

# Preview, change nothing (dry run + diffs):
ansible-playbook -i inventories/acme site.yml --tags idam --check --diff

# See what the tags would run:
ansible-playbook -i inventories/acme site.yml --list-tags

# Union / audit across several tenants on the SAME realm (one reconcile):
ansible-playbook -i inventories/acme -i inventories/globex site.yml --tags idam
```

**Which inventory you point at = which tenants exist for that run.** Other teams
aren't in the inventory, so they can't be iterated (fast) or deleted (safe). No
flag, no wrapper.

## Why this is fast — and why the prune is safe

A monolithic users/groups list is slow because the role applies groups, hostgroups,
and memberships **one item per API round-trip, every run** — so a full reconcile
scales with the *whole estate* even when nothing changed. (`--limit` doesn't help:
it limits hosts, and there is one FreeIPA host.) Running just one team's inventory
means the role only iterates that team's handful of items → seconds, not minutes.

The role reconciles **one marker group** and deletes managed users not in the
desired set — so isolation must be real, or a one-team run would delete the other
teams. `group_vars/all/10_generate.yml` derives the marker from what's loaded:

| Run | Loaded | Marker group | Touches |
|-----|--------|--------------|---------|
| `-i inventories/acme` | acme | `idam-managed-acme` | ACME only — GLOBEX never read |
| `-i inventories/globex` | globex | `idam-managed-globex` | GLOBEX only |
| `-i inventories/acme -i inventories/globex` | both | `idam-managed-users` | union (correct: desired set *is* everyone) |

So a single-team run reconciles only that team's marker; the others are untouched.

## The layers (smallest grain up)

| Layer | Pattern | Generated example | Defined in |
|-------|---------|-------------------|-----------|
| **group** (atomic permission) | `<tenant>-<app>-<tier>` | `acme-payments-admin` | `apps × tiers` |
| **role** (persona; bundles groups) | `role-<tenant>-<persona>` | `role-acme-lead` → `[acme-payments-admin, acme-ledger-admin]` | tenant `roles:` |
| **user** (assigned roles/groups) | — | `ann.lee` → `role-acme-lead` | tenant `members:` |
| **HBAC rule** (access) | `hbac-<tenant>-<app>` | `hbac-acme-payments` → those groups SSH to `acme-payments` hostgroup | `apps` (generated) |

**Two kinds of tenant.** Identity tenants (above) own users/groups/roles + HBAC
*rules*. The **realm/auth tenant** (the FreeIPA realm itself) owns the
infrastructure baseline (`freeipa_server_*` install/config) and the **global** bits:
HBAC *services* (`sshd`, custom svcs — one per realm), automember, DNS. Rules are
per-tenant; services are shared. HBAC rules are additive (not pruned) — remove
access with `state: absent`.

**Type marker:** roles are prefixed `role-` so the FIRST token tells you the kind —
`role-*` is a role, everything else is a plain permission group, no segment-parsing
to guess whether `admin` is an app tier or a role. A role bundles groups; a user
references roles (preferred) and/or a direct group; the role expands `user.roles`
→ groups at apply time, so a roles-only user is valid.

Plus `hostgroup` (`<tenant>-<app>`), host FQDN (`<env>-<tenant>-<app>-<NN>.<domain>`),
and service URL (`<app>.<tenant>.<env>.<domain>`). All patterns live in `00_naming.yml`.

## What this produces (verified with `ansible-playbook`)

```
-i inventories/acme                       → marker idam-managed-acme
  groups(6): acme-payments-{admin,operator,viewer}, acme-ledger-{admin,operator,viewer}
  roles:     role-acme-developer, role-acme-lead
  users(3):  ann.lee→role-acme-lead, bo.ng→role-acme-developer, cj.park→group acme-payments-viewer

-i inventories/acme -i inventories/globex → marker idam-managed-users  (union)
  groups(8): + globex-search-{admin,viewer}      # globex narrowed its tiers
  roles:     + role-globex-analyst, role-globex-owner
  users(5):  + dee.fox→role-globex-owner, eli.mak→role-globex-analyst
```

## Scaling checklist

- **Add a team** → `cp -r inventories/acme inventories/<code>`, edit its `tenant.yml`
  (and `hosts.yml` if it's a different realm), run
  `ansible-playbook -i inventories/<code> site.yml --tags idam`. Nothing else to
  touch — no count, no registry, no selector.
- **Add an app** → one line in the tenant's `apps:`; its groups/hostgroup appear.
- **Add a persona** → one entry in the tenant's `roles:`.
- **Add a person** → one `members:` line referencing a role, then
  `ansible-playbook -i inventories/<code> site.yml --tags idam` (seconds; others untouched).
- **Add an environment (dev/test)** → a tenant inventory whose `hosts.yml` points at
  that environment's FreeIPA IP and whose group_vars set `env` — same convention,
  same generators.
- **Tighten tiers for a team** → set `tiers:` in that tenant's spec.

## Alternative layout

`alt-single-inventory/` keeps **all** tenants in one inventory and isolates a run
with `-e idam_tenants=acme` instead of by which `-i` you pass — same generators,
same layers. See its README for the trade-offs; prefer the per-tenant layout above.

> Sanitised example — fictional tenants (ACME/GLOBEX), `example.com`, and RFC1918
> addresses. Replace with your own before use.
