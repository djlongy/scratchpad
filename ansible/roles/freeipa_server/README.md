# freeipa_server

Portable, multi-OS FreeIPA **server** role. Wraps the upstream
`freeipa.ansible_freeipa` `ipaserver`/`ipareplica` roles for install, and layers
cold-start resilience, a scheduled backup timer, declarative IDAM
reconciliation, and an opt-in post-install hardening baseline.

Pairs with [`freeipa_client`](../freeipa_client/) for host enrolment.

## Quick start

Requirements: `ansible-galaxy collection install freeipa.ansible_freeipa`
(plus `community.general` for one hardening task, and `community.hashi_vault`
only if you use the Vault password fallback). Targets: EL-family
(RHEL/Rocky/Alma/CentOS/Fedora) or Debian/Ubuntu — the full FreeIPA *server*
support matrix; packaging and firewall are handled by the upstream roles.

```yaml
# inventory.yml — a single server needs NO special groups
all:
  hosts:
    ipa01: { ansible_host: 10.0.0.10 }
  vars:
    domain: example.com                    # THE one required var (realm derives)
    freeipa_server_admin_password: "..."   # or freeipa_server_vault_secret
    freeipa_server_dm_password: "..."
    freeipa_server_forwarders: ["10.0.0.1"]

# site.yml
- hosts: all
  become: true
  roles: [freeipa_server]
```

```bash
ansible-playbook -i inventory.yml site.yml            # install + everything
ansible-playbook -i inventory.yml site.yml -t idam    # later: just reconcile identity
```

Add hosts (optionally to a `freeipa_primary` group) to grow into a cluster —
non-primary hosts enrol as replicas automatically.

## Phases (tags)

A no-tag run runs everything except the `never`-tagged ops.

