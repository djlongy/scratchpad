# semaphore

## TL;DR

Installs and configures Semaphore UI Community Edition via `docker compose` +
systemd, with optional FreeIPA LDAP and declarative project reconcile against
the REST API. Privilege is self-contained — no play-level `become`.

```bash
ansible-playbook -i inventories/mgt/hosts.yml playbooks/apps_container.yml --tags semaphore
```

## Requirements

Install collections before running (repo `requirements.yml`, or ad-hoc):

    ansible-galaxy collection install -r requirements.yml

| Collection | When | Used for |
|---|---|---|
| `ansible.posix` | always | firewalld ports |
| `community.general` | always | local git_config for the smoke repo |
| `community.hashi_vault` | always | fetch/write self-managed secrets |
| `freeipa.ansible_freeipa` | When LDAP | provision `svc-semaphore` |

## Key variables

Full list: `defaults/main.yml`. Contract: `meta/argument_specs.yml`.

**Required** = value must be correct for a successful run (defaults often work).
**Optional** = safe to leave default / empty; phase stays off or uses built-ins.
**When X** = required only if that feature is on.

| Req | Variable | Default | Purpose |
|---|---|---|---|
| **Required** | `semaphore_fqdn` | `""` | Public FQDN (TLS terminates upstream) |
| Optional | `semaphore_dns_servers` | `[]` | Container DNS resolvers (empty = host resolv.conf) |
| Optional | `semaphore_ldap_enabled` | `false` | FreeIPA LDAP/SSO |
| When LDAP | `semaphore_freeipa_admin_password` | `""` | FreeIPA admin password (else Vault) |
| When LDAP | `semaphore_ldap_admins` | `[]` | LDAP users promoted to Semaphore admin |
| Optional | `semaphore_projects` | `[]` | Declarative project/template definitions |

Admin/db/access-key/LDAP-bind passwords and the CI API token are self-managed
in Vault — not role inputs.

## Minimum configuration

```yaml
# group_vars/semaphore_hosts.yml
---
# Required
semaphore_fqdn: service.example.internal
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
ansible-playbook -i inventories/<env>/hosts.yml playbooks/<playbook>.yml --tags semaphore
```

## Preconditions

- Docker Engine already on the host (this playbook's prepare play runs
  `storage` → `baseline` → `docker` first).
- When LDAP: reachable FreeIPA at `semaphore_ldap_host` and a FreeIPA admin
  password source (declared var or Vault).

## Behaviour

- Root work (packages, dirs, compose, systemd, firewalld) uses task-level
  `become: true`. Vault lookups and REST API calls use `become: false`.
- Secrets are fetch-or-generate into Vault with `read_before_write: true`
  (steady-state no-op). Reruns do not rotate them.
- Smoke test is a non-destructive `ansible.builtin.ping` against localhost.
- Empty `semaphore_projects` is a no-op for reconcile (does not abort the play).

## Out of scope

- Does not create DNS records or the reverse-proxy TLS vhost.
- Does not install Docker Engine.
- Does not delete projects removed from inventory (additive reconcile only).

## Tag safety

`--tags config` also runs declarative project reconcile. A config-only run can
mutate project/template state when `semaphore_projects` is non-empty. Use
`--tags install` for stack-only without reconcile, or leave
`semaphore_projects: []`.
