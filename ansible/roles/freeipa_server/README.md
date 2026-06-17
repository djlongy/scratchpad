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
| `hardening` | Opt-in post-install hardening (primary only) |
| `backup` | Borg-readable backup timer (primary only) |
| `idam` (`users`/`groups`/`hbac`/`sudo`) | Declarative IDAM reconciliation (primary only) |

A no-tag run runs everything. Migration is **not** in the default flow — run it
explicitly via `playbooks/freeipa_migrate.yml`.

## Required inventory variables

| Variable | Example | Purpose |
|---|---|---|
| `domain` | `example.com` | Base domain (in `group_vars/all.yml`); realm derives as upper-case |
| `freeipa_server_vault_secret` | `kv/data/platform/freeipa/runtime` | Vault path for `admin_password` + `dm_password` |
| `freeipa_idam_vault_secret` | `kv/data/platform/freeipa/runtime` | Vault path for the IDAM admin password |
| `freeipa_server_forwarders` | `[10.0.0.1]` | Upstream DNS forwarders for the IPA zone |

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
