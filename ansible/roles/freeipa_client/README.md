# freeipa_client

## TL;DR

Enrols a host into a FreeIPA realm by wrapping `freeipa.ansible_freeipa.ipaclient`,
and configures the full client-side integration — CA trust, home directories,
SSSD sudo + HBAC enforcement, DNS self-registration. Idempotent: a stale or
realm-mismatched enrolment is auto-detected and cleanly re-joined.

```bash
ansible-playbook -i inventories/<env>/hosts.yml playbooks/20_iam_freeipa_client.yml
```

Force a clean uninstall + re-join:

```bash
ansible-playbook -i inventories/<env>/hosts.yml playbooks/20_iam_freeipa_client.yml \
  -e freeipa_client_force_rejoin=true
```

## Requirements

Install collections before running (repo `requirements.yml`, or ad-hoc):

    ansible-galaxy collection install -r requirements.yml

| Collection | When | Used for |
|---|---|---|
| `freeipa.ansible_freeipa` | always | wraps `ipaclient` for enrolment, `ipadnsrecord` for DNS self-registration, `ipaservice` for the principals a non-host cert SAN needs |
| `community.hashi_vault` | When admin-join uses the Vault fallback | reads `freeipa_client_vault_secret` when `freeipa_client_admin_password` is empty |
| `community.general` | When resilience phase | `ini_file` for the SSSD offline-cache config |

## Key variables

Full list: `defaults/main.yml`. Contract: `meta/argument_specs.yml`.

**Required** = value must be correct for a successful run (defaults often work).
**Optional** = safe to leave default / empty; phase stays off or uses built-ins.
**When X** = required only if that feature is on.

| Req | Variable | Default | Purpose |
|---|---|---|---|
| **Required** | `freeipa_client_domain` | `{{ domain }}` | FreeIPA realm DNS domain to join |
| **Required** | `freeipa_client_realm` | `{{ domain \| upper }}` | Kerberos realm |
| When admin-join | `freeipa_client_admin_password` | `""` | Admin password (declared var wins over Vault) |
| When admin-join | `freeipa_client_vault_secret` | unset | HashiCorp Vault path — fallback only, used when the password above is empty |
| When OTP-join | `freeipa_client_use_otp` / `freeipa_client_otp` | `false` / `""` | OTP-based join instead of admin |
| Optional | `freeipa_client_server_group` | `freeipa` | Inventory group of FreeIPA servers (builds `freeipa_client_servers`; also the preflight server-guard group) |
| Optional | `freeipa_client_servers` | derived from `freeipa_client_server_group` | Servers to enrol against (else SRV discovery) |
| Optional | `freeipa_client_on_master` | `false` | Escape hatch: bypass the preflight guard to run this role ON an IPA server host |
| Optional | `freeipa_client_rejoin_stale` | `true` | Health-check an existing enrolment and re-join if broken/realm-mismatched |
| Optional | `freeipa_client_force_rejoin` | `false` | Force uninstall + re-join regardless of health |
| Optional | `freeipa_client_mkhomedir` | `true` | Create home directories on first login |
| Optional | `freeipa_client_enable_dns_updates` | `true` | SSSD dyndns self-registration (effective only where IPA is authoritative for the zone) |
| Optional | `freeipa_client_seed_dns_record` | `true` | Server-side seed of the A (+PTR) record at enrol time (admin-join only) |
| Optional | `freeipa_client_offline_resilient` | `true` | Applies offline-resilience guards (SSSD offline-auth cache) so login survives IPA/storage outages |
| Optional | `freeipa_client_no_sudo` | `false` | Disable the SSSD sudo provider |
| Optional | `freeipa_client_service_certs` | `[]` | Multi-SAN FreeIPA certs via certmonger (`--tags certs`) |
| Optional | `freeipa_client_certs_include_host_fqdn` | `true` | Always add host FQDN to every cert SAN set |
| When a cert declares a non-host SAN | `freeipa_client_manage_service_principals` | `true` | Create the Kerberos principal each non-host SAN needs (uses the admin credentials) |
| When a cert declares a non-host SAN | `freeipa_client_cert_service_prefix` | `HTTP` | Kerberos service component for those principals |

### Service certs (multi-SAN)

```yaml
freeipa_client_service_certs:
  - cert_path: /etc/pki/tls/nginx-proxy/fullchain.pem
    key_path:  /etc/pki/tls/nginx-proxy/privkey.pem
    dnsnames:                         # preferred — full SAN set
      - lb-01.mgt.example.internal
      - prometheus.mgt.example.internal
      - splunk.mgt.example.internal
    # dnsname: single-name still works
    # service: HTTP                   # Kerberos service component for the SANs
    # postsave: "/usr/local/sbin/reload-app"
```

Changing `dnsnames` re-issues a full CSR (stop-track → request with every `-D`).
Unchanged set is a no-op; expiry renewal stays with certmonger.
For the LB, prefer `nginx_proxy_freeipa_cert` so SANs are derived from
`nginx_proxy_services` automatically.

