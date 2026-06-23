# freeipa_server

Portable, multi-OS FreeIPA **server** role. Wraps the upstream
`freeipa.ansible_freeipa` `ipaserver`/`ipareplica` roles for install, and layers
cold-start resilience, Borg-readable backup, declarative IDAM reconciliation, an
opt-in post-install hardening baseline, and an **IPA-to-IPA realm migration**
(`ipa-migrate`) — all driven from inventory variables.

> Sanitised reference copy. Replace the example domain/IPs/Vault paths with your
> own. Pairs with a separate `freeipa_client` role for host enrolment (not included).

## Supported platforms

EL-family (RHEL/Rocky/Alma/CentOS/Fedora) and Debian/Ubuntu — the full FreeIPA
*server* support matrix. Packaging and firewall are handled by the upstream roles.

## Requirements

- `freeipa.ansible_freeipa` collection (the role wraps its `ipaserver`/`ipareplica` roles)
- `community.hashi_vault` (credentials are read from HashiCorp Vault)
- `ansible.posix`, `community.general`
- A reachable HashiCorp Vault with the admin/DM passwords stored (see below)

## Quick start

```bash
# Build the realm (primary first, then replica — or all at once on a single host)
ansible-playbook -i inventories/example/hosts.yml playbooks/freeipa.yml

# Re-reconcile declarative IDAM only
ansible-playbook -i inventories/example/hosts.yml playbooks/freeipa.yml --tags idam

# Re-apply just the cold-start resilience config
ansible-playbook -i inventories/example/hosts.yml playbooks/freeipa.yml --tags resilience
```

## Phases (tags)

| Tag | Runs |
|---|---|
| `preflight` | FIPS/time/FQDN/primary-up dependency guards |
| `install` | Vault creds + upstream primary/replica install |
| `configure` / `resilience` | Cold-start timeout override + recovery timer |
| `certs` (`ca`) | Import trusted external CAs into the IPA trust store (primary only, opt-in) |
| `hardening` | Opt-in post-install hardening (primary only) |
| `backup` | Borg-readable backup timer + opt-in controller offload (primary only) |
| `idam` (`users`/`groups`/`hbac`/`sudo`) | Declarative IDAM reconciliation (primary only) |
| `dns` | Declarative DNS zones / forward-zones / records (primary only, opt-in) |
| `automember` | Auto group/hostgroup membership rules (primary only, opt-in) |
| `adtrust` (`trust`) | Active Directory trust(s) (primary only, opt-in) |
| `restore` | Break-glass restore from a backup (`never` tag — explicit opt-in) |

A no-tag run runs everything. Migration is **not** in the default flow — run it
explicitly via `playbooks/freeipa_migrate.yml`.

## Credentials — declared vars first, HashiCorp Vault as fallback

Every secret the role needs resolves in this order: **a declared Ansible var
wins; HashiCorp Vault is only the fallback.** So you can run any part of the role
with the password supplied directly (group_vars, an Ansible-Vault file, or `-e`)
and **no HashiCorp Vault at all** — Vault is not every environment. The Vault
lookup is evaluated lazily, so it never fires when the password is provided.

| Secret | Declared var (wins) | Vault fallback |
|---|---|---|
| IPA admin password | `freeipa_server_admin_password` | `freeipa_server_vault_secret` : `admin_password` |
| Directory Manager password | `freeipa_server_dm_password` | `freeipa_server_vault_secret` : `dm_password` |
| IDAM admin password | `freeipa_server_admin_password` | `freeipa_idam_vault_secret` : `admin_password` |

Provide **one** column. Either set the password var(s), or set the
`*_vault_secret` path(s) — the role asserts at least one source exists.

### Minimum to run each phase

| Phase | Minimum vars |
|---|---|
| `export` (snapshot a live IPA) | `freeipa_server_admin_password` **or** `freeipa_idam_vault_secret` — nothing else |
| `idam` (reconcile identity) | admin password (declared or Vault) + the `freeipa_idam_*` data |
| `install` (build a server) | admin **and** dm password (declared or Vault) + `freeipa_server_forwarders` |

Other inventory variables:

| Variable | Example | Purpose |
|---|---|---|
| `domain` | `example.com` | Base domain (in `group_vars/all.yml`); realm derives as upper-case |
| `freeipa_server_forwarders` | `[10.0.0.1]` | Upstream DNS forwarders for the IPA zone (install) |

