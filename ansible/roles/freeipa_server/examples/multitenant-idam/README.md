# Multi-tenant IDAM for `freeipa_server` — a scalable reference template

One FreeIPA realm (or one *per* environment), many teams. The **logic is global**
(the naming convention + generators sit next to the playbook, auto-loaded every
run), and **each team is its own self-contained inventory directory** — its own
FreeIPA host plus only that team's data. You change a team by running *that team's
inventory* — nothing else is loaded, queried, or deleted.

Groups, roles, and users are **generated from a single naming convention**, so every
tenant is consistent by construction and you never hand-type a group or role name.

```
multitenant-idam/
├── site.yml                         # the play
├── group_vars/all/                  # GLOBAL, env-AGNOSTIC logic — adjacent to the playbook
│   ├── 00_naming.yml                #   access levels + the documented name patterns
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

## Why this is fast — and why the delete is safe

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
| **group** (atomic permission) | `<tenant>-<env>-<service>-<level>` | `acme-prod-payments-access`, `…-priv-access` | `services × access_levels` |
| **role** (persona; bundles groups) | `role-<tenant>-<persona>` | `role-acme-lead` → `[acme-prod-payments-priv-access, acme-prod-ledger-priv-access]` | tenant `roles:` |
| **user** (rich: name/givenname/sn/title/email) | explicit `name`; email=`<name>@<domain>` | `alice.smith` / `alice.smith@example.com` → `role-acme-lead` | tenant `members:` |
| **HBAC rule** (access) | `hbac-<tenant>-<env>-<service>` | `hbac-acme-prod-payments` → that service's access groups SSH to `acme-prod-payments` | `services` (generated) |

The **access level** suffix is the point — `-access` = normal, `-priv-access` =
privileged — so the group name says what it's *for* at a glance. This is the inline
form teams usually type (`{{ tenancy }}-{{ env }}-{{ service }}-access`), except the
pattern lives in ONE place and a typo fails fast (see referencing below).

**Two kinds of tenant.** Identity tenants (above) own users/groups/roles + HBAC
*rules*. The **realm/auth tenant** (the FreeIPA realm itself) owns the
infrastructure baseline (`freeipa_server_*` install/config) and the **global** bits:
HBAC *services* (`sshd`, custom svcs — one per realm), automember, DNS. Rules are
per-tenant; services are shared. HBAC rules are additive (not deleted) — remove
access with `state: absent`.

### Referencing generated objects from elsewhere

Names are generated, never hand-written — so **don't copy-paste them out of
FreeIPA**. Three ways to refer to them:

```yaml
# 1. one group/role by INTENT (env-portable key; typo'd key fails fast):
firewalld_allow_groups: "{{ [freeipa_group_ref['acme.payments.priv-access']] }}"  # -> acme-prod-payments-priv-access
app_admin_role:         "{{ freeipa_role_ref['acme.lead'] }}"                      # -> role-acme-lead

# 2. loop the RICH user list — each item has name/givenname/sn/title/email + roles/groups:
- debug: { msg: "{{ item.name }} <{{ item.email }}>" }
  loop: "{{ freeipa_idam_users }}"

# 3. flat name lists for bulk loops, or a whole team's users:
loop: "{{ freeipa_all_group_names }}"            # every group in the loaded scope
notify: "{{ freeipa_users_by_tenant['acme'] }}"  # -> ['alice.smith','bob.ng','carol.jones']
```

A single **username** is just the literal (`alice.smith`) — it's explicit, not derived.
If the convention ever changes, the `*_ref` *values* change and every reference
follows; no estate-wide find/replace. "All" in the flat lists = all tenants LOADED
this run (one, or several on a union run) — realm-wide is a FreeIPA query by design.

**Type marker:** roles are prefixed `role-` so the FIRST token tells you the kind.
A role bundles groups; a user references roles and/or a direct group; the role
expands `user.roles` → groups at apply time, so a roles-only user is valid.

## What this produces (verified with `ansible-playbook`)

```
-i inventories/acme                       → marker idam-managed-acme
  groups(4): acme-prod-{payments,ledger}-{access,priv-access}
  roles:     role-acme-lead → [acme-prod-payments-priv-access, acme-prod-ledger-priv-access]
  hbac:      hbac-acme-prod-payments, hbac-acme-prod-ledger
  users(3):  alice.smith <alice.smith@example.com> → role-acme-lead
             bob.ng, carol.jones → direct -access groups

-i inventories/acme -i inventories/globex → marker idam-managed-users  (union)
  + globex-prod-search-access            # globex overrides access_levels to [access] only
  + role-globex-analyst, users dee.fox / eli.mak
```

## Scaling checklist

- **Add a team** → `cp -r inventories/acme inventories/<code>`, edit its `tenant.yml`
  (and `hosts.yml`/`00_env.yml` if it's a different realm/env), run
  `ansible-playbook -i inventories/<code> site.yml --tags idam`. No count/registry.
- **Add a service** → one line in the tenant's `services:`; its groups/hostgroup/HBAC appear.
- **Add a persona** → one entry in the tenant's `roles:`.
- **Add a person** → one `members:` line (`{name, givenname, sn, title, …}`; email is built from name).
- **Add an environment (dev/test)** → a tenant inventory whose `hosts.yml` points at
  that environment's FreeIPA IP and whose `00_env.yml` sets `env`/`domain`.
- **Restrict access levels for a team** → set `access_levels:` in that tenant's spec.

## Alternative layout

`alt-single-inventory/` keeps **all** tenants in one inventory and isolates a run
with `-e idam_tenants=acme` instead of by which `-i` you pass — same generators,
same layers. See its README for the trade-offs; prefer the per-tenant layout above.

> Sanitised example — fictional tenants (ACME/GLOBEX), `example.com`, and RFC1918
> addresses. Replace with your own before use.
