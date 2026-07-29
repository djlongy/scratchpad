# freeipa_server

## TL;DR

Wraps the upstream `freeipa.ansible_freeipa` `ipaserver`/`ipareplica` roles for
install, then layers cold-start resilience, a backup timer, declarative IAM
reconciliation (users/groups/HBAC/sudo/DNS/automember), and opt-in hardening.

**Most common: reconcile identity.** Edit a tenant file under
`freeipa_iam_tenants_dir` (see [Tenant model](#tenant-model)) — or the
`freeipa_iam_*` lists directly, if no tenants directory is set — then re-run
(idempotent, primary-only):

```bash
ansible-playbook -i inventories/<env>/hosts.yml playbooks/20_iam_freeipa.yml --tags iam
```

Install is a separate one-time run (no tags does install + everything):

```bash
ansible-playbook -i inventories/<env>/hosts.yml playbooks/20_iam_freeipa.yml
```

## Requirements

Install collections before running (repo `requirements.yml`, or ad-hoc):

    ansible-galaxy collection install -r requirements.yml

| Collection | When | Used for |
|---|---|---|
| `freeipa.ansible_freeipa` | always | wraps `ipaserver`/`ipareplica` and every declarative IAM/DNS/trust module |
| `community.general` | always | `ldap_search` for the stale-account report; `ldap_attrs` for opt-in hardening |
| `community.hashi_vault` | When admin/dm password empty | reads the admin/DM password fallback from Vault |

## Key variables

Full list: `defaults/main.yml`. Contract: `meta/argument_specs.yml`.

**Required** = value must be correct for a successful run (defaults often work).
**Optional** = safe to leave default / empty; phase stays off or uses built-ins.
**When X** = required only if that feature is on.

| Req | Variable | Default | Purpose |
|---|---|---|---|
| **Required** | `domain` | (inventory `group_vars/all`) | Base domain; `freeipa_server_domain`/`_realm` derive from it |
| When install | `freeipa_server_admin_password` | `""` | IPA admin password (declared var wins over Vault fallback) |
| When install | `freeipa_server_dm_password` | `""` | Directory Manager password (declared var wins over Vault fallback) |
| When admin/dm empty | `freeipa_server_vault_secret` | unset | HashiCorp Vault KV path — fallback only |
| **Required** | `freeipa_server_forwarders` | `[]` | Upstream DNS forwarders (install + day-2 `ipadnsconfig`; e.g. `["{{ network_gateway }}"]`). Integrated DNS is ON by default, and preflight rejects a run that declares neither this nor `freeipa_server_no_forwarders: true` — so this is required unless you set one of those. |
| When DNS | `freeipa_server_dns_trusted_networks` | `[]` | CIDRs allowed to recurse (BIND ACL; every DNS server) |
| Optional | `freeipa_server_primary_group` | `freeipa_primary` | Inventory group whose first host is the primary (falls back to `freeipa`, then this host) |
| Optional | `freeipa_server_ca_mode` | `self-signed` | Serving CA: `self-signed` \| `external-ca` \| `ca-less` |
| Optional | `freeipa_server_setup_dns` | `true` | Install FreeIPA's integrated DNS |
| Optional | `freeipa_server_resilience_enabled` | `true` | Cold-start recovery timer + SSSD self-heal watchdog |
| Optional | `freeipa_iam_tenants_dir` | `""` | Directory of per-tenant identity files — the primary way to declare identity. Empty = declare the `freeipa_iam_*` lists directly in `group_vars`. See [Tenant model](#tenant-model) |
| Optional | `freeipa_server_authoritative` | `false` | Soft-prune switch: reconcile removes undeclared members/objects (archives users) |
| Optional | `freeipa_iam_users_preserved` | `[]` | Explicit offboarding — entries **moved** out of `freeipa_iam_users`. Preserve-only, ungated by `freeipa_server_authoritative` |
| Optional | `freeipa_iam_delete` | `false` | Hard-delete gate (irrecoverable `ipa *-del`); which operation runs is chosen by `--tags` |
| Optional | `freeipa_server_rbac_roles` | `[]` | Thin RBAC overlay: assign users an abstract role instead of many groups. Declarable inside a tenant file — see [Tenant model](#tenant-model); field reference in `docs/rbac_roles.md` |
| Optional | `freeipa_server_trusted_external_cas` | `[]` | Additive trust of third-party CA certs (does not change the serving CA) |

## Minimum configuration

```yaml
# group_vars/freeipa_server_hosts.yml
---
# Required
domain: example.internal
# Integrated DNS is ON by default, and preflight refuses a run that declares
# neither of these. Pick ONE (declaring both is rejected upstream):
freeipa_server_forwarders: ["192.0.2.1"]
# freeipa_server_no_forwarders: true     # root servers only
# ...or turn integrated DNS off entirely with freeipa_server_setup_dns: false
```

<!-- NOTE: hand-maintained. scripts/update_role_readmes_min_config.py emits a scalar
     "REPLACE_ME_<var>" placeholder for every required var, which is wrong for
     list-typed ones like freeipa_server_forwarders — regenerating this block would
     reintroduce a minimum configuration that cannot run. -->

## Usage

```yaml
- name: Deploy and reconcile FreeIPA server
  hosts: freeipa
  roles:
    - role: freeipa_server
      tags: [freeipa_server]
```

Run it:

```bash
ansible-playbook -i inventories/<env>/hosts.yml playbooks/20_iam_freeipa.yml --tags iam
```

Single server needs no special inventory groups — one host in `freeipa` is the
primary. Add more hosts (optionally to `freeipa_primary`) to grow into a
cluster; non-primary hosts enrol as replicas automatically.

## Tenant model

Identity is normally declared as **one file per tenant** rather than as flat
`freeipa_iam_*` lists in `group_vars`. The role reads **every** file in one run
and flattens them into the native lists before a single reconcile, so one
converge sees the whole realm. Switch it on with one variable:

```yaml
# group_vars/all/realm.yml
freeipa_iam_tenants_dir: "{{ inventory_dir }}/tenants"
```

The path is globbed on the **control node**, so it is a repo path, not a path on
the IPA server. Leave it empty and the role uses the `group_vars` lists instead.

A complete runnable version of everything below — three tenant files, an RBAC
slice, and the export-adoption template — is in
`examples/per-tenant-inventory/`. `examples/rbac-overlay/` is the overlay on its
own.

### Layout

Keep `tenants/` **beside** the playbook, never inside a directory Ansible
auto-loads:

```text
per-tenant-inventory/
├── inventory.yml
├── site.yml                  # applies the freeipa_server role
├── group_vars/all/
│   └── realm.yml             # role contract: connection, DNS, CA, tenants_dir
└── tenants/                  # ← freeipa_iam_tenants_dir
    ├── global.yml            # objects belonging to no tenant
    ├── acme.yml
    └── globex.yml
```

That placement is deliberate. These files are **not** picked up by any Ansible
auto-loading mechanism — they are not `group_vars`, not `host_vars`, and not
`vars_files`. Nothing reads them until the role globs the directory you name in
`freeipa_iam_tenants_dir` and loads each file explicitly. Keeping the directory
out of an auto-loaded location makes that obvious: a path Ansible never walks on
its own cannot be mistaken for one it does. If your layout puts playbooks in
their own subdirectory, point past it — `"{{ playbook_dir }}/../tenants"` keeps
the climb visible in the value itself.

Keep the two planes separate: `group_vars` carries the **role contract** (how to
build the realm), each tenant file carries **identity** (what lives in it).

### A global tenant

Built-ins, cross-tenant service accounts and realm-wide policy belong to no
tenant. Put them in one file marked `shared`:

```yaml
# tenants/global.yml
---
tenant: global
shared: true

freeipa_iam_users:
  - {name: admin, groups: [admins], first: admin, last: Administrator}
  - {name: svc-ldap-bind, groups: [ipausers, app-bind], first: Service, last: LDAP Bind}

freeipa_iam_usergroups:
  - {name: admins, description: Account administrators}
  - {name: ipausers, description: built-in}
  - {name: app-bind, description: Directory bind accounts}
```

### Two tenants

Each tenant file is small and owns only its own objects:

```yaml
# tenants/acme.yml
---
tenant: acme

freeipa_iam_users:
  - {name: acme-admin-1, groups: [ipausers, acme-admins], first: Acme, last: Admin1}
  - {name: acme-viewer-1, groups: [ipausers, acme-viewers], first: Acme, last: Viewer1}

freeipa_iam_usergroups:
  - {name: acme-admins, description: Acme admins}
  - {name: acme-viewers, description: Acme viewers}
```

```yaml
# tenants/globex.yml
---
tenant: globex

freeipa_iam_users:
  - {name: globex-admin-1, groups: [ipausers, globex-admins], first: Globex, last: Admin1}

freeipa_iam_usergroups:
  - {name: globex-admins, description: Globex admins}
```

A file may carry its **whole** configuration, not just users and groups —
`freeipa_iam_hbac_rules`, `freeipa_iam_sudo_rules`, `freeipa_iam_hostgroups`,
`freeipa_server_dns_records`, automember, and so on. Hand-friendly short keys
work too (`users`, `groups`, `hbac_rules`, `sudo_rules`, `dns_records`), which is
what lets a `--tags export` snapshot be dropped in as a starting point.

### The RBAC overlay inside a tenant file

The overlay is compiled **after** the tenant load, so a tenant file can carry its
own slice. A role is a plain group nested into groups that already exist; granting
it is a one-line diff to `members`:

```yaml
# tenants/acme.yml (continued)
freeipa_server_rbac_roles:
  - name: role-acme-platform-admin
    description: Acme platform admins
    member_of: [acme-admins]          # must ALREADY exist natively
    members: [acme-admin-1]           # must ALREADY exist natively
    hbac_rules:                       # optional — usergroup: [role-…] injected
      - {name: hbac-acme-platform-admin, hostgroup: [hg-acme], service: [sshd, sudo]}
```

Field-by-field reference, including `sudo_rules` and the category axes:
`docs/rbac_roles.md`.

### Rules that bite

- **Objects concatenate across files; a name must appear in exactly ONE file.**
  Declaring `acme-admins` in two tenants applies it twice.
- **`member_of` and `members` targets must already exist** natively or in the
  realm. The overlay nests into groups and grants to users; it invents neither.
  (`freeipa_server_rbac_allow_missing_member_of` /
  `freeipa_server_rbac_allow_unknown_users` relax this deliberately.)
- **Overlay lists concatenate across tenant files, but a tenant-declared
  `freeipa_server_rbac_roles` REPLACES a `group_vars` one.** Declare the overlay
  in tenant files *or* in `group_vars`, never split across both.
- **`tenant:` is also a variable.** A file is loaded with `include_vars`, so any
  value may reference any other var the file defines, plus `group_vars` —
  `name: "hg-{{ tenant }}-{{ env }}"` works. Define your own scalar for a
  per-file variant.
- **Prefix a helper with `_` to keep it out of the merge.** Ad-hoc keys are
  skipped so a file can build its own values, but a key that *resembles* an
  object key (`freeipa_iam_usergroup`, `hbac_rule`, `sudorules`, `my_users`) is
  rejected outright rather than silently dropped — a one-character typo in a list
  name would otherwise drop every object it should have declared. The `_` prefix
  is checked first and always wins, so `_my_users` is safe where `my_users` is
  refused.
- **Server settings in a tenant file are ignored, loudly.** `freeipa_server_domain`,
  `freeipa_server_forwarders` and the export scope markers warn and have no
  effect; they belong in `group_vars`.
- **An empty directory or a load that assembles zero users aborts the run.** A
  declared `freeipa_iam_tenants_dir` that globs nothing is always a
  misconfiguration, and an empty desired state under
  `freeipa_server_authoritative` would archive the whole realm.
- **`shared: true` marks intent, not enforcement.** The merge records per-object
  ownership from `tenant:`/`shared:`, but no phase reads those maps today — it
  does not isolate tenants or exempt objects from the prune. What makes shared
  groups reconcile correctly is that the load sees every tenant at once, not the
  ownership stamp. Scope still lives in the names you declare.

## Preconditions

- FIPS mode must already be enabled (`fips-mode-setup --enable`) and the host
  rebooted before install when `freeipa_server_fips_required` is true — FreeIPA
  cannot be migrated into FIPS after install.
- Chrony must be tracking with clock skew inside
  `freeipa_server_preflight_time_tolerance_sec` — Kerberos auth fails above
  300s skew.
- A replica install needs the primary's LDAP port (389) already reachable.
- An offline install (`freeipa_server_offline: true`) needs
  `freeipa_server_package_repo` defined with a working baseurl.

## Behaviour

- `freeipa_server_ca_mode` picks the realm's CA at **install** time:
  `self-signed` (default; IPA-minted root, trusted only where you
  distribute `/etc/ipa/ca.crt`), `external-ca` (two-phase — the first run
  emits a CSR at `/root/ipa.csr`, sign it externally, set
  `freeipa_server_external_cert_files`, and re-run), or `ca-less` (no CA of
  its own; you supply every service cert). The install path is a no-op once
  `/etc/ipa/default.conf` exists — changing `ca_mode` later does not convert
  the realm. To reparent a **working** self-signed CA under the org offline
  CA without reinstall, use playbook `--tags reparent_ca` /
  `scripts/freeipa-reparent-ca.sh` (`ipa-cacert-manage renew --external-ca`).
  See `docs/runbooks/freeipa-external-ca-reparent.md`.
- `freeipa_server_trusted_external_cas` additively imports third-party CA
  certs into the trust store without changing the serving CA.
- Prune (`freeipa_server_authoritative: true`) archives undeclared users
  (recoverable — `ipa user-undel`) and deletes undeclared matrix-managed
  objects. **Object deletion is NOT recoverable**: a group cannot be
  preserve-archived, so a deleted group and its memberships are gone. Object
  deletion is scoped — only names containing `freeipa_iam_reconcile_scope` are
  eligible, a blank scope deletes nothing, and
  `freeipa_iam_reconcile_protect_regexes` shields externally-owned names. That
  shield ships **empty** — the role carries no patterns for any particular
  third-party product — so declare your realm's in inventory; an armed prune
  with both protect lists empty warns and shields nothing by pattern.
  `freeipa_iam_protected_groups` is separate and unconditional: those groups are
  never deleted at any setting, and are exempt from both undeclared-member
  removers (the declarative strip and the eviction/snap-back pass).
- **Offboarding has two routes, and the explicit one is ungated.** *Inferred*: a
  managed user missing from `freeipa_iam_users` becomes an archival candidate,
  but only under `freeipa_server_authoritative` — dropping someone from
  `group_vars` must not deactivate them on an ordinary converge. *Explicit*:
  `freeipa_iam_users_preserved` is the leaver's own entry, **moved** out of
  `freeipa_iam_users`. It runs at **every** setting, because an entry there is an
  instruction rather than an inference from absence — so you can offboard one
  person on an additive run without arming whole-realm authoritative mode (which
  would also enable group deletion and the membership strip). It accepts a bare
  login or a whole user dict, so the move is a literal cut-and-paste, and it only
  ever *preserves* (`user-del --preserve`, recoverable via `ipa user-undel`)
  regardless of `freeipa_iam_preserve_archived`. The two routes agree in steady
  state and are each idempotent, so running both is free.
  **Move, do not copy:** a login in both lists is a hard validation failure
  naming the user. It is refused rather than resolved, because task order would
  otherwise decide it — the reactivate pass would undel the account early and the
  offboarding pass would re-archive it late, so the run would never reach
  `changed=0` and the account would be able to authenticate in between. Names are
  folded, so `Alice` in one list and `alice` in the other is still caught.
- **"Additive" covers usergroup membership and object existence — nothing
  else.** Every other member or attribute list that a declared object *carries*
  is reconciled declaratively at *every* setting, defaults included: HBAC and
  sudo rule members, hostgroup nesting **and its `host:` members**,
  hbacsvcgroup/sudocmdgroup nesting, permission/privilege/role members,
  automember conditions, and a user's
  `principal`/`manager`/`certificate`/`certmapdata`. The upstream modules
  compute `del_list = current − declared`, so a member added out of band is
  removed on the next converge even with `freeipa_server_authoritative: false`.
  Sudo rules are the worst case — no precheck gate, so they reconcile on *every*
  run — and every rule the RBAC overlay generates has `usergroup: [<role>]`
  injected unconditionally, so any other group bound to it is revoked each run.
  The strip exemptions (`freeipa_iam_protected_groups` plus automember-owned
  groups) cover usergroup membership only; there is no exemption mechanism for
  the lists above. See `defaults/main.yml` under "Pruning control".
- Delete (`freeipa_iam_delete: true`) is hard and irrecoverable — it enables
  `ipa *-del`; `--tags delete` or `--tags prune_preserved` picks the
  operation. `--check` makes either a dry-run.

## Out of scope

- Host enrolment — handled by `freeipa_client`.
- Removing a master from the realm — a deliberate, manual operator action, not
  automated by this role.