Derived automatically (override only to break convention):
`freeipa_server_domain` (`{{ domain }}`), `freeipa_server_realm`
(`{{ domain | upper }}`), `freeipa_server_is_primary` /
`freeipa_server_primary_host` / `freeipa_server_primary_ip` (from the
`freeipa_primary` inventory group + each host's `ansible_host`).

## Single-server vs. cluster

Topology comes from inventory — no code change between sizes:

- **Single server:** put one host in the `freeipa` group. It is the primary; the
  replica path never runs; backup/IDAM/hardening all run on it.
- **Cluster:** add more hosts to `freeipa` (optionally `freeipa_primary` /
  `freeipa_replica` groups). Non-primary hosts enrol as replicas.

`freeipa_server_has_replicas` reflects whether more than one server is defined.

## Install options (selected)

`freeipa_server_ca_mode` (`self-signed`|`external-ca`|`ca-less`),
`freeipa_server_setup_kra`, `freeipa_server_setup_dns`,
`freeipa_server_forward_policy`, `freeipa_server_idstart`/`_idmax`,
`freeipa_server_no_hbac_allow`, `freeipa_server_mkhomedir`,
`freeipa_server_setup_ntp` + `freeipa_server_ntp_servers`,
`freeipa_server_fips_required`. See `defaults/main.yml` for the full surface.

### external-ca is two-phase

`freeipa_server_ca_mode: external-ca` requires a CSR roundtrip: the first run
emits `/root/ipa.csr`, which you sign with your external CA; supply the signed
chain via `freeipa_server_external_cert_files` and re-run. Preflight asserts the
files are present.

> `external-ca` makes the IPA CA itself a **subordinate** of your CA. To instead
> **trust other CAs** (other domains/devices) *alongside* the IPA CA, see below.

## Trusting external CAs (additive to the IPA CA)

`freeipa_server_trusted_external_cas` imports third-party / other-domain CA
certs into the IPA trust store (`ipa-cacert-manage install` + `ipa-certupdate`),
so IPA and its enrolled clients **trust certificates issued by those CAs**. This
is *additive* — it does not change the IPA CA. Primary only; replicates to the
cluster. Idempotent (skips nicknames already present); skipped in `ca-less` mode.

```yaml
freeipa_server_trusted_external_cas:
  - name: corp-root            # nickname in the IPA trust store
    src: files/corp-root.pem   # PEM on the control node
  - name: partner-domain
    content: |                 # …or inline PEM
      -----BEGIN CERTIFICATE-----
      ...
      -----END CERTIFICATE-----
```

Provide the full chain (root, and intermediates if any). Run the `certs` tag to
apply just this step. Auth uses the **Directory Manager** password from
`freeipa_server_vault_secret` (`ipa-cacert-manage`, fed on stdin);
`ipa-certupdate` runs as root via the host keytab.

## Backup restore / offload

`backup.yml` runs `ipa-backup` on a timer; these add the missing paths by
wrapping the upstream `ipabackup` role:

- **Restore (break-glass, DESTRUCTIVE)** — `--tags restore` +
  `-e freeipa_server_restore_name=<backup-name>` (e.g. `ipa-data-2026-06-22-…`).
  Gated behind the `never` tag so it can't run by accident. DM password from Vault.
- **Offload to controller** — set `freeipa_server_backup_fetch_name` (a backup
  name, or `all`); copies to `freeipa_server_backup_controller_path` on the next
  backup run.

## Declarative DNS

When IPA runs integrated DNS, manage zones/records declaratively (primary only,
idempotent):

```yaml
freeipa_server_dns_zones:
  - { name: "internal.example.com", dynamic_update: true, allow_sync_ptr: true }
freeipa_server_dns_forward_zones:
  - { name: "corp.example.com", forwarders: ["10.0.0.53"], forwardpolicy: "first" }
freeipa_server_dns_records:
  - { zone_name: "internal.example.com", name: "app1", record_type: "A", record_value: "10.0.0.20" }
```

## Automember

Auto-assign users/hosts to a group/hostgroup by attribute regex (the target
group must already exist via IDAM):

```yaml
freeipa_server_automember_rules:
  - name: linux-hosts
    automember_type: hostgroup
    inclusive:
      - { key: "fqdn", expression: ".*\\.example\\.com$" }
```

**Solves the enrolment chicken-and-egg.** A workstation must land in the
hostgroup that an HBAC rule grants SSH to, but enrolment is client-side
(`freeipa_client`) and group/HBAC are server-side directory data. Automember
closes the gap **without chaining the client and server plays**: a host's FQDN
(or other attribute) auto-places it in its hostgroup when IPA creates the host
record, so a freshly-enrolled box is immediately reachable by its team. You run
only the client play for a new host.

A newly-enrolling host is placed automatically at host-create — no rebuild
needed. A rebuild only re-evaluates members that *already existed* when a rule
was added or changed (e.g. hosts enrolled before the rule). The role runs that
rebuild automatically when a rule changes this run; force a one-off rebuild
with:

```yaml
freeipa_server_automember_rebuild: true   # default false
```

## Active Directory trust

Trust an AD forest so AD users authenticate against IPA resources. Needs the AD
trust controller and reachable AD DCs + two-way DNS. Opt-in; **shipped as a
capability, not exercised here (no AD).**

```yaml
freeipa_server_setup_adtrust: true          # enables --setup-adtrust at install
freeipa_server_ad_trusts:
  - realm: "AD.EXAMPLE.COM"
    admin: "Administrator"
    password_field: "ad_trust_password"      # field in freeipa_server_vault_secret
    two_way: true
```

## Hardening (opt-in, default off)

`freeipa_server_harden_anonymous_bind` (→ `rootdse`),
`freeipa_server_search_size_limit` / `_search_time_limit`,
`freeipa_server_require_otp_groups`,
`freeipa_server_disable_allow_all` (guarded — refuses unless a replacement HBAC
rule exists), `freeipa_server_crypto_policy` (report-only).

## IDAM data (declarative)

`freeipa_idam_*` lists drive users/groups/hostgroups/HBAC/sudo/password-policy
reconciliation; managed users removed from config are deleted (except
`freeipa_idam_protected_users`).

## Adopt an existing instance (config export / snapshot)

Already have a hand-built FreeIPA you'd rather not rebuild? Snapshot its live
config into this role's declarative contract, then reapply with the role —
no green-field rebuild. The export is **read-only** (only `*_find`/`*_show`
via the on-server `ipalib`) and opt-in behind the `export` tag.

**Minimum to export:** an inventory with the IPA host in the `freeipa` group, and
**one** credential source — just the admin password, no HashiCorp Vault needed:

```bash
# Option A — no HashiCorp Vault: pass the admin password directly.
# (Best from an Ansible-Vault file: -e @secrets.yml, so it isn't in your shell history.)
ansible-playbook -i inventories/example/hosts.yml playbooks/freeipa.yml \
  --tags export \
  -e freeipa_server_admin_password='<ADMIN_PASSWORD>'

# Option B — fall back to HashiCorp Vault (set freeipa_idam_vault_secret in group_vars):
ansible-playbook -i inventories/example/hosts.yml playbooks/freeipa.yml --tags export

# Either way → writes freeipa.config.snapshot.yml on the control node; move it
# into an inventory group_vars (rename to taste) to reapply.
```

It captures users, groups (+ nesting), hostgroups, HBAC rules, sudo commands &
rules, password policies, and automember rules into `freeipa_idam_*` /
`freeipa_server_automember_rules` — the same vars `default/main.yml` documents,
so the output is drop-in and reapplies idempotently.

Deliberately **not** captured: user passwords / Kerberos keys (unreadable),
POSIX uid/gid numbers (IPA reassigns on a rebuild — avoids ID collisions),
hostgroup host rosters (enrolment + automember repopulate them — opt in with
`freeipa_server_export_include_host_membership=true`), and FreeIPA's own
`global_policy`. SSH keys are off by default
(`freeipa_server_export_include_sshkeys=true` to include).

## IPA-to-IPA realm migration

Migrate identities from a source (old) realm into this realm — 100% Ansible,
defaults to **dry-run**:

```bash
# dry-run (no writes) — review the planned counts
ansible-playbook -i inventories/example/hosts.yml playbooks/freeipa_migrate.yml

# apply
ansible-playbook -i inventories/example/hosts.yml playbooks/freeipa_migrate.yml \
  -e freeipa_migrate_dryrun=false
```

It pulls the source bind password + target admin password from Vault, slurps the
source CA cert, `kinit`s, runs `ipa-migrate` (prod-mode), restarts SSSD, and
cleans up staged secrets. Set the source via `freeipa_migrate_source`,
`freeipa_migrate_source_host`, `freeipa_migrate_bind_pw_vault` (see the example
`group_vars/freeipa.yml`). Passwords are **not** migrated (Kerberos keys are
realm-salted) — users re-key via the migration web page, so migration mode is
left ON unless `freeipa_migrate_disable_mode_after=true`.

## Layout

```
freeipa_server/
├── defaults/main.yml      # full freeipa_server_* + freeipa_migrate_* surface
├── vars/main.yml          # derived constants (systemd names, backup paths)
├── meta/{main,argument_specs}.yml
├── tasks/
│   ├── main.yml           # preflight→install→resilience→hardening→backup→idam
│   ├── preflight.yml      # dependency-ordering guards (FIPS/time/FQDN/primary)
│   ├── install.yml primary.yml replica.yml   # upstream ipaserver/ipareplica wrap
│   ├── resilience.yml backup.yml             # cold-start timer + Borg backup
│   ├── hardening.yml      # opt-in post-install hardening
│   ├── idam*.yml          # declarative IDAM reconciliation
│   └── migrate.yml        # ipa-migrate realm migration (via freeipa_migrate.yml)
├── handlers/main.yml
└── templates/
```
