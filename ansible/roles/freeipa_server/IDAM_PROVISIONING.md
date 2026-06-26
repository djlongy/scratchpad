# FreeIPA IDAM — How User & Group Provisioning Works

A fresh, end-to-end explanation of the `freeipa_server` role's identity provisioning:
what's built, what it can do, every toggle, and what's deliberately left out.

---

## The mental model

The `freeipa_server` role reconciles a **declarative description of identity** (users,
groups, HBAC, sudo, etc.) into a live FreeIPA realm. It runs **only on the primary** (the
replica gets it via FreeIPA's own replication), server-side via `ipalib` using the official
`freeipa.ansible_freeipa` modules (`ipagroup`, `ipauser`, …).

There are **two layers of input**, and both compile down to the same native dictionaries:

1. **Native baseline dicts** — `freeipa_idam_usergroups`, `freeipa_idam_users`,
   `freeipa_idam_hostgroups`, `freeipa_idam_hbac_rules`, `freeipa_idam_sudo_rules`, …
   These are the ground truth. You can `--tags export` a live realm into exactly this shape
   and re-apply it to rebuild. You can run the whole thing from these raw lists with no
   abstraction at all.
2. **The access-matrix overlay** (`freeipa_idam_access_matrix`) — a compact
   `tenant × environment × application × privilege_level` matrix. The matrix playbook's
   `pre_tasks` compile it (via filter plugins) and **merge it onto the native dicts** before
   the role runs. So the matrix is sugar; behind the scenes it's still the native
   dictionaries. This is the "closer to native" model.

---

## The two-tier group model (proven on the live realm)

For each generated cell you get two groups:

- **`role-*` (grant group)** — people join *this*.
- **`ug-*` (policy group)** — HBAC/sudo/password-policy rules target *this*.

The `ug-` policy group **contains** the `role-` group (`ug-x` carries `group: [role-x]`). A
user added only to `role-x` becomes an **indirect member** of `ug-x`, so every rule pointing
at `ug-x` applies to them.

```
alice ──▶ role-acme-prod-platform
              ├─(nested into)▶ ug-acme-prod-gitlab-admins   ◀── HBAC/sudo target these
              ├─(nested into)▶ ug-acme-prod-docker-operators
              └─(nested into)▶ ug-acme-prod-monitoring-admins
```

That's the abstraction: assign a person to *one* role group and they inherit access from many
policy groups — instead of hand-adding them to 50 granular groups. The direction was verified
empirically; the reverse form (`role-*` carrying `group: [ug-*]`) does **not** work.

There's also a lighter helper: **`freeipa_idam_roles`** — a "role = named bundle of groups"
where a user's `roles: [...]` expands to the union of those groups at apply time (flat
expansion, not nesting). Don't confuse it with native IPA **`iparoles`** (delegation roles
that bundle *privileges* for helpdesk-style powers).

---

## What a run actually does (provisioning order)

1. **Validate** inputs are well-formed lists; reject deprecated field names; run one surgical
   pass that collects *every* referential problem (unknown group, missing first/last, dangling
   HBAC service, …) and reports them all at once.
2. **Build the effective sets**: fold in service + break-glass accounts, expand `roles` →
   groups, compute desired usernames, group names, and merged user↔group memberships.
3. **Create, in dependency order** (one bulk call where the module supports it): groups →
   users (`update_password: on_create`, so existing users are never reset) → password policies
   → hostgroups → HBAC services/service-groups/rules → sudo commands/command-groups/rules →
   permissions → privileges → iparoles.
4. **Memberships**: apply user↔group and nested-group memberships.
5. **Existence tracking** via marker containers (see deletion below).
6. **Then** (in `main.yml`): object reconcile → automember rules → end-of-run summary report.

---

## The three deletion mechanisms (the subtle part)

Creation is always *additive* — `state: present` never deletes. Deletion is opt-in and comes
in three distinct flavours, all governed by the single authoritative switch:

| Mechanism | What it removes | How it's scoped |
|---|---|---|
| **Membership reconcile** | members no longer declared *in* a managed group | only non-protected groups you declare |
| **Group-existence reconcile** | whole groups you removed from `freeipa_idam_usergroups` | a **container marker** (`idam-managed-groups`) — only groups the role itself enrolled are eligible |
| **Object reconcile** | orphaned `ug-`/`hg-`/`hbac-`/`sudo-`/automember objects | a **name substring** (`reconcile_scope`, e.g. `acme-prod`) — only names containing it |

Users have their own existence marker (`idam-managed-users`): a user dropped from config is
**archived/preserved** (deactivated, recoverable) rather than destroyed.

---

## The switches that matter

### The one you'll actually set

- **`freeipa_server_authoritative`** *(default `false`)* — the master switch, and the **sole**
  pruning control (there are no per-mechanism flags).
  - `false` = **additive-safe**: create/update everything declared, prune *nothing*.
  - `true` = **authoritative**: undeclared members stripped, undeclared groups/objects deleted,
    removed users archived. The same boolean directly gates all three deletion mechanisms —
    membership reconcile, group-existence reconcile, and object reconcile — each task keys on
    `freeipa_server_authoritative | bool` directly.
- **`freeipa_idam_reconcile_scope`** — the scope marker for object pruning. **Blank = nothing
  is object-pruned** (fail-safe). The matrix sets it to `<tenant>-<environment>`.

