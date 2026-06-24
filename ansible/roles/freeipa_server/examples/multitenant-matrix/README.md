# Multi-tenant RBAC from a matrix

Define your whole estate as one compact **matrix** and let a short, readable flatten
derive every FreeIPA object. The `freeipa_server` role stays a **pure baseline** — it
only reconciles `freeipa_idam_*` lists; it has no role generator inside it, so a change
to the role's internals can never alter your roles. All the generation lives **here, in
your inventory**, where you can read and debug every line.

## The three files you edit

```
group_vars/all/
├── 00_matrix.yml    # tenancy → { environments, apps: app → [privileges] }, name prefixes
├── 10_access.yml    # what each (app, privilege) can do: login services + sudo
└── 20_generate.yml  # the flatten: matrix + access → baseline freeipa_idam_* (read it!)
```

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

## People

`tenancy_rosters/<tenancy>.yml` lists the tenancy's people once (shared across its
environments). Each person's **abstract grants** `{app, privilege}` resolve to the
scoped environment's concrete role group, so the same roster serves dev and prod:
```yaml
freeipa_people:
  - { name: alice.smith, first: Alice, last: Smith, email: ["alice.smith@acme.example.com"],
      grants: [{app: docker, privilege: admin}, {app: postgres, privilege: dba}] }
```
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
