# Access matrix → FreeIPA, compiled in Python

Define your whole estate as **one** declarative dict and let two Python filter plugins
compile it into the role's baseline `freeipa_idam_*` objects. You edit YAML; the
relationship-building and naming are handled in tested Python — no Jinja flatten.

```
access-matrix/
├── group_vars/all/
│   ├── 00_access_matrix.yml   # the ONE matrix: naming, tenants, apps, privileges, access_sets
│   └── 10_people.yml          # freeipa_people: identity + grants:[access_set]
├── inventories/<tenant>-<env>/ # scope.yml pins one tenant+env per run
├── site.yml                   # compile (2 filters) → layer onto baseline → reconcile
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

## Lifecycle — membership is ADDITIVE

Users are added to their `role-…` groups with `ipagroup action: member`, which is
**additive** — it never strips memberships it didn't add. Consequences for the operator:

- A person is a member of their `role-…` group(s) **and** every group they were already
  in (baseline-snapshot groups, hand-added groups) — both are kept. (The user-grant
  compiler also *unions* a person's existing `groups` with the matrix-granted ones, so
  the declarative user object stays complete.)
- **Removing a grant from `freeipa_people` does NOT revoke it on the next run** (additive
  never removes). To actually revoke access, remove the membership directly, or use the
  gated `--tags prune` (hard-delete) on a throwaway realm.

## Hosts — the one thing you wire yourself

Generated hostgroups (`hg-…`) are created **empty**. The HBAC and sudo rules are scoped
to them, so until a host is **enrolled** in FreeIPA **and** placed in the matching `hg-…`
hostgroup (manually, or via an automember rule), the rules match no machine. The matrix
builds the *policy skeleton*; populating hostgroups with hosts is out of band (enrolment
+ automember).

## The matrix (`00_access_matrix.yml`)

| section | what it is |
|---|---|
| `naming.prefixes` + `naming.templates` | every object name; **rearrange the `{placeholders}`** freely — the compiler resolves them |
| `tenants` | tenant → its environments (differ per tenant) |
| `apps` | app → description; may **override** a privilege's hbac/sudo for app-specific commands |
| `privileges` | global access tiers: `hbac_services` + `sudo_commands` (`[ALL]` → full sudo, `[]` → no sudo rule) |
| `access_sets` | reusable `(app, privilege)` bundles scoped to tenants/envs (`include: all`/list, optional `exclude`) |

People reference access_sets by name in `grants:`. That's the whole grant surface.

## Run it (one tenant × env per run)

```bash
ansible-playbook -i inventories/acme-dev   site.yml          # add --check --diff to preview
ansible-playbook -i inventories/globex-dev site.yml
```

Each inventory's `scope.yml` sets `freeipa_scope_tenancy`/`freeipa_scope_environment`,
so a run compiles only that slice. `site.yml` passes the run's scope into both filters.

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

- New app/privilege/tenant/env → edit `00_access_matrix.yml`; every derived object appears.
- New access bundle → add an `access_set`.
- New person → add to `freeipa_people` with `grants:[…]`.
- Rearrange naming → edit `naming.templates` (one place, applies everywhere).

## Validation

The compilers raise (failing the run, naming the culprit) on: an access_set with an
unknown app/privilege/tenant, a person granting an undefined access_set, or a naming
template with an unknown placeholder. Nothing drops silently.

> This is the single-dict + Python-compiler model (one matrix + two filter plugins),
> replacing the earlier three-dict + `20_generate.yml` Jinja-flatten approach.