#### SANs that are not the host FQDN

FreeIPA does not issue a certificate for a name it cannot tie to a Kerberos
identity. It validates every `dNSName` in the CSR by taking the **requesting**
principal, swapping its hostname for that name, and requiring the result to
exist and to be writable by the host making the request. `HTTP/lb-01` asking for
`portal` is checked as `HTTP/portal`; `host/lb-01` asking for the same name is
checked as `host/portal`, which FreeIPA looks up as a *host object* and refuses
to create as a service.

The host's own FQDN is the exception: the derived principal is the requesting
principal itself, so it always exists. That is why a single-SAN host certificate
has never hit this, and why the failure only appears once a second, non-host
name is added.

**The failure is misleading.** The request is accepted, then:

```text
ipa-getcert request -w …            → rc=3, "non-zero return code"
getcert list:
    status: CA_UNREACHABLE
    ca-error: Server at https://ipa-01.example.internal/ipa/json failed request,
      will retry: 4001 (The service principal for subject alt name
      portal.mgt.example.internal in certificate request does not exist).
```

Nothing in the play output mentions principals, and the request stays parked
until it is stop-tracked. Grep `4001` or `CA_UNREACHABLE` when a multi-SAN
request hangs.

The role closes that itself. For every entry that declares a SAN other than
`freeipa_client_fqdn` it creates the service object the certificate is requested
under, with this host as manager, and attaches `<service>/<san>` as a principal
alias for each extra SAN. `<service>` is `freeipa_client_cert_service_prefix`
(`HTTP`) unless the entry sets `service:` or pins a service `principal:`.

**One service, aliases for the rest** — not one service per SAN. The aliases
share the base service's object, so there is one identity, one keytab and one
`managedby` relationship to keep correct. A service per SAN multiplies objects
that must each be granted to the host separately, and the certificate would
still be issued to only one of them.

- The service component is Kerberos naming only — it does not constrain what
  the certificate is used for. A non-HTTP TLS listener (MinIO, Stroom proxy)
  keeps the `HTTP` default unless something actually does Kerberos against it.
- Only SANs that differ from the requesting principal's own name need anything:
  a SAN equal to it validates against the principal itself.
- A pinned service `principal:` is adopted — its service component and hostname
  name the service object, and this host is still set as the manager.
- **A pinned `host/…` principal is replaced** when the entry declares a non-host
  SAN. Under a host principal FreeIPA resolves each SAN to a *host object* of
  that name and refuses to create one as a service, so the combination cannot
  issue. The certificate is requested under `<service>/<host>` instead; contents
  are unchanged. The run reports the substitution.
- Creating principals needs the admin credentials. Set
  `freeipa_client_manage_service_principals: false` where they are managed
  out-of-band; the run then fails at preflight naming the SAN, rather than at
  `ipa-getcert request -w` with `rc=3`.
- A cert whose only SAN is the host FQDN keeps the host principal and makes no
  server-side call, so it needs no privilege.

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
- name: Enrol host into FreeIPA
  hosts: freeipa_clients
  roles:
    - role: freeipa_client
      tags: [freeipa_client]
```

Run it:

```bash
ansible-playbook -i inventories/<env>/hosts.yml playbooks/20_iam_freeipa_client.yml
```

## Preconditions

- Admin-join via the Vault fallback (`freeipa_client_vault_secret` set,
  `freeipa_client_admin_password` empty) requires the password to already
  exist at that HashiCorp Vault path.
- OTP-join (`freeipa_client_use_otp: true`) requires the OTP to already be
  generated for this host on the FreeIPA server.
- On Debian/Ubuntu, preflight's own tooling install (`freeipa_client_packages`)
  is RedHat-only — its `chronyc`/`kinit`/`ipa-client-install` health checks
  depend on whatever tooling is already present on the host.

## Behaviour

- On every invocation, regardless of `--tags`, the role checks whether
  `inventory_hostname` is a member of `freeipa_client_server_group`. If so it
  ends the host instead of running client logic against a FreeIPA server. Set
  `freeipa_client_on_master: true` to deliberately configure a client on a
  server host.
- `freeipa_client_rejoin_stale` (default `true`) auto-detects a stale or
  realm-mismatched enrolment and re-joins by running `ipa-client-install
  --uninstall` then re-enrolling. `freeipa_client_force_rejoin` forces this
  regardless of detected health.
- `freeipa_client_offline_resilient` (default `true`) rewrites the SSSD cache
  configuration on every run to keep login working during IPA/storage
  outages.

## Out of scope

FreeIPA server-side IAM — a client cannot self-assign these; they live in
`freeipa_server`:

- Host-group membership (`freeipa_iam_hostgroups`)
- HBAC rules (`freeipa_iam_hbac_rules`) — this client enforces them via SSSD
- sudo rules (`freeipa_iam_sudo_rules`) — this client runs them via SSSD
- DNS zones/records beyond the host's own auto-registered A/SSHFP
