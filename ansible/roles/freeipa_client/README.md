# freeipa_client

Portable, multi-OS FreeIPA **client** role. Enrols a host into a FreeIPA realm by
wrapping `freeipa.ansible_freeipa.ipaclient`, and configures the full client-side
integration — CA trust, home directories, SSSD sudo + HBAC enforcement, DNS
self-registration — with **idempotent re-runs** and **effective stale-machine
rejoin** (including realm-cutover re-enrolment).

Pairs with [`freeipa_server`](../freeipa_server/).

## Supported platforms

EL-family (RHEL/Rocky/Alma/CentOS/Fedora) and Debian/Ubuntu — packaging of the join
itself is handled by the upstream `ipaclient` role's per-distro vars. **Preflight's
own tooling install is RedHat-only** (see "Preflight package install" below) — on
Debian/Ubuntu, preflight's `chronyc`/`kinit`/`kdestroy`/`ipa-client-install` health
checks depend on whatever is already present; there is no bundled Debian equivalent
of the preflight package list. This is a known limitation, not a bug.

## Phases (tags)

| Tag | Runs |
|---|---|
| `always` | **FreeIPA-server guard** (runs on every invocation, before all phases) |
| `preflight` | preflight package install (RedHat), hostname/time guards + **stale-enrolment detection & cleanup** |
| `enroll` / `install` | upstream `ipaclient` join (idempotent) + `ipa-certupdate` |
| `dns` | server-side seed of this host's A/PTR record, **on by default** (admin-join only) |
| `configure` | post-join verification + server-side pointers |

## FreeIPA-server guard (always)

Before anything else — on **every** invocation regardless of `--tags` selection —
the role (top of `tasks/main.yml`, tagged `always`) checks whether
`inventory_hostname` is a member of the `freeipa_client_server_group` inventory
group (the FreeIPA **servers** group — defaults to `freeipa`). If it is, the play
prints an explanatory message and ends the host (`meta: end_host`) instead of
running client logic against a server — health-checking or `ipa-client-install
--uninstall`-ing an IPA server by accident is exactly what this guard prevents.
It is tagged `always` (not `preflight`) deliberately: a tag-limited run such as
`--tags configure` would otherwise skip a preflight-tagged guard and still execute
the other phases against a server host.

Set `freeipa_client_on_master: true` only for the deliberate case of
(re)configuring a client integration **on** an IPA server host — this is the
escape hatch that bypasses the guard.

## Preflight package install

`preflight` installs its own tooling (`freeipa_client_packages`, default
`ipa-client`, `krb5-workstation`, `chrony`, `bind-utils`) via `dnf` before any
health checks run, so `chronyc`, `kinit`/`kdestroy`, and `ipa-client-install` are
guaranteed present. This is gated `when: ansible_os_family == 'RedHat'` — on
Debian/Ubuntu no equivalent package list is installed by this role; the upstream
`ipaclient` role handles its own distro packaging during `enroll`, but preflight's
tooling on Debian is the caller's responsibility (documented limitation, not
in scope for this role — see "Supported platforms" above).

## What it does (client-side)

- **Join** — admin-based (Vault) or OTP-based, against `freeipa_client_servers`
  (defaults to the `freeipa` inventory group) or SRV discovery.
- **CA trust** — `/etc/ipa/ca.crt` + system trust, installed automatically on join,
  refreshed afterwards with `ipa-certupdate` so the local NSS/CA trust store picks
  up any CA changes made server-side since the last enrolment.
- **Home dirs** — `freeipa_client_mkhomedir` (oddjob/pam); optional NFS automount
  via `freeipa_client_automount_location`.
- **sudo** — SSSD sudo provider (`freeipa_client_no_sudo: false`); the host runs
  server-defined sudo rules.
- **HBAC** — enforced automatically by SSSD (`access_provider=ipa`).
- **DNS** — with `freeipa_client_enable_dns_updates` (**on by default**) the host
  self-registers its A record on join and SSSD keeps it in sync with the host IP via
  `dyndns_update`; SSHFP is published regardless. Only effective where IPA is authoritative
  for the client's zone. See "DNS record propagation" below.

## DNS record propagation

The client's forward record is managed **only when the realm's integrated DNS is
authoritative for the client's zone** — e.g. a client named `*.ipa.<env>.<domain>`
when IPA owns that zone. For a client in a zone IPA does **not** own (e.g.
`webnode01.example.com` while IPA owns only `ipa.example.com`), `ipa-client-install`
cannot create the record; it must come from site DNS or a server-side
`ipa dnsrecord-add` against an IPA-managed zone.

When IPA *is* authoritative:

- **At join** — with `freeipa_client_enable_dns_updates` (default **true**),
  `ipa-client-install` adds the A record (and PTR if the reverse zone exists) via an
  authenticated nsupdate; with it **false** no A record is created at join (use the explicit
  seed below or site DNS). `freeipa_client_all_ip_addresses: true` registers every NIC IP.