| Tag | Runs |
|---|---|
| `preflight` | FIPS/time/FQDN/primary-up dependency guards |
| `install` | Vault creds + upstream primary/replica install |
| `configure` / `resilience` | Cold-start timeout override + recovery timer |
| `certs` (`ca`) | Import trusted external CAs into the IPA trust store (primary, opt-in) |
| `hardening` | Opt-in post-install hardening (primary) |
| `backup` | Scheduled ipa-backup timer + opt-in controller offload (primary) |
| `idam` (`users`/`groups`/`hbac`/`sudo`) | Declarative IDAM reconciliation (primary) |
| `dns` | Declarative DNS zones / forward-zones / records (primary, opt-in) |
| `automember` | Auto group/hostgroup membership rules (primary, opt-in) |
| `adtrust` (`trust`) | Active Directory trust(s) (primary, opt-in) |
| `restore` | Break-glass restore from a backup (`never`) |
| `delete` | HARD-DELETE declared IDAM objects (`never`; see [Destructive operations](#destructive-operations)) |
| `prune_preserved` | HARD-DELETE orphaned preserved users (`never`; same section) |

```bash
# Re-reconcile IDAM after editing the data
ansible-playbook -i inventories/mgt/hosts.yml playbooks/freeipa_idam.yml --tags idam
# Re-apply just resilience
ansible-playbook -i inventories/mgt/hosts.yml playbooks/freeipa_idam.yml --tags resilience
```

The resilience phase (cold-start recovery timer, ccache cleanup, SSSD self-heal
watchdog) is on by default — disable wholesale with
`freeipa_server_resilience_enabled: false`, or just the watchdog with
`freeipa_server_sssd_selfheal: false`.

## Credentials — declared vars first, HashiCorp Vault as fallback

Every secret resolves the same way: **a declared Ansible var wins; Vault is only the
fallback** (evaluated lazily, so it never fires when the password is supplied). You can
run any part of the role with the password given directly (group_vars, Ansible-Vault
file, or `-e`) and **no HashiCorp Vault at all**.

| Secret | Declared var (wins) | Vault fallback |
|---|---|---|
| IPA admin password | `freeipa_server_admin_password` | `freeipa_server_vault_secret` : `admin_password` |
| Directory Manager password | `freeipa_server_dm_password` | `freeipa_server_vault_secret` : `dm_password` |

`freeipa_server_admin_password` is the **single** admin credential for the *entire* role
(install, IDAM, reconcile, automember, export, delete — no separate `freeipa_idam_*`
credential vars). Provide **one** column; the role asserts at least one source exists.

### Minimum to run each phase

| Phase | Minimum vars |
|---|---|
| `export` | `freeipa_server_admin_password` **or** `freeipa_server_vault_secret` — nothing else |
| `idam` | admin password (declared or Vault) + the `freeipa_idam_*` data |
| `install` | admin **and** dm password + `freeipa_server_forwarders` (e.g. `[192.0.2.1]`) |

Derived automatically (override only to break convention): `freeipa_server_domain`
(`{{ domain }}`), `freeipa_server_realm` (`{{ domain | upper }}`),
`freeipa_server_is_primary` / `_primary_host` / `_primary_ip` (from the `freeipa_primary`
inventory group + `ansible_host`).

## Single-server vs. cluster

Topology comes from inventory — no code change between sizes:

- **Single server:** one host in the `freeipa` group. It's the primary; the replica path
  never runs; backup/IDAM/hardening all run on it.
- **Cluster:** add hosts to `freeipa` (optionally `freeipa_primary` / `freeipa_replica`
  for clarity). Non-primary hosts enrol as replicas.

## Install options (selected)

`freeipa_server_ca_mode` (`self-signed`|`external-ca`|`ca-less`), `_setup_kra`,
`_setup_dns`, `_forward_policy`, `_idstart`/`_idmax`, `_no_hbac_allow`, `_mkhomedir`,
`_setup_ntp` + `_ntp_servers`, `_fips_required`. Full surface in `defaults/main.yml`.

### external-ca is two-phase

`freeipa_server_ca_mode: external-ca` needs a CSR roundtrip: the first run emits
`/root/ipa.csr` (sign it with your CA), then supply the signed chain via
`freeipa_server_external_cert_files` and re-run. Preflight asserts the files are present.
This makes the IPA CA a **subordinate** of your CA — to instead *trust other CAs alongside*
the IPA CA, see below.

## Trusting external CAs (additive to the IPA CA)

`freeipa_server_trusted_external_cas` imports third-party / other-domain CA certs into the
IPA trust store (`ipa-cacert-manage install` + `ipa-certupdate`), so IPA and its enrolled
clients **trust certs issued by those CAs**. Additive (does not change the IPA CA); primary
only, replicates cluster-wide; idempotent; skipped in `ca-less`. Run the `certs` tag to apply
just this. Auth uses the **DM** password from `freeipa_server_vault_secret` (fed on stdin).

```yaml
freeipa_server_trusted_external_cas:
  - { name: corp-root, src: files/corp-root.pem }   # nickname + PEM on the control node
  - name: partner-domain
    content: |                                       # …or inline PEM (full chain)
      -----BEGIN CERTIFICATE-----
      ...
      -----END CERTIFICATE-----
  - name: internal-root                              # …or fetched BY THE SERVER from an
    url: "https://artifactory.example.com/pki/internal-root.pem"   # internal artifact repo
    checksum: "sha256:abc123..."                     # optional; validate_certs: false also supported
```

## Backup restore / offload

`backup.yml` runs `ipa-backup` on a timer; these add the missing paths via the upstream
`ipabackup` role:

- **Restore (break-glass, DESTRUCTIVE)** — `--tags restore` + `-e freeipa_server_restore_name=<backup>`
  (e.g. `ipa-data-2026-06-22-…`). `never`-tagged so it can't run by accident. DM password from Vault.
- **Offload to controller** — set `freeipa_server_backup_fetch_name` (a backup name, or `all`);
  copies to `freeipa_server_backup_controller_path` on the next backup run.

## Declarative DNS

With integrated DNS, manage zones/records declaratively (primary only, idempotent):

```yaml
freeipa_server_dns_zones:
  - { name: "internal.example.com", dynamic_update: true, allow_sync_ptr: true }
freeipa_server_dns_forward_zones:
  - { name: "corp.example.com", forwarders: ["10.0.0.53"], forwardpolicy: "first" }
freeipa_server_dns_records:
  - { zone_name: "internal.example.com", name: "app1", record_type: "A", record_value: "10.0.0.20" }
```

## Automember

Auto-assign users/hosts to a group/hostgroup by attribute regex (target group must already
exist via IDAM):

```yaml
freeipa_server_automember_rules:
  - name: linux-hosts
    automember_type: hostgroup
    inclusive:
      - { key: "fqdn", expression: ".*\\.example\\.com$" }
```

**Solves the enrolment chicken-and-egg**: a host must land in the hostgroup an HBAC rule
grants SSH to, but enrolment is client-side and group/HBAC are server-side. Automember places
a host by FQDN (or other attribute) when IPA creates the host record — so a freshly-enrolled
box is immediately reachable, without chaining the client and server plays.

A new host is placed automatically at host-create. A **rebuild** only re-evaluates members
that *already existed* when a rule was added/changed (e.g. hosts enrolled before the rule);
the role runs it automatically when a rule changes this run. Force a one-off with
`freeipa_server_automember_rebuild: true` (default false).

## Active Directory trust

Trust an AD forest so AD users authenticate against IPA resources. Needs the AD trust
controller, reachable AD DCs, and two-way DNS. Opt-in; **shipped as a capability, not
exercised here (no AD).**

```yaml
freeipa_server_setup_adtrust: true          # enables --setup-adtrust at install
freeipa_server_ad_trusts:
  - { realm: "AD.EXAMPLE.COM", admin: "Administrator", password_field: "ad_trust_password", two_way: true }
```

## Hardening (opt-in, default off)

`freeipa_server_harden_anonymous_bind` (→ `rootdse`), `_search_size_limit` / `_search_time_limit`,
`_require_otp_groups`, `_disable_allow_all` (guarded — refuses unless a replacement HBAC rule
exists), `_crypto_policy` (report-only).

## Declarative IDAM provisioning

Reconciles a declarative description of identity — users, groups, hostgroups, HBAC, sudo,
password policies, delegation roles — into a live realm. Primary only (replicas replicate),
server-side via `ipalib`. Run with `--tags idam`.

### Native dicts are the source of truth

Everything is declared in native `freeipa_idam_*` lists (`_usergroups`, `_users`, `_hostgroups`,
`_hbac_rules`, `_sudo_rules`, `_hbacsvcs`, `_sudo_commands`, `_pwpolicies`, `_iparoles`, …);
`defaults/main.yml` documents every list + item shape. `--tags export` snapshots a live realm
into exactly this shape, so you can adopt an existing instance and reapply it idempotently.

### Per-tenant identity directory (optional front-end)

Instead of one big group_vars file, point **`freeipa_idam_tenants_dir`** (typically
`"{{ inventory_dir }}/tenants"`) at a directory of per-tenant files. The role reads and flattens
**all** of them in **one run** — the precondition for a single declarative reconcile that sees
every tenant at once. Empty = legacy mode (lists directly in group_vars).

Each file is a plain `.yml` of `{tenant, shared?, <object lists>}` and may carry a tenant's
**whole** config. Use the short key (`users`, `groups`, `hbac_rules`, …) **or** the full
`freeipa_idam_*`/`freeipa_server_*` var (e.g. straight from an export). Lists concatenate across
files by target var; users/groups are stamped `_owner` + `_shared`. Loaded via a single
`include_vars`, so a file templates **exactly like any inventory YAML**: a value can reference
**any other var the file defines** (native self-reference) as well as group_vars, including
pulling a whole list in from a group_var (stays a native list). No Jinja ⇒ loads verbatim.
`{{ env }}` is the inventory-wide env; for a per-file naming variant just define your own scalar
(e.g. `env_local: dev`) — no special header key.

```yaml
# inventories/<env>/tenants/acme.yml  — a plain vars YAML, templated like any inventory file
tenant: acme
shared: false
env_local: dev                        # any scalar; referable below like a normal var
ug_prefix: "ug-{{ tenant }}-{{ env_local }}"   # file-local var referencing other file-local vars
groups:
  - { name: "{{ ug_prefix }}-admins", description: "Acme admins" }   # -> ug-acme-dev-admins
users:
  - { name: acme.dave, givenname: Dave, sn: Okafor, groups: ["{{ ug_prefix }}-admins"] }
freeipa_idam_hbac_rules: "{{ shared_hbac_baseline }}"   # pull a whole list from group_vars
dns_records:
  - { zone_name: ipa.example.com., records: [ { record_name: app1, a_record: [10.0.0.20] } ] }
```

### The two-tier role/policy group model

A FreeIPA *user* group can't hold HBAC/sudo/host rules, so policy is decoupled from membership:

- **`role-*` (grant group)** — people are members of *this*.
- **`ug-*` (policy group)** — HBAC/sudo/pwpolicy rules target *this*; it **contains** the `role-*`
  group (`ug-x` carries `group: [role-x]`).

A user in `role-x` is an **indirect** member of `ug-x`, so every rule pointing at `ug-x` applies.
(The reverse nesting does not work.)

### The thin RBAC overlay (optional, built-in)

Instead of hand-adding a person to dozens of granular policy groups, assign them an abstract
**role**. The overlay generates **only** role groups, their nesting into **existing** `ug-*`
policy groups, and user→role-group membership — nothing else. It is compiled **by the role
itself** (`tasks/rbac.yml`, inside the desired phase): declare the two `freeipa_server_rbac_*`
vars in group_vars and run the role — no playbook `pre_task` needed. With nothing declared it
no-ops, so a pure-baseline realm runs untouched, and because it compiles **after** the tenant
load it composes with `freeipa_idam_tenants_dir`. (Compiled objects are merged via `set_fact`,
so supplying `freeipa_idam_users`/`_usergroups` as extra-vars would bypass the overlay — use
group_vars or tenant files.)

**Tenancy is a hard boundary**: a role is *defined* inside its `tenant → environment` cell and
*assigned* by naming that exact cell, so one grant can never reach another tenant/environment.

```yaml
# DEFINITIONS — tenant → environment → role → {description?, policy_groups}
# A policy_groups entry is either a LITERAL existing group name — paste it straight
# from the --tags export snapshot, zero renaming — or a {service, privilege} dict
# expanded through the naming template. Both point at EXISTING native groups.
freeipa_server_rbac_roles:
  acme:
    prod:
      platform-admin:
        policy_groups:
          - app-gitlab-admins                         # literal, exactly as exported
          - { service: docker, privilege: operators } # templated → ug-acme-prod-docker-operators
# ASSIGNMENTS — user → tenant → environment → [roles]  (every grant fully qualified)
freeipa_server_rbac_user_assignments:
  alice: { acme: { prod: [platform-admin] } }   # → role-acme-prod-platform-admin → indirect ug-*
```

`ug-*` groups **must already exist natively** (the overlay only nests onto them). Names come from
`freeipa_server_rbac_naming`. `freeipa_rbac_validate` fails fast — naming the culprit — on an
assignment to an undefined `(tenant, environment, role)` cell (this enforces isolation), a missing
policy group, a built-in collision, or a user not in `freeipa_idam_users`. A runnable
3-tenant × 3-environment example ships under `examples/rbac-overlay/`.

Don't confuse the overlay with **`freeipa_idam_roles`** (a flat bundle of groups added *directly*
to a user — no role group, no nesting) or native **`freeipa_idam_iparoles`** (delegation of
IPA-management *privileges*).

### Additive by default; one switch that prunes

Creation is *additive* — `state: present` never deletes. **`freeipa_server_authoritative`**
(default `false`) is the single switch governing all **soft** pruning:

| Mechanism (when `true`) | Removes | Scoped by |
|---|---|---|
| Membership reconcile | members no longer declared in a managed group | the declared, non-protected groups |
| Group-existence reconcile | groups dropped from `freeipa_idam_usergroups` | container marker (`idam-managed-groups`) |
| Object reconcile | orphaned `ug-`/`hg-`/`hbac-`/`sudo-`/automember objects | name substring `freeipa_idam_reconcile_scope` (blank ⇒ nothing) |

Removed **users** are archived (preserved, recoverable). **Authoritative is realm-scoped** — only
run it against a *complete* assembled desired state, never a partial tenant file, or it prunes the
other tenants.

**`freeipa_idam_reconcile_memberships_only`** (default `false`) is the **safe nightly drift-revoke
mode**: runs the membership reconcile (strips members no longer declared) but **suppresses every
deletion**. It enables the strip on its own (no `authoritative` needed) and, removing nothing, is
safe to run per-tenant against a partial file — the right mode for a nightly cron.

> **Scope boundary:** object reconcile manages only `group`/`hostgroup`/`hbacrule`/`sudorule`/
> automember. Leaf building blocks (`hbacsvc`, `sudocmd`, `permission`, `privilege`, `iparole`,
> `pwpolicy`) are left orphaned when undeclared, never auto-deleted — revoke with explicit
> `state: absent`.

### Account types & state controls

`freeipa_idam_service_accounts` (forced nologin), `_breakglass_accounts` (login-on, auto-protected),
`_nologin_accounts` / `_disabled_accounts` (with admin-lockout guards), `_default_user_password`,
`_group_gids` (deterministic GIDs), `_hbac_rules_disable` (guarded), `_reactivate_preserved`
(undelete a re-declared archived user). The `idam` phase is fully idempotent.

### Reference-integrity validation

Before any change, the role validates the whole data set and reports *all* problems at once.
Shape/typo errors (missing `name`, a user with no groups/roles, a duplicate user) **always
hard-fail**. Cross-object *reference* checks (role→group, user→group/role, hbac→service,
sudo→command) are governed by **`freeipa_server_idam_reference_validation`**:

| Mode | Behaviour |
|---|---|
| `strict` (default) | a reference must be declared here, or be a known built-in |
| `warn` | unknown references are reported, the run continues |
| `off` | reference checks skipped |
| `live` | also accept any reference already on the realm (`ipa *-find`; not usable under `--check`) |

Built-ins (`freeipa_server_idam_builtin_groups`) are always valid targets, so a tenant slice
never has to redeclare them just to validate.

## Destructive operations

Two cases, by increasing severity. **Soft = prune** (recoverable), **hard = delete**
(irrecoverable) — and hard-delete is one gate, set once.

| Case | Gate | What it does |
|---|---|---|
| **Prune** (soft) | `freeipa_server_authoritative` | Reconcile: archive undeclared users (recoverable) + delete undeclared objects. See above. |
| **Delete** (hard) | `freeipa_idam_delete` | Irrecoverable `ipa *-del`. **ONE** gate for all hard-deletes; default off; MOCK/lab realms only. |

With `freeipa_idam_delete: true`, the `never`-tag picks *which* hard-delete runs — you do **not**
set a second boolean per operation. Add `--check` to make any of them a read-only dry-run.

```bash
# Hard-delete every object DECLARED in this run's freeipa_idam_* lists (protected excluded)
ansible-playbook ... --tags delete

# Hard-delete ORPHANED preserved users the reconcile archived but no longer declares
# (which --tags delete can't reach). --check first to see the plan.
ansible-playbook ... --tags prune_preserved --check    # dry-run
ansible-playbook ... --tags prune_preserved            # apply
```

Scope selectors for `--tags prune_preserved` (shield preserved logins from the sweep — not gates):
`freeipa_idam_prune_preserved_keep` (explicit list, on top of `freeipa_idam_protected_users`) and
`freeipa_idam_prune_preserved_keep_regex` (default `^svc-` shields service accounts; `""` = none).

## Adopt an existing instance (config export / snapshot)

Snapshot a live FreeIPA into this role's declarative contract, then reapply — no green-field
rebuild. **Read-only** (`*_find`/`*_show` via on-server `ipalib`), opt-in behind the `export` tag.
Minimum: an inventory with the IPA host in `freeipa`, and **one** credential source.

```bash
# Option A — no Vault: pass the admin password directly.
# (Best from an Ansible-Vault file: -e @secrets.yml, so it isn't in shell history.)
ansible-playbook -i inventories/<env>/hosts.yml playbooks/freeipa_idam.yml \
  --tags export -e freeipa_server_admin_password='<ADMIN_PASSWORD>'

# Option B — fall back to Vault (set freeipa_server_vault_secret in group_vars):
ansible-playbook -i inventories/<env>/hosts.yml playbooks/freeipa_idam.yml --tags export

# Either way → writes freeipa.config.snapshot.yml on the control node; move it into an
# inventory group_vars to reapply.
```

Captures realm/domain, users, groups (+ nesting + member-managers), hostgroups, custom HBAC
services, HBAC rules, sudo commands & rules, password policies, and automember rules into
`freeipa_idam_*` / `freeipa_server_*` — drop-in and idempotent, **including onto a fresh, empty
server** (users and custom HBAC services are created before the rules that reference them, so no
first-run ordering race).

**Not** captured by default (each has an opt-in): POSIX group GIDs
(`freeipa_server_export_include_gids=true` to pin them for a same-realm DR rebuild), hostgroup
host rosters (`freeipa_server_export_include_host_membership=true`; enrolment + automember
normally repopulate them), SSH keys (`freeipa_server_export_include_sshkeys=true`), and the
stock HBAC service groups Sudo/ftp (`freeipa_server_export_stock_hbacsvcgroups=true` — they ship
on every fresh server, so only export them if their membership was customised). Never captured:
user passwords / Kerberos keys (unreadable), user UIDs (IPA reassigns — avoids collisions),
FreeIPA's own `global_policy`. If a section could not be captured (unavailable plugin), the
snapshot header carries a loud `# SKIPPED :` line — an empty section ≠ an empty realm.

## See also

- [`freeipa_client`](../freeipa_client/) — host enrolment
- [`hashicorp_vault`](../hashicorp_vault/) — credential source
- A runnable 3-tenant × 3-environment RBAC-overlay template under `examples/rbac-overlay/`.
