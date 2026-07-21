# freeipa_client

## TL;DR

Enrols a host into a FreeIPA realm (wraps `freeipa.ansible_freeipa.ipaclient`) and configures
client-side integration — CA trust, home directories, SSSD sudo/HBAC, DNS self-registration —
with idempotent re-runs and automatic stale-enrolment rejoin.

```bash
ansible-playbook -i inventories/<env>/hosts.yml playbooks/freeipa_client.yml
```

Force a clean uninstall + re-join:

```bash
ansible-playbook -i inventories/<env>/hosts.yml playbooks/freeipa_client.yml -e freeipa_client_force_rejoin=true
```

## Requirements

Install collections before running (repo `requirements.yml`, or ad-hoc):

    ansible-galaxy collection install -r requirements.yml

| Collection | When | Used for |
|---|---|---|
| `freeipa.ansible_freeipa` | always | `ipaclient` join, `ipadnsrecord` DNS seed |
| `community.general` | always | `ini_file` (post-join config tweaks) |
| `community.hashi_vault` | When admin-join fallback | Vault lookup for the admin password |

## Key variables

Full list: `defaults/main.yml`. Contract: `meta/argument_specs.yml`.

**Required** = value must be correct for a successful run (defaults often work).
**Optional** = safe to leave default / empty; phase stays off or uses built-ins.
**When X** = required only if that feature is on.

| Req | Variable | Default | Purpose |
|---|---|---|---|
| **Required** | `freeipa_client_domain` | `{{ domain }}` | Realm DNS domain to join |
| **Required** | `freeipa_client_realm` | `{{ freeipa_client_domain \| upper }}` | Kerberos realm |
| Optional | `freeipa_client_server_group` | `freeipa` | Inventory group of FreeIPA servers; also the preflight server-guard group |
| Optional | `freeipa_client_servers` | derived from `server_group` | Explicit server FQDNs (else SRV discovery) |
| When admin-join | `freeipa_client_admin_password` | `""` | Admin password (declared var wins; empty falls back to Vault) |
| When admin-join fallback | `freeipa_client_vault_secret` | unset | HashiCorp Vault KV path for the admin password |
| When OTP-join | `freeipa_client_use_otp` / `freeipa_client_otp` | `false` / `""` | One-time-password join instead of admin |
| Optional | `freeipa_client_force_rejoin` | `false` | Force uninstall + re-join regardless of health |
| Optional | `freeipa_client_on_master` | `false` | Escape hatch to run this role on a FreeIPA server host |
| Optional | `freeipa_client_no_sudo` | `false` | Disable the SSSD sudo provider |
| Optional | `freeipa_client_automount_location` | `""` | IPA-managed NFS automount for home dirs (`""` disables) |
| Optional | `freeipa_client_enable_dns_updates` | `true` | SSSD dyndns keeps the A record synced to the host IP |
| Optional | `freeipa_client_seed_dns_record` | `true` | Server-side A(+PTR) seed at enrol time (admin-join only) |
| Optional | `freeipa_client_sync_dns_record` | `false` | Force an immediate A-record refresh on drift |
| Optional | `freeipa_client_service_certs` | `[]` | certmonger-managed certs chained to the IPA CA (`--tags certs`) |
| Optional | `freeipa_client_packages` | `[ipa-client, krb5-workstation, chrony, bind-utils]` | Preflight tooling (RedHat family only) |

## Minimum configuration

```yaml
# group_vars/freeipa_client_hosts.yml
---
# Required
freeipa_client_domain: example.internal
freeipa_client_realm: "REPLACE_ME_freeipa_client_realm"
```

## Usage

```yaml
- name: Enrol FreeIPA clients
  hosts: freeipa_clients
  become: true
  roles:
    - role: freeipa_client
```

Run it:

```bash
ansible-playbook -i inventories/<env>/hosts.yml playbooks/freeipa_client.yml
```

## Preconditions

- `freeipa_client_domain`/`_realm` must already be a reachable FreeIPA realm (SRV
  discovery, or `freeipa_client_servers` set explicitly).
- Admin-join fallback needs a secret already present at `freeipa_client_vault_secret`
  (field `freeipa_client_admin_password_field`) — this role only reads it.
- DNS self-registration and server-side seeding only take effect when the realm's
  integrated DNS is authoritative for the client's zone (e.g. a client in
  `*.ipa.<env>.<domain>` when IPA owns that zone). Outside that zone,
  `ipa-client-install` cannot create the record — this role warns rather than fails,
  and the record must come from site DNS or a server-side `ipa dnsrecord-add`.

## Behaviour

- **FreeIPA-server guard** — on every invocation, regardless of `--tags`, checks
  whether `inventory_hostname` is a member of `freeipa_client_server_group`. If so,
  it prints a message and ends the host (`meta: end_host`) instead of running client
  logic, so the role can't accidentally uninstall or health-check a server. Set
  `freeipa_client_on_master: true` to deliberately run client config on a server host.
- **Stale-machine rejoin** — `preflight` reads `/etc/ipa/default.conf`, compares its
  realm to the target, and runs `kinit -k host/<fqdn>` to prove the host keytab still
  authenticates:

  | Enrolment state | Result |
  |---|---|
  | Healthy + correct realm | No-op; `enroll` is idempotent |
  | Broken keytab / host deleted server-side / realm mismatch | `ipa-client-install --uninstall` then a clean re-join (also how realm cutovers are handled — point `freeipa_client_domain`/`_realm` at the new realm and re-run) |
  | `freeipa_client_force_rejoin: true` | Uninstall + re-join unconditionally |

  A re-run on a healthy client makes no changes.

## Out of scope

- Host-group membership, HBAC rules, sudo rules, and DNS zones/records beyond the
  host's own auto-registered A/SSHFP are server-side operations a client cannot
  self-assign — manage them via server-side IAM or a dedicated host-management play.
- Preflight's own tooling install (`freeipa_client_packages`) is RedHat-only; on
  Debian/Ubuntu, preflight's health checks depend on whatever tooling is already
  present — installing it there is out of scope.
