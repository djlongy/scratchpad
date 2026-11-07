# nexus

## TL;DR

Deploy Nexus Repository OSS with Docker Compose, systemd and idempotent repository API configuration.

## Requirements

- `community.hashi_vault` when `nexus_admin_password` is empty.
- Other tasks use `ansible.builtin`.

## Key variables

Full defaults: `defaults/main.yml`. Input contract: `meta/argument_specs.yml`.

| Req | Variable | Default | Purpose |
|---|---|---|---|
| Required | `nexus_repositories` | Public upstream examples | Repository definitions |
| When no password override | `vault_apps_runtime_base` | Unset | Vault API path prefix |
| When Vault lookup is used | `vault_addr` | Unset | Vault server URL |
| Optional | `nexus_admin_password` | Empty | Password supplied through your secret provider |
| Optional | `nexus_repositories[].strict_content_type_validation` | True on creation | Explicit booleans reconcile existing MIME validation |

## Minimum configuration

```yaml
# group_vars/repository_servers.yml
nexus_admin_password: "{{ repository_admin_password }}"
nexus_repositories:
  - name: model-cache
    format: raw
    type: proxy
    strict_content_type_validation: false
    proxy:
      remoteUrl: https://huggingface.co/
```

## Usage

```yaml
# nexus.yml
- hosts: repository_servers
  roles:
    - nexus
```

```bash
ansible-playbook -i inventory.yml nexus.yml
ansible-playbook -i inventory.yml nexus.yml --tags nexus_repos
```

## Preconditions

- Docker, Compose and systemd are installed on the target.
- The administrative password is available through your secret provider.
- A verified database backup exists before updating repository configuration.

## Behaviour

Missing repositories are created. Existing settings are preserved except an explicitly
declared `strict_content_type_validation` boolean, which is reconciled and read back.
Disable MIME detection for opaque binaries only when the consumer verifies pinned
artifact checksums. Repository reads work in check mode; API writes are skipped.

First boot replaces the generated password with your configured password. Compose or
unit changes restart the service. Use `--tags nexus_repos` for repository changes only.
