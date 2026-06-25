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

## How the baseline stays sovereign

`site.yml` hands the role **`(your hand-written *_extra baseline) + (generated overlay)`**
for each list. With no matrix at all the role still works on pure baseline — the matrix
only ever *adds*. Uncomment `freeipa_idam_usergroups_extra` etc. in `10_people.yml` to
keep hand-managed objects alongside generated ones.

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