- **Explicit seed at enrol** — `freeipa_client_seed_dns_record` is **on by default**
  (set `false` to disable): it writes the A record (and, with
  `freeipa_client_seed_dns_reverse`, the reverse PTR) server-side via the admin API
  right after join, independent of join-time self-registration and of SSSD dyndns.
  This **guarantees** the record exists even with dyndns off, and **seeds the PTR**
  that `ipa-client-install` skips. SSSD then maintains it from there. Admin-join
  only (needs admin creds) — OTP-joined hosts skip it and rely on
  self-registration. Idempotent and tolerant: if IPA isn't authoritative for the
  zone (`freeipa_client_dns_zone`, default = realm domain), it warns instead of
  failing. The PTR is managed directly in the reverse zone
  (`freeipa_client_dns_reverse_zone`; empty = auto-derive the `/24` zone from the
  host IP — override for non-`/24` reverse delegations). **Tested:** a fresh enrol
  seeds the forward A and reverse PTR, and a re-run is `changed=0`.
- **Following the host IP** — SSSD `dyndns_update` (on with `freeipa_client_enable_dns_updates`)
  re-registers the A record on each SSSD start/reboot and per `dyndns_refresh_interval` (daily).
  A live in-place re-IP isn't picked up until the next restart/refresh — for an immediate fix
  use the next bullet.
- **Forcing an immediate refresh** — `freeipa_client_sync_dns_record: true` compares the live A
  record to the host's current IP and restarts SSSD to push an update only on a mismatch (needs
  `freeipa_client_enable_dns_updates`). Idempotent; converges once IPA's bind-dyndb-ldap refresh
  lag catches up.

## Stale-machine rejoin (idempotency model)

`preflight` reads `/etc/ipa/default.conf`, compares its realm to the target, and
runs `kinit -k host/<fqdn>` to prove the host keytab still authenticates:

- **Healthy + correct realm** → nothing happens; `enroll` no-ops (idempotent).
- **Broken keytab / host deleted server-side / realm mismatch** → `ipa-client-install
  --uninstall` then a clean re-join (with `force_join`). This is also how you
  re-enrol clients during a **realm cutover** (old realm → new realm): point
  `freeipa_client_domain`/`_realm` at the new realm and re-run.
- `freeipa_client_force_rejoin: true` forces uninstall + re-join unconditionally.

A re-run on a healthy client makes **no changes**.

## Required / common variables

| Variable | Example | Purpose |
|---|---|---|
| `domain` | `example.com` | base domain (`group_vars/all.yml`); realm derives as upper-case |
| `freeipa_client_admin_password` | `<ipa-admin-password>` | admin password (admin-join) — set directly (e.g. from an ansible-vault var) for a HashiCorp-free deployment |
| `freeipa_client_vault_secret` | `kv/data/platform/freeipa/runtime` | HashiCorp Vault path for the admin password — **fallback only**, used when `freeipa_client_admin_password` is empty |
| `freeipa_client_use_otp` + `freeipa_client_otp` | `true` / `<otp>` | OTP-join instead of admin |
| `freeipa_client_servers` | `[idm01.example.com]` | servers to enrol against (else SRV) |
| `freeipa_client_server_group` | `idm` | inventory group of FreeIPA **servers** — used to build `freeipa_client_servers` and as the group the preflight guard checks `inventory_hostname` against |
| `freeipa_client_on_master` | `true` | escape hatch for the preflight server-group guard (see "FreeIPA-server guard" above) |

See `defaults/main.yml` for the full surface (NTP, SSH/SSHFP, automount, subid,
DNS resolver, kinit attempts, `freeipa_client_packages`, …).

## Server-side config (NOT in this role)

A client cannot self-assign these — they need admin and live in `freeipa_server`
IAM (or a host-management play):

- **Host-group membership** → `freeipa_iam_hostgroups` (with host lists)
- **HBAC rules** → `freeipa_iam_hbac_rules` (this client enforces them via SSSD)
- **sudo rules** → `freeipa_iam_sudo_rules` (this client runs them via SSSD)
- **DNS zones/records** beyond the host's own auto-registered A/SSHFP

## Usage

```bash
# Enrol the `freeipa_clients` group (admin-join via Vault)
ansible-playbook -i inventories/<env>/hosts.yml playbooks/freeipa_client.yml

# Re-enrol stale hosts / realm cutover (same command — preflight detects + rejoins)
ansible-playbook -i inventories/<env>/hosts.yml playbooks/freeipa_client.yml

# Force a clean rejoin
ansible-playbook -i inventories/<env>/hosts.yml playbooks/freeipa_client.yml \
  -e freeipa_client_force_rejoin=true
```
