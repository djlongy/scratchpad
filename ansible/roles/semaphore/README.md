# semaphore

## TL;DR

Installs and configures Semaphore UI Community Edition via `docker compose` +
systemd, with **optional** LDAP, FreeIPA provision, and declarative project
reconcile. Privilege is self-contained — no play-level `become`.

**External dependencies are off by default.** Opt in only when they exist.

```bash
ansible-playbook -i inventories/example/hosts.yml playbooks/apps_container.yml --tags semaphore
```

## Requirements

Install collections before running (repo `requirements.yml`, or ad-hoc):

    ansible-galaxy collection install -r requirements.yml

| Collection | When | Used for |
|---|---|---|
| `ansible.posix` | always | firewalld ports |
| `community.general` | always | local git_config for the smoke repo |
| `community.hashi_vault` | `ansible_secret_store=vault_kv` (or FreeIPA admin via Vault) | Ansible deploy secret I/O |
| `freeipa.ansible_freeipa` | `freeipa_provision=true` | provision `svc-semaphore` |

## Key variables

Full list: `defaults/main.yml`. Contract: `meta/argument_specs.yml`.

**Required** = value must be correct for a successful run (defaults often work).
**Optional** = safe to leave default / empty; phase stays off or uses built-ins.
**When X** = required only if that feature is on.

| Req | Variable | Default | Purpose |
|---|---|---|---|
| **Required** | `semaphore_fqdn` | `""` | Public FQDN (TLS terminates upstream) |
| Optional | `semaphore_dns_servers` | `[]` | Container DNS resolvers (empty = host resolv.conf) |
| Optional | `semaphore_ansible_secret_store` | `host_file` | Where **Ansible** persists install secrets (see below) |
| Optional | `semaphore_ldap_enabled` | `false` | Generic LDAP auth in Semaphore |
| When LDAP | `semaphore_ldap_host` | `""` | LDAPS hostname (required; no FreeIPA assumption) |
| When LDAP | `semaphore_ldap_admins` | `[]` | LDAP users promoted to Semaphore admin |
| Optional | `semaphore_freeipa_provision` | `false` | Auto-create svc-semaphore + slurp FreeIPA CA |
| When FreeIPA | `semaphore_freeipa_admin_password` | `""` | FreeIPA admin password (else Vault freeipa/runtime) |
| Optional | `semaphore_projects` | `[]` | Declarative project/template definitions |

### Ansible secret store vs Semaphore secret storage

These are **different things**. Do not confuse them.

| Concern | Owner | This role |
|---|---|---|
| Admin / DB / encryption passwords for first boot | **Ansible** (this role) | `semaphore_ansible_secret_store` |
| How Semaphore stores *credentials it manages* (keys, repos, etc.) | **Semaphore app** | Out of scope — not configured here |

`semaphore_ansible_secret_store` only answers: *where does the playbook remember
the install secrets it generates?* Values are then written into compose as
`SEMAPHORE_ADMIN_PASSWORD`, `SEMAPHORE_DB_PASS`, etc. The container does **not**
read them back from Vault or a host file at runtime.

| Value | Default | Behaviour |
|---|---|---|
| `host_file` | yes | Persist under `{{ semaphore_data_dir }}/secrets/runtime.yml` (mode 0600) |
| `vault_kv` | no | Fetch-or-generate into HashiCorp KV `apps/semaphore/runtime` (estate `vault_*` + control-node token) |

Resolution lives in `tasks/resolve_ansible_secrets.yml` (not named `secrets.yml` —
that pattern is gitignored for real secret files).

Semaphore itself can use HashiCorp Vault as **its own** secret backend; that is a
product setting this role does not wire up.

### LDAP vs FreeIPA

- `semaphore_ldap_enabled` only wires Semaphore LDAP env vars.
- `semaphore_freeipa_provision` is a **separate** opt-in that talks to FreeIPA
  (`groups[semaphore_freeipa_inventory_group]`, default `freeipa`).
- Without freeipa provision you can still point LDAP at any directory: set
  host/bind DN, and optionally `semaphore_ldap_ca_cert_src` for LDAPS trust.

## Minimum configuration

```yaml
# group_vars/semaphore_hosts.yml — bare deploy, no Vault / no FreeIPA
---
semaphore_fqdn: service.example.internal
# ansible_secret_store defaults to host_file; LDAP/FreeIPA stay off
```

### Estate with Vault + FreeIPA (mgt example)

```yaml
semaphore_fqdn: "semaphore.{{ env }}.{{ domain }}"
semaphore_ansible_secret_store: vault_kv
semaphore_ldap_enabled: true
semaphore_ldap_host: "freeipa-01.{{ env }}.{{ domain }}"
semaphore_freeipa_provision: true
semaphore_ldap_admins:
  - username: "operator"
    name: "Operator"
    email: "operator@{{ env }}.{{ domain }}"
```

## Usage

```yaml
- name: Deploy Semaphore
  hosts: semaphore
  # No play-level become — the role escalates per task.
  roles:
    - role: semaphore
      tags: [semaphore]
```

Run:

```bash
export ANSIBLE_VAULT_PASSWORD=$(cat ~/secrets/vault-password.txt)
ansible-playbook -i inventories/<env>/hosts.yml playbooks/apps_container.yml --tags semaphore
```

## Preconditions

- Docker Engine already on the host (this playbook's prepare play runs
  `storage` → `baseline` → `docker` first).
- When `ansible_secret_store=vault_kv`: reachable Vault + estate `vault_*` vars
  and a control-node token (`vault_token_file`).
- When `freeipa_provision`: FreeIPA inventory group, admin password source,
  and `semaphore_ldap_enabled: true`.

## Behaviour

- Root work (packages, dirs, compose, systemd, firewalld) uses task-level
  `become: true`. Vault lookups and REST API calls use `become: false`.
- Install secrets are fetch-or-generate and persisted; re-runs do not rotate them.
- Smoke test is a non-destructive `ansible.builtin.ping` against localhost.
- Empty `semaphore_projects` is a no-op for reconcile (does not abort the play).

## Out of scope

- Does not create DNS records or the reverse-proxy TLS vhost.
- Does not install Docker Engine.
- Does not delete projects removed from inventory (additive reconcile only).
- Does not enroll the host as a FreeIPA client (use `freeipa_client` role).
- Does not configure Semaphore's product-level secret storage (Vault/DB/etc.).

## Tag safety

`--tags config` also runs declarative project reconcile. A config-only run can
mutate project/template state when `semaphore_projects` is non-empty. Use
`--tags install` for stack-only without reconcile, or leave
`semaphore_projects: []`.
