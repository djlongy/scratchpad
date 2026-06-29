# freeipa_server

Portable, multi-OS FreeIPA **server** role. Wraps the upstream
`freeipa.ansible_freeipa` `ipaserver`/`ipareplica` roles for install, and adds cold-start
resilience, a scheduled backup, declarative IDAM reconciliation, and opt-in post-install
hardening. Pairs with [`freeipa_client`](../freeipa_client/) for host enrolment.

**Platforms:** EL-family (RHEL/Rocky/Alma/CentOS/Fedora) and Debian/Ubuntu — the full FreeIPA
*server* support matrix.

## Phases (tags)

A no-tag run runs everything; tag to narrow it.

| Tag | Runs |
|---|---|
| `preflight` | FIPS / time / FQDN / primary-up dependency guards |
| `install` | Upstream primary/replica install (packages, firewall) |
| `configure` / `resilience` | Cold-start timeout override + recovery timer |
| `certs` (`ca`) | Import trusted external CAs into the IPA trust store (opt-in) |
| `hardening` | Post-install hardening (opt-in) |
| `backup` | Backup timer + opt-in controller offload |
| `idam` (`users`/`groups`/`hbac`/`sudo`/`automember`/`reconcile`/`report`) | Declarative identity reconciliation |
| `dns` | Declarative DNS zones / forward-zones / records (opt-in) |
| `adtrust` (`trust`) | Active Directory trust(s) (opt-in) |
| `export` | Snapshot a live realm to declarative vars (`never`-gated; read-only) |
| `restore` | Restore from a backup (`never`-gated; break-glass) |
| `delete` | Hard-delete declared objects (`never`-gated; mock/test only) |

Everything except `install`/`preflight` runs **only on the primary** (replicas get it via
FreeIPA replication).

```bash
# reconcile identity after editing your freeipa_idam_* group_vars
ansible-playbook -i inventory.yml site.yml --tags idam

# re-apply just one phase
ansible-playbook -i inventory.yml site.yml --tags resilience
```

## Credentials — declared var wins, Vault is the fallback

Every secret resolves the same way: a **declared Ansible var wins; HashiCorp Vault is only the
fallback** (and the lookup is lazy, so it never fires when the var is set). You can run the role
with passwords supplied directly and **no Vault at all**.

| Secret | Declared var (wins) | Vault fallback |
|---|---|---|
| IPA admin password | `freeipa_server_admin_password` | `freeipa_server_vault_secret` : `admin_password` |
| Directory Manager password | `freeipa_server_dm_password` | `freeipa_server_vault_secret` : `dm_password` |

`freeipa_server_admin_password` is the **single** admin credential for the whole role (install,
IDAM, reconcile, export, delete — no separate `freeipa_idam_*` credential vars). Provide one
column: the password var(s), **or** the `freeipa_server_vault_secret` path.

| Phase | Minimum to run |
|---|---|
| `export` | admin password (declared or Vault) |
| `idam` | admin password + your `freeipa_idam_*` data |
| `install` | admin **and** DM password + `freeipa_server_forwarders` |

Derived automatically (override only to break convention): `freeipa_server_domain` (`{{ domain }}`),
`freeipa_server_realm` (`{{ domain | upper }}`), and `freeipa_server_is_primary` /
`_primary_host` / `_primary_ip` (from the `freeipa_primary` inventory group + `ansible_host`).

## Topology — single server vs. cluster

Topology comes from inventory, no code change:

- **Single server** — one host in the `freeipa` group: it is the primary, the replica path
  never runs, IDAM/backup/hardening all run on it.
- **Cluster** — add hosts to `freeipa` (optionally `freeipa_primary` / `freeipa_replica` for
  clarity); extra hosts enrol as replicas. `freeipa_server_has_replicas` reflects this.

## Install options

`freeipa_server_ca_mode` (`self-signed`|`external-ca`|`ca-less`), `freeipa_server_setup_kra`,
`freeipa_server_setup_dns`, `freeipa_server_forwarders` + `_forward_policy`,
`freeipa_server_idstart`/`_idmax`, `freeipa_server_mkhomedir`, `freeipa_server_setup_ntp` +
`_ntp_servers`, `freeipa_server_fips_required`. Full surface + item shapes in
`defaults/main.yml`.

