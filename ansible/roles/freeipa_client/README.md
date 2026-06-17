# freeipa_client

Portable, multi-OS FreeIPA **client** role. Enrols a host into a FreeIPA realm by
wrapping `freeipa.ansible_freeipa.ipaclient`, and configures the full client-side
integration — CA trust, home directories, SSSD sudo + HBAC enforcement, DNS
self-registration — with **idempotent re-runs** and **effective stale-machine
rejoin** (including realm-cutover re-enrolment).

Pairs with [`freeipa_server`](../freeipa_server/).

## Supported platforms

EL-family (RHEL/Rocky/Alma/CentOS/Fedora) and Debian/Ubuntu — packaging is handled
by the upstream `ipaclient` role's per-distro vars.

## Phases (tags)

| Tag | Runs |
|---|---|
| `preflight` | hostname/time guards + **stale-enrolment detection & cleanup** |
| `enroll` / `install` | upstream `ipaclient` join (idempotent) |
| `dns` | opt-in server-side seed of this host's A/PTR record (admin-join only) |
| `configure` | post-join verification + server-side pointers |

## What it does (client-side)

- **Join** — admin-based (Vault) or OTP-based, against `freeipa_client_servers`
  (defaults to the `freeipa` inventory group) or SRV discovery.
- **CA trust** — `/etc/ipa/ca.crt` + system trust, installed automatically on join.
- **Home dirs** — `freeipa_client_mkhomedir` (oddjob/pam); optional NFS automount
  via `freeipa_client_automount_location`.
- **sudo** — SSSD sudo provider (`freeipa_client_no_sudo: false`); the host runs
  server-defined sudo rules.
- **HBAC** — enforced automatically by SSSD (`access_provider=ipa`).
- **DNS** — the host self-registers its A/SSHFP records on join, and (with
  `freeipa_client_enable_dns_updates`) keeps the A record in sync with the host IP
  via SSSD `dyndns_update`. See "DNS record propagation" below.

## DNS record propagation

The client's forward record is managed **only when the realm's integrated DNS is
authoritative for the client's zone** — e.g. a client named `*.ipa.<env>.<domain>`
when IPA owns that zone. For a client in a zone IPA does **not** own (e.g.
`webnode01.example.com` while IPA owns only `ipa.example.com`), `ipa-client-install`
cannot create the record; it must come from site DNS or a server-side
`ipa dnsrecord-add` against an IPA-managed zone.

When IPA *is* authoritative:

- **At join** — `ipa-client-install` adds the A record (and PTR if the reverse
  zone exists). `freeipa_client_all_ip_addresses: true` registers every NIC IP.
- **Explicit seed at enrol** — set `freeipa_client_seed_dns_record: true` to write
  the A record (and, with `freeipa_client_seed_dns_reverse`, the reverse PTR)
  server-side via the admin API right after join, independent of join-time
  self-registration and of SSSD dyndns. This **guarantees** the record exists even
  with dyndns off, and **seeds the PTR** that `ipa-client-install` skips. SSSD then
  maintains it from there. Admin-join only (needs admin creds) — OTP-joined hosts
  skip it and rely on self-registration. Idempotent and tolerant: if IPA isn't
  authoritative for the zone (`freeipa_client_dns_zone`, default = realm domain),
  it warns instead of failing. The PTR is managed directly in the reverse zone
  (`freeipa_client_dns_reverse_zone`; empty = auto-derive the `/24` zone from the
  host IP — override for non-`/24` reverse delegations). **Tested:** a fresh enrol
  seeds the forward A and reverse PTR, and a re-run is `changed=0`.
- **Following the host IP** — set `freeipa_client_enable_dns_updates: true`. This
  turns on SSSD `dyndns_update`, which re-registers the A record (authenticated
  with the host keytab) **on SSSD start (i.e. every boot/`sssd` restart)** and
  periodically per `dyndns_refresh_interval` (default daily). **Tested caveat:** a
  live in-place IP change (e.g. `nmcli` re-IP without a reboot) is **not** picked
  up promptly — SSSD did not push an update within ~100s of the address changing.
  The record catches up at the next SSSD restart/reboot or the next refresh
  interval. So over a reboot cycle the record does follow the host, but if you
  re-IP a running host and need DNS correct immediately, use the next bullet.
- **Forcing an immediate refresh** in an Ansible run (e.g. right after re-IPing a
  host) — `freeipa_client_sync_dns_record: true` checks the live A record against
  the host's current primary IP and restarts SSSD to push an update only on a
  mismatch (requires `freeipa_client_enable_dns_updates`). **Tested:** corrects a
  real drift within ~8s and skips cleanly once in sync. Note IPA's bind-dyndb-ldap
  serves DNS from LDAP with a short refresh lag, so the authoritative answer can
  trail the keytab update by a few seconds; a re-run fired immediately
  back-to-back may briefly re-detect drift and restart SSSD again (harmless — it
  converges). It is idempotent once the DNS layer has caught up.

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
| `freeipa_client_vault_secret` | `kv/data/platform/freeipa/runtime` | admin password path (admin-join) |
| `freeipa_client_use_otp` + `freeipa_client_otp` | `true` / `<otp>` | OTP-join instead of admin |
| `freeipa_client_servers` | `[idm01.example.com]` | servers to enrol against (else SRV) |

See `defaults/main.yml` for the full surface (NTP, SSH/SSHFP, automount, subid,
DNS resolver, kinit attempts, …).

## Server-side config (NOT in this role)

A client cannot self-assign these — they need admin and live in `freeipa_server`
IDAM (or a host-management play):

- **Host-group membership** → `freeipa_idam_hostgroups` (with host lists)
- **HBAC rules** → `freeipa_idam_hbac_rules` (this client enforces them via SSSD)
- **sudo rules** → `freeipa_idam_sudo_rules` (this client runs them via SSSD)
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
