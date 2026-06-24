# Multi-tenant RBAC from a matrix

Define your whole estate as one compact **matrix** and let a short, readable flatten
derive every FreeIPA object. The `freeipa_server` role stays a **pure baseline** — it
only reconciles `freeipa_idam_*` lists; it has no role generator inside it, so a change
to the role's internals can never alter your roles. All the generation lives **here, in
your inventory**, where you can read and debug every line.

## The files you edit

```
group_vars/all/
├── 00_matrix.yml    # tenancy → { environments, apps: app → privileges }, name prefixes
├── 10_access.yml    # the CATALOG: what each (app, privilege) can do — login + sudo
├── 15_jobroles.yml  # job roles: named bundles of grants (assign people by role, not app)
└── 20_generate.yml  # the flatten: matrix + access + jobroles → freeipa_idam_* (read it!)
tenancy_rosters/<t>.yml  # the tenancy's people: name + jobroles/grants
```

### One source of truth, references everywhere else (no freehand drift)

`10_access.yml` (the **catalog**) is the ONE place app and privilege names are spelled.
Everything else **references** it rather than re-typing — and anything you do spell by
hand is validated against it, so a typo stops the run and names itself (it can never
silently drop access). Three reference forms resolve at load time:

| You write | resolves to | where |
|---|---|---|
| `docker: "*"` | every privilege the catalog gives docker | `00_matrix.yml` (app privileges) |
| `apps: [postgres]` | every privilege the catalog gives postgres | `15_jobroles.yml` (a jobrole) |
| `includes: [appdev]` | another jobrole's grants, by name | `15_jobroles.yml` (compose jobroles) |

Add a privilege to the catalog and every `"*"` / `apps:` reference picks it up — you
never hand-maintain the same privilege name in two places.

**00_matrix.yml** — the source of truth. Each tenancy declares its **own environments**
(they need not match), and apps differ per tenancy; the cross-product (tenancy's envs ×
its apps × privileges) is **derived, never copy-pasted**:
```yaml
freeipa_role_prefix: "role-"        # "" → no prefix (names become tenancy-env-app-privilege)
freeipa_matrix:
  acme:
    environments: [dev, qa, stg, prod]      # acme has 4 envs
    apps: { docker: [admin, readonly], postgres: [dba], web: [admin] }
  globex:
    environments: [dev, test, prod]         # globex has 3 (different)
    apps: { docker: [admin], linux: [sysadmin] }
  initech:
    environments: [dev]                     # initech has just one
    apps: { web: [admin], linux: [sysadmin], postgres: [dba, readonly] }
```

**10_access.yml** — defined once, shared by every tenancy (no repeating sshd/sudo):
```yaml
freeipa_access:
  docker: { admin: {login: [sshd], sudo: ["/usr/bin/docker"]}, readonly: {login: [sshd]} }
  linux:  { sysadmin: {login: [sshd, cockpit], sudo_all: true} }
```

## Run it (one tenancy × one of its environments per run)

There's one inventory per valid `(tenancy, environment)` — `acme-dev`, `acme-qa`,
`acme-stg`, `acme-prod`, `globex-dev`, `globex-test`, `globex-prod`, `initech-dev`:

```bash
ansible-playbook -i inventories/acme-qa     site.yml --tags idam
ansible-playbook -i inventories/globex-test site.yml --tags idam
#  …  add --check --diff to preview
```

Each inventory's **scope** (`inventories/<tenancy>-<env>/group_vars/all/scope.yml`)
filters the shared matrix to one tenancy + one environment — so a run reconciles only
that slice (a handful of groups), never the whole estate. If you point a scope at an
environment the tenancy doesn't have (e.g. `initech/prod`), `site.yml` refuses and tells
you which environments that tenancy actually has.

## What gets derived (all baseline `freeipa_idam_*`, all WYSIWYG names)

| from the matrix | →  |
|---|---|
| every cell | `freeipa_idam_usergroups`: `{prefix}{tenancy}-{env}-{app}-{privilege}` |
| each (tenancy,env,app) | `freeipa_idam_hostgroups`: `hosts-{tenancy}-{env}-{app}` (deduped — shared across privileges) |
| cells with `login` | `freeipa_idam_hbac_rules` (group → hostgroup, services) |
| cells with `sudo`/`sudo_all` | `freeipa_idam_sudo_rules` |
| the access catalog | `freeipa_idam_hbacsvcs` (custom only) + `freeipa_idam_sudo_commands` — the **foundation**, derived so it's never out of sync |

## People — assign by job role, not per app

`tenancy_rosters/<tenancy>.yml` lists the tenancy's people once (shared across its
environments). You assign access by **job role** — "alice is a sysadmin" — not by
re-listing apps per person:
```yaml
freeipa_people:
  - { name: alice.smith, first: Alice, last: Smith, email: [...], jobroles: [sysadmin] }
  - { name: carol.jones, first: Carol, last: Jones, email: [...],
      jobroles: [dba], grants: [{app: web, privilege: admin}] }   # job role + ad-hoc extra
```
- `jobroles: [..]` — one or more named bundles from `15_jobroles.yml`.
- `grants: [{app, privilege}]` — ad-hoc one-offs, still supported, **UNIONED** with jobroles.
- Both resolve to the scoped environment's concrete role groups, so the same roster serves
  dev and prod. A jobrole **auto-intersects**: the same `sysadmin` lands linux+docker+postgres
  in acme but just linux+docker in globex (no postgres) — one definition, tenancy-correct.
- A typo'd jobrole, or a grant for an app/privilege not in the catalog, **stops the run**
  (caught by `freeipa_matrix_problems`).
The per-tenancy **marker group** (`idam-<tenancy>-managed`) scopes the role's prune, so
running `acme-dev` can never archive `globex`'s users on the shared realm.

**A person can belong to multiple tenancies.** A FreeIPA user is one global identity —
list them in each tenancy's roster (e.g. `sam.cross` appears in both `acme.yml` and
`globex.yml`); each tenancy run creates the account idempotently and adds *its* groups.
Keep their `first/last/email` identical across rosters (the last run to touch the account
sets those attributes).

## Cleaning up a mock run (`--tags prune`)

For a throwaway realm you can create everything, look at it, then **hard-delete** it:

```bash
ansible-playbook -i inventories/acme-dev site.yml --tags idam      # create
ansible-playbook -i inventories/acme-dev site.yml --tags prune     # remove it all
```

`prune` is **double-gated** so it can't fire by accident: it needs `--tags prune`
**and** `freeipa_idam_prune: true` (commented out in each `scope.yml` — set it only on a
mock realm). It hard-deletes (not archive) exactly what that scope declared, never
protected users/groups. Note it removes **global user identities**, so pruning one
tenancy deletes any person it shares with another — which is fine for a mock teardown.

## Guardrails

- **Configurable prefixes** (`freeipa_role_prefix`, `freeipa_hostgroup_prefix`, …) — set
  `freeipa_role_prefix: ""` for no prefix; whatever you set is exactly what you get.
- **Consistency check**: a privilege in the matrix with no entry in `freeipa_access` is
  caught by `freeipa_matrix_problems`; `site.yml` refuses to run and names it.
- **Self-contained**: the stock-HBAC-services list lives in `10_access.yml`, so the
  example doesn't reach into the role's internals.

## Add a tenancy / app / privilege

Edit `00_matrix.yml` (and `10_access.yml` if it's a new app/privilege). That's it — the
groups, hostgroups, HBAC and sudo for the new cells appear on the next run. Add an
inventory `inventories/<new>-<env>/` (copy any, change `scope.yml`) and a
`tenancy_rosters/<new>.yml` for its people.