**`external-ca` is two-phase:** the first run emits `/root/ipa.csr`; sign it with your CA,
supply the chain via `freeipa_server_external_cert_files`, re-run. (This makes the IPA CA a
*subordinate* of your CA. To instead *trust* other CAs alongside the IPA CA, see below.)

## Trusting external CAs (additive)

`freeipa_server_trusted_external_cas` imports third-party CA certs into the IPA trust store so
IPA and its enrolled clients trust certs issued by them — *additive*, it doesn't change the IPA
CA. Idempotent; skipped in `ca-less` mode. Run the `certs` tag to apply just this.

```yaml
freeipa_server_trusted_external_cas:
  - { name: corp-root, src: files/corp-root.pem }    # PEM on the control node
  - name: partner-domain
    content: |                                        # …or inline PEM
      -----BEGIN CERTIFICATE-----
      ...
```

## Backup — restore / offload

`backup.yml` schedules `ipa-backup` on a timer and leaves the backup dir readable for an
external backup agent. Two extras wrap the upstream `ipabackup` role:

- **Offload to controller** — set `freeipa_server_backup_fetch_name` (a backup name, or `all`)
  to copy backups to `freeipa_server_backup_controller_path` on the next run.
- **Restore (break-glass, DESTRUCTIVE)** — `--tags restore -e freeipa_server_restore_name=<name>`;
  `never`-gated so it can't run by accident.

## Declarative DNS

With IPA-integrated DNS, manage zones/records declaratively (idempotent):

```yaml
freeipa_server_dns_zones:
  - { name: "internal.example.com", dynamic_update: true, allow_sync_ptr: true }
freeipa_server_dns_forward_zones:
  - { name: "corp.example.com", forwarders: ["10.0.0.53"], forwardpolicy: "first" }
freeipa_server_dns_records:
  - { zone_name: "internal.example.com", name: "app1", record_type: "A", record_value: "10.0.0.20" }
```

## Automember

Auto-assign hosts/users to a group by attribute regex (the target group must already exist):

```yaml
freeipa_server_automember_rules:
  - name: linux-hosts
    automember_type: hostgroup            # hostgroup | group
    inclusive:
      - { key: "fqdn", expression: ".*\\.example\\.com$" }
```

This closes the enrolment gap: a freshly-enrolled host is placed in its hostgroup at host-create
(no rebuild needed), so an HBAC rule targeting that hostgroup reaches it immediately — without
chaining the client and server plays. A rebuild only re-evaluates *pre-existing* members; the
role runs one automatically when a rule changes, or force it with
`freeipa_server_automember_rebuild: true`.

## Active Directory trust (opt-in, off by default)

Trust an AD forest so AD users authenticate against IPA resources. Needs the AD trust
controller, reachable AD DCs, and two-way DNS.

```yaml
freeipa_server_setup_adtrust: true           # --setup-adtrust at install
freeipa_server_ad_trusts:
  - { realm: "AD.EXAMPLE.COM", admin: "Administrator", password_field: "ad_trust_password", two_way: true }
```

## Hardening (opt-in, off by default)

`freeipa_server_harden_anonymous_bind`, `freeipa_server_search_size_limit` / `_search_time_limit`,
`freeipa_server_require_otp_groups`, `freeipa_server_disable_allow_all` (guarded — refuses unless
a replacement HBAC rule exists), `freeipa_server_crypto_policy` (report-only).

---

## Declarative IDAM provisioning

The heart of the role: reconcile a declarative description of identity — users, groups,
hostgroups, HBAC, sudo, password policies, delegation roles — into the realm. Run with
`--tags idam`. The `idam` phase is fully idempotent and validates first, reporting *all*
referential problems at once before it touches anything.

### Native dicts are the source of truth

You declare everything in native `freeipa_idam_*` lists (every list + item shape is documented
in `defaults/main.yml`):

| Var | Holds |
|---|---|
| `freeipa_idam_usergroups` | groups: `{ name, description?, gidnumber?, group: [nested], user: [members] }` |
| `freeipa_idam_users` | users: `{ name, givenname, sn, email?, groups: [...], shell?, password? }` (`first`/`last` accepted as aliases) |
| `freeipa_idam_hostgroups` | `{ name, description?, host: [...], hostgroup: [...] }` |
| `freeipa_idam_hbacsvcs` / `_hbacsvcgroups` | custom HBAC services + service bundles |
| `freeipa_idam_hbac_rules` | `{ name, usergroup: [...], hostgroup: [...], service: [...] }` (or `*category: all`) |
| `freeipa_idam_sudo_commands` / `_sudocmdgroups` | sudo commands + command bundles |
| `freeipa_idam_sudo_rules` | `{ name, usergroup, hostgroup, cmd: [...] }` (or `cmdcategory: all`) |
| `freeipa_idam_pwpolicies` | per-group password policy `{ name, maxlife, minlength, … }` |
| `freeipa_idam_permissions` / `_privileges` / `_iparoles` | native IPA delegation (RBAC of management *privileges*) |

