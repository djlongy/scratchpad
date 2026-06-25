# Access matrix → FreeIPA, compiled in Python

Define your whole estate as **one** declarative dict and let two Python filter plugins
compile it into the role's baseline `freeipa_idam_*` objects. You edit YAML; the
relationship-building and naming are handled in tested Python — no Jinja flatten.

```
access-matrix/
├── group_vars/all/
│   ├── 00_access_matrix.yml   # the ONE matrix: object_naming, tenants, applications,
│   │                          #   privilege_levels, host_automember, access_grants, role_sets
│   └── 10_people.yml          # freeipa_people: identity + grants:[role_set | access_grant]
├── inventories/<tenant>-<env>/ # scope.yml pins one tenant+env per run
├── site.yml                   # validate scope → compile (2 filters) → merge onto baseline → reconcile
└── ansible.cfg                # finds the role + loads its filter_plugins
```

The compilers live in the role: `../../filter_plugins/freeipa_idam.py`
(unit tests: lab `ansible/tests/unit/roles/test_freeipa_idam_filters.py`).

## The object model

For each generated cell `tenant/env/app/privilege` the compiler emits a clean two-tier
grant model (a FreeIPA user group cannot itself hold HBAC/sudo/hosts, so policy is
decoupled from membership):

```
user ──member──▶ role-…  (grant group)
                   └─nested into─▶ ug-…  (policy group)  ◀── HBAC rule ─┐
                                                          ◀── sudo rule ─┤
                                                   hg-…  (hostgroup) ◀───┘
```

- **users join `role-…`** only. Add/remove a person ⇒ one group membership.
- **HBAC + sudo target `ug-…`**, scoped to `hg-…`. Want a break-glass group on a
  policy? Add it to that one `ug-…`; no rule is touched.

## Prerequisites — what must already exist (almost nothing)

The matrix **creates** the groups, the policy groups, the hostgroups, the HBAC rules,
the sudo rules and the users. The only things it assumes already exist:

1. **The FreeIPA server + admin credentials** (`freeipa_idam_admin_principal` /
   `freeipa_idam_admin_password`).
2. **Stock HBAC services** (`sshd`, `sudo`, …) — these ship with every FreeIPA. The
   matrix auto-creates any *non-stock* service you reference (e.g. `cockpit`); it does
   not re-create the stock ones. So a `privileges.*.hbac_services` entry is always
   satisfied: stock already exists, custom is generated.
3. **Enrolled hosts** — but *only* if you want the rules to actually match a machine
   (see "Hosts" below). Nothing in the matrix needs a host to exist to be created.

**Users do NOT need to pre-exist.** List a person in `freeipa_people` with their identity
(`name` + `first`/`last`/`email`) and `grants:`; the role creates the account
(`ipauser state: present`) and then adds their memberships. An already-existing or
protected account (e.g. `admin`) is never recreated — only its memberships are added.

So the minimum to go from nothing → working access is: a FreeIPA server, admin creds,
and the matrix + people. Everything else is generated.

## What it creates, and what it does NOT

**Creates**, per cell: `role-…` grant group, `ug-…` policy group (nesting the role
group), `hg-…` hostgroup (empty), the `hbac-…` rule, the `sudo-…` rule (skipped when the
privilege has no sudo), and any custom hbacsvc / sudo-command objects referenced.

**Does NOT create** FreeIPA permissions / privileges / delegation **roles** (`ipapermission`
/ `ipaprivilege` / `iparole`). The "role group" here is a **user group**, not an IPA
`role`. Access is enforced by **HBAC** (who may log in, with which services) and **sudo**
rules — there is no RBAC-permission layer in this model.

## Lifecycle — membership is ADDITIVE by default (declarative is a toggle)

By default users are added to their `role-…` groups with `ipagroup action: member`, which
is **additive** — it never strips memberships it didn't add:

- A person is a member of their `role-…` group(s) **and** every group they were already
  in (baseline-snapshot groups, hand-added groups) — both are kept. (The user-grant
  compiler also *unions* a person's existing `groups` with the matrix-granted ones.)
- **Removing a grant does NOT revoke it on the next run.**

**To make it declarative, set `freeipa_idam_membership_declarative: true`** (role var,
default off). Then — for the **declared, non-protected** groups only — membership is
reconciled to *exactly* what's in code: a user/nested-group no longer granted is
**removed**, and a managed group with zero grants is **emptied**. So deleting a grant
actually revokes it next run. Guard rails:

- **Protected/built-in groups** (`admin`, `ipausers`, break-glass — `freeipa_idam_protected_groups`)
  always stay **additive**; they are never stripped.
- **Undeclared groups** are never touched.
- It's the surgical sibling of `delete`: this reconciles *membership* of groups you declare;
  `delete` deletes the *objects* themselves.

## Hosts — the one thing you wire yourself

Generated hostgroups (`hg-…`) are created **empty**. The HBAC and sudo rules are scoped
to them, so until a host is **enrolled** in FreeIPA **and** placed in the matching `hg-…`
hostgroup (manually, or via an automember rule), the rules match no machine. The matrix
builds the *policy skeleton*; populating hostgroups with hosts is out of band (enrolment
+ automember).

### Automember — populating hostgroups (and base user groups) by regex

**Let the matrix generate the host rules for you (optional).** Add an `automember` block
to `00_access_matrix.yml` and the compiler emits one `hostgroup` automember rule per
generated `hg-…`, with an anchored fqdn regex derived from the same tokens — so host
membership wires itself and stays consistent with the names:

```yaml
freeipa_idam_access_matrix:
  host_automember:
    fqdn_pattern: "{tenant}-{application}-{instance}.{environment}.example.com"
    # domain: example.com     # only if you use the {domain} placeholder
    # instance: "[0-9]+"      # regex for {instance} (default)
```

Placeholders `{tenant}` `{environment}` `{application}` `{domain}` are substituted (regex-escaped);
`{instance}` is a raw regex fragment; every literal (dots, dashes) is escaped and the whole
thing anchored `^…$`. `hg-acme-dev-grafana-admins` → `^acme\-grafana\-[0-9]+\.dev\.example\.com$`.
`site.yml` merges these into `freeipa_server_automember_rules` (onto any you wrote by hand).
Omit the `host_automember` block and nothing is generated — you keep full manual control below.
Put `{environment}` in the pattern to split `hg-` by env; a flat pattern (no `{environment}`)
only disambiguates when each environment is its own realm.

The role applies `freeipa_server_automember_rules` (one ipaautomember rule each). To write
them by hand instead (or in addition), the shape is:

```yaml
freeipa_server_automember_rules:
  - name: hg-acme-dev-artifactory   # the target group/hostgroup (must already exist)
    automember_type: hostgroup      # hostgroup | group
    inclusive:
      - { key: "fqdn", expression: "<regex>" }   # OR'd if multiple
    exclusive: []                   # optional; an exclusive match wins over inclusive
```

**Regex hygiene (FreeIPA-specific):**
- **Always anchor `^…$`.** IPA matches as a *substring search* — unanchored `acme` also
  hits `acme2`, `notacme`, …
- **Escape dots:** `\.` in the regex → `\\.` inside a YAML double-quoted string.
- Use `[0-9]+`, not `\d` (safer across IPA's engine). Inclusive entries are **OR'd**.
- Automember fires at **host/user create time**; pre-existing entries are only (re)placed
  by a rebuild — the role runs one automatically when a rule changes (or set
  `freeipa_server_automember_rebuild: true` for a one-off).

#### Hosts → `hg-…` (fqdn)

> **Caveat:** a host's environment must be **in the FQDN** for automember to split
> `hg-<tenant>-<env>-<app>` by env. A flat `<tenant>-<service>-<N>.<domain>` (no env
> label) can only be matched by tenant+app — so use it when **each environment is its own
> realm** (env implicit), or move the env into the FQDN (recommended, below).

Flat domain `auth.team.dev`, host `acme-artifactory-1.auth.team.dev` (env from realm):
```yaml
  - name: hg-acme-dev-artifactory
    automember_type: hostgroup
    inclusive:
      - { key: "fqdn", expression: "^acme-artifactory-[0-9]+\\.auth\\.team\\.dev$" }
# generic:  ^{tenant}-{app}-[0-9]+\.auth\.team\.dev$
```

Env-as-subdomain `acme-artifactory-1.dev.mydomain.internal` (full tenant/env/app split):
```yaml
  - name: hg-acme-dev-artifactory
    automember_type: hostgroup
    inclusive:
      - { key: "fqdn", expression: "^acme-artifactory-[0-9]+\\.dev\\.mydomain\\.internal$" }
# generic:  ^{tenant}-{app}-[0-9]+\.{env}\.{domain}$
```

#### Users → base groups (attribute)

A person's `role-…` membership is set **explicitly** by their matrix grants — **do not**
automember those (no user attribute encodes tenant/env/app/privilege). Reserve user
automember for **coarse base groups** matched on a user attribute (`uid`, `mail`, `title`,
`employeetype`, …):
```yaml
  - name: acme-users                      # a tenant base group
    automember_type: group
    inclusive:
      - { key: "mail", expression: "@acme\\.example\\.com$" }   # by email domain
      # or by uid prefix:  { key: "uid", expression: "^acme-" }
  - name: contractors
    automember_type: group
    inclusive:
      - { key: "title", expression: "^Contractor$" }
```
(`mail` is multivalued — the rule matches if *any* value matches.)

## The matrix (`00_access_matrix.yml`)

| section | what it is |
|---|---|
| `object_naming.prefixes` + `.templates` | names of the **five** generated object types. Keys are a **fixed set** (`role_group`, `user_group`, `host_group`, `hbac_rule`, `sudo_rule`) — you can't add types; edit the prefix **value** and/or rearrange the template `{placeholders}` |
| `descriptions` *(optional)* | enriched object **descriptions** (defaults already pull in the app + privilege-level descriptions, e.g. *"Grafana — Read-only … [acme/dev]"*). Override per object type; placeholders add `{application_description}` + `{privilege_level_description}` |
| `tenants` | tenant → its environments (the **single source of truth** for what exists) |
| `applications` | app → description; may **override** a level's hbac/sudo via `privilege_overrides` |
| `privilege_levels` | the access tiers (admins/readers/…): `hbac_services` + `sudo_commands` (`[ALL]` → full sudo, `[]` → no sudo rule) |
| `access_grants` | a named, **scoped grant** = `application` + `privilege_level` + which tenants/envs (`include: all`/list, optional `exclude`) |
| `role_sets` | a **role** = a named super-set of `access_grants` (e.g. `platform_admin: [grafana_admins, postgres_admins]`) |
| `host_automember` *(optional)* | auto-generate one hostgroup automember rule per `hg-…` from a `fqdn_pattern` — host membership wires itself (see above) |

A person's `grants:` holds **role_set or access_grant** names — that's the whole grant surface.

## Run it (one tenant × env per run)

```bash
ansible-playbook -i inventories/acme-dev   site.yml          # add --check --diff to preview
ansible-playbook -i inventories/globex-dev site.yml
```

Each inventory's `scope.yml` sets `freeipa_scope_tenancy`/`freeipa_scope_environment`,
so a run compiles only that slice. This is a **selection**, not a second declaration — the
matrix `tenants:` is the single source of truth. `site.yml` **asserts the scope exists in
the matrix** first, so a typo or drift (`acme/staging` when acme has no `staging`) stops the
run and lists the valid tenants/envs, instead of silently doing nothing.

## Baseline + matrix overlay (brownfield)

The **baseline is the role's own native keys** — `freeipa_idam_usergroups`,
`freeipa_idam_hbac_rules`, `freeipa_idam_users`, … — exactly as an exported snapshot
drops them into group_vars. `site.yml` MERGES the matrix-generated objects onto that
baseline with the `freeipa_idam_merge` filter:

```yaml
freeipa_idam_usergroups: "{{ freeipa_idam_usergroups | default([]) | freeipa_idam_merge(_freeipa_generated.usergroups) }}"
freeipa_idam_users:      "{{ freeipa_idam_users      | default([]) | freeipa_idam_merge(_freeipa_matrix_users, union_fields=['groups']) }}"
```

- **Unique by name** — the matrix only adds objects the baseline doesn't already have.
- **Baseline wins** on a name collision (it's the base); for **users** the `groups`
  lists are **unioned**, so a person in both the snapshot and the matrix keeps both.
- **Greenfield** = the native keys are simply empty, so you get just the generated set.

So you can `export` an existing realm, drop the snapshot into group_vars, and layer
matrix-managed access on top — the role is handed one combined, deduped set.

## Add capacity

- New application / privilege_level / tenant / env → edit `00_access_matrix.yml`; every
  derived object appears.
- New scoped grant → add an `access_grant`. New role → add a `role_set`.
- New person → add to `freeipa_people` with `grants:[…]`.
- Rearrange naming → edit `object_naming.templates` (one place, applies everywhere).

## Validation

The compilers raise (failing the run, naming the culprit) on: an `access_grant` with an
unknown `application`/`privilege_level`/`tenant`, a `role_set` referencing an undefined
`access_grant`, a person granting an undefined role_set/access_grant, or a naming template
with an unknown placeholder. `site.yml` additionally asserts the inventory **scope** exists
in the matrix. Nothing drops silently.

> This is the single-dict + Python-compiler model (one matrix + two filter plugins),
> replacing the earlier three-dict + `20_generate.yml` Jinja-flatten approach.