### Safety / identity guards

- `freeipa_idam_protected_users` (`admin`) / `freeipa_idam_protected_groups`
  (`admins`, `editors`, `ipausers`, `trust admins`) — never deleted.
- `freeipa_idam_unmodifiable_users` (`admin`) — never have attributes written (admin lacks the
  `inetOrgPerson` objectClass).
- `freeipa_idam_preserve_archived` *(default `true`)* — archive vs hard-delete removed users.
- `freeipa_idam_managed_group` / `freeipa_idam_managed_groups_group` — the marker container
  names (override these to isolate — exactly how the validation kit stays safe).

### Provisioning conveniences

- `freeipa_idam_default_user_password`, `freeipa_idam_email_domain` — create-time defaults.
- `freeipa_idam_group_gids` — deterministic GIDs (stable POSIX across DR rebuilds).
- `freeipa_idam_service_accounts` + `freeipa_idam_nologin_shell` — non-human accounts forced to
  nologin.
- `freeipa_idam_breakglass_accounts` + `freeipa_idam_breakglass_group` — real emergency
  accounts (login stays on, auto-protected from deletion).
- `freeipa_idam_nologin_accounts` — existing accounts to force to the nologin shell
  (interactive login OFF, account stays active for API/kinit). Append `admin` or any kept
  account; a lockout guard refuses to nologin `admin` unless a break-glass account exists.
- `freeipa_idam_disabled_accounts` — existing accounts to `ipa user-disable` (fully OFF,
  cannot authenticate). `admin` is refused (you'd lose API admin).
- `freeipa_idam_reactivate_preserved` *(default `true`)* — re-declaring a previously-archived
  user reactivates (undeletes) it instead of failing the run with "no such entry".
- `freeipa_idam_hbac_rules_disable` — disable stock rules like `allow_all` (with a lockout
  guard).
- `freeipa_idam_report` *(default `true`)* — the boxed end-of-run summary.
- `freeipa_idam_apply_via_api` *(default `false`)* — native server-side path vs the slower
  community-module API path.

### Removed (no back-compat shim)

The old per-mechanism flags `freeipa_idam_membership_declarative`,
`freeipa_idam_reconcile_objects`, and `freeipa_idam_reconcile_groups` were **removed** — they
no longer exist, not even as aliases. `freeipa_server_authoritative` is the only pruning
control; set just that one var. (The validation kit's README still mentions leaving them
"unset" for safety — harmless, since unknown vars are simply ignored.)

---

## What's deliberately left out

### Out of scope (kept the simpler/better path)

- The spec's minimal `freeipa_rbac.py` DSL compiler — **not built**. The existing access-matrix
  already compiles the two-tier model and is more capable; a second compiler would have added
  complexity, not removed it.
- **Per-item "created vs modified" reporting** — the bulk modules only return an *aggregate*
  `changed`, so the report says yes/no per phase. Itemised data is only available for
  *removals* (which we do show). True per-item create/modify would need a before/after state
  query per object.
- **Authoritative pruning covers a FIXED set of object types** — `group`, `hostgroup`,
  `hbacrule`, `sudorule`, and `automember` rules (plus user archival via the user marker and
  group-existence via the container marker). It deliberately does **not** delete the leaf
  *building blocks* — custom HBAC services (`hbacsvc`), HBAC service-groups (`hbacsvcgroup`),
  sudo commands (`sudocmd`), sudo command-groups (`sudocmdgroup`) — nor the RBAC delegation
  chain (`permission` / `privilege` / `iparole`) or password policies (`pwpolicy`). Dropping
  one of those from config under `authoritative` leaves the live object **orphaned, not
  deleted**. This is intentional: a shared building block (e.g. a `sudocmd` referenced by
  another rule) must not vanish because one rule stopped using it. **Caveat worth knowing:** a
  removed **`iparole` delegation therefore stays in force** — to revoke a delegated power, set
  that role/permission/privilege `state: absent` explicitly (or delete it out-of-band), don't
  just delete the lines. (Verified live against a test realm.)

### Gated because dangerous

- **Hard-deleting users/groups** — gated behind both the `never` tag *and* `freeipa_idam_delete`
  (mock/test only). Real inventories archive, never destroy.
- **Authoritative pruning is realm-scoped, never per-file.** The multi-tenant landmine: if you
  run `authoritative: true` against a *partial* tenant file, it treats everything else as
  "undeclared" and prunes the other tenants. The rule: assemble the **complete** per-realm
  desired state first, then apply authoritatively. `reconcile_scope` scoping and the marker
  containers are the structural guardrails that keep a mistake bounded.
- **Passwords / Kerberos keys** are not managed declaratively beyond create-time
  (`on_create`), so a run never resets a live user's password.

### Adjacent, intentionally separate

Not part of RBAC provisioning: DNS, automember rules, AD trust, and realm migration are their
own phases/playbooks.

---

## TL;DR

Declare native dicts (optionally via the matrix) → the role creates everything additively →
flip **`freeipa_server_authoritative`** to make it prune to exactly what's declared, scoped by
the marker containers + **`reconcile_scope`** so it can never run away. Everything destructive
is either opt-in, scoped, archived-not-deleted, or `never`-tag-gated.

Pair the role with an isolated, authoritative-mode validation kit (a standalone inventory +
test-prefixed objects scoped by `reconcile_scope`) for a safe, runnable lifecycle demo.