`--tags export` snapshots a live realm into exactly this shape, so you can adopt an existing
instance and reapply it idempotently (see "Adopt an existing instance").

### The two-tier role/policy group model

A FreeIPA *user group* can't itself hold HBAC/sudo/host rules, so policy is decoupled from
membership:

- **`role-*` (grant group)** — people are members of *this*.
- **`ug-*` (policy group)** — HBAC/sudo/pwpolicy rules target *this*; it **contains** the role
  group (`ug-x` carries `group: [role-x]`).

A user in `role-x` is an **indirect** member of `ug-x`, so every rule pointing at `ug-x`
applies. (The nesting direction is load-bearing — the reverse does not work.)

### Two ways to assign users to groups

- **`freeipa_idam_roles`** — a flat named bundle of groups; a user's `roles: [...]` expands to
  the union of those groups, added *directly* to the user. No role group, no nesting.

  ```yaml
  freeipa_idam_roles:
    - { name: platform-admin, groups: [ug-gitlab-admins, ug-docker-operators] }
  freeipa_idam_users:
    - { name: alice, givenname: A, sn: Smith, roles: [platform-admin] }   # alice → both groups directly
  ```

- **The thin RBAC overlay** (`freeipa_server_rbac_*`) — generates a `role-*` group, nests it
  into existing `ug-*` policy groups, and adds the user to the role group only (so the user is
  an *indirect* member of the policy groups). This is the two-tier model above, automated.

### The thin RBAC overlay (optional, external to the role)

The role itself has **no knowledge** of the overlay — it consumes only native dicts. The overlay
is three external, decoupled pieces, so it sits on top of any native baseline (drop it and you
run from raw dicts):

1. **`filter_plugins/freeipa_rbac.py`** — the compiler (a Jinja filter).
2. **group_vars `freeipa_server_rbac_*`** — the overlay data you author.
3. **a playbook pre_task** — validates + compiles + merges the overlay into
   `freeipa_idam_usergroups` + `freeipa_idam_users` **before** the role runs (null-safe + gated:
   no `freeipa_server_rbac_roles` ⇒ no-op ⇒ pure baseline). Ship it as one reusable
   [`freeipa_server_rbac_compile.yml`](examples/rbac-overlay/freeipa_server_rbac_compile.yml).

**Tenancy is a hard boundary.** The overlay data is a nested tree — a role is *defined* inside
its `tenant → environment` cell and *assigned* by naming that exact cell — so one grant can
never reach a second tenant or environment. Breadth is opt-in only: list each cell explicitly.

```yaml
# DEFINITIONS — tenant → environment → role → {description?, policy_groups}
freeipa_server_rbac_roles:
  acme:
    prod:
      platform-admin:
        policy_groups:                         # nest into EXISTING native ug-* groups
          - { service: gitlab, privilege: admins }     # → ug-acme-prod-gitlab-admins
          - { service: docker, privilege: operators }
# ASSIGNMENTS — user → tenant → environment → [roles]  (every grant fully qualified)
freeipa_server_rbac_user_assignments:
  alice: { acme: { prod: [platform-admin] } }  # alice → role-acme-prod-platform-admin → indirect ug-*
```

Policy groups **must already exist natively** (that's where the HBAC/sudo point) — the overlay
nests onto them, it never invents them. Names come from `freeipa_server_rbac_naming`
(`role_template` / `policy_group_template`). `freeipa_rbac_validate` fails fast on an assignment
to an **undefined** `(tenant, environment, role)` cell (enforcing tenant isolation), a missing
policy group, a built-in collision, or a user not in `freeipa_idam_users`. The flat
`freeipa_server_rbac_role_sets` list is **removed** (the compile step rejects it). A runnable
3-tenant × 3-environment template is in [`examples/rbac-overlay/`](examples/rbac-overlay/).

(Distinct from native IPA **`freeipa_idam_iparoles`** — delegation of IPA-*management*
privileges, e.g. a helpdesk role, not app/host access.)

### Additive by default; one switch prunes

Creation is *additive* — `state: present` never deletes. **`freeipa_server_authoritative`**
(default `false`) is the single switch that turns on pruning:

| Mechanism (when `true`) | Removes | Scoped by |
|---|---|---|
| Membership reconcile | members no longer declared in a managed group | the declared, non-protected groups |
| Group-existence reconcile | groups dropped from `freeipa_idam_usergroups` | the `idam-managed-groups` container marker |
| Object reconcile | orphaned `ug-`/`hg-`/`hbac-`/`sudo-`/automember objects | name substring `freeipa_idam_reconcile_scope` (blank ⇒ nothing) |

Removed **users** are archived (preserved, recoverable) via the `idam-managed-users` marker, not
destroyed. **Authoritative is realm-scoped** — only run it against a *complete* assembled desired
state, never a partial tenant file, or it prunes the other tenants.

**`freeipa_idam_reconcile_memberships_only`** (default `false`) is the **safe nightly
drift-revoke mode**: it runs the *membership* reconcile (strips members no longer declared —
e.g. a user who added themselves to a privileged group) but **suppresses every deletion** (no
group-existence delete, no user archival, no object reconcile). It enables the strip on its own
(no `authoritative` needed) and, removing *nothing*, is **safe to run per-tenant against a
partial file** — the right mode for a nightly cron. Reserve full `authoritative` for a
deliberate, complete run when you actually intend to delete objects.

> **Scope boundary:** object reconcile manages only `group`/`hostgroup`/`hbacrule`/`sudorule`/
> automember. The leaf building blocks (`hbacsvc`, `sudocmd`, `permission`, `privilege`,
> `iparole`, `pwpolicy`) are left orphaned when undeclared — a removed `iparole` delegation
> stays in force, so revoke it with an explicit `state: absent`.

### Account types & state controls

- `freeipa_idam_service_accounts` — non-human accounts, forced to the nologin shell.
- `freeipa_idam_breakglass_accounts` (+ `_breakglass_group`) — emergency accounts: login stays
  on, auto-protected from archival.
- `freeipa_idam_nologin_accounts` / `freeipa_idam_disabled_accounts` — force existing accounts to
  nologin (still active for API) or fully disable them; admin-lockout guards refuse to disable
  `admin` (or nologin `admin` without a break-glass account).
- `freeipa_idam_default_user_password` (create-time only), `freeipa_idam_group_gids` (deterministic
  GIDs), `freeipa_idam_hbac_rules_disable` (guarded), `freeipa_idam_reactivate_preserved`
  (re-declaring an archived user undeletes it).

Protected/never-touched: `freeipa_idam_protected_users` (incl. `admin`),
`freeipa_idam_protected_groups` (incl. `admins`, `ipausers`).

## Adopt an existing instance (`--tags export`)

Snapshot a hand-built FreeIPA into this role's declarative contract, then reapply it — no
green-field rebuild. The export is **read-only** (`*_find`/`*_show` only) and `never`-gated.

```bash
# admin password directly (best from an Ansible-Vault file so it isn't in shell history):
ansible-playbook -i inventory.yml site.yml --tags export -e @secrets.yml
# …or fall back to Vault (set freeipa_server_vault_secret): just --tags export
```

Writes `freeipa.config.snapshot.yml` on the control node; move it into group_vars to reapply.
It captures the realm/domain, users, groups (+ nesting + member-managers), hostgroups, custom
HBAC services, HBAC/sudo rules, password policies, and automember rules — drop-in and idempotent,
**including onto a fresh server** (users + custom services are created before the rules that
reference them, so there's no first-run ordering race).

**Not captured:** passwords / Kerberos keys (unreadable), POSIX uid/gid numbers (IPA reassigns,
avoiding collisions), hostgroup host rosters (re-derived by enrolment + automember; opt in with
`freeipa_server_export_include_host_membership=true`), and the built-in `global_policy`. SSH keys
are off by default (`freeipa_server_export_include_sshkeys=true`).

## See also

- [`examples/rbac-overlay/`](examples/rbac-overlay/) — runnable 3-tenant × 3-environment
  RBAC-overlay template (native policy groups + the thin role overlay)
- [`freeipa_client`](../freeipa_client/) — host enrolment
- [`hashicorp_vault`](../hashicorp_vault/) — credential source
