# harbor

## TL;DR

Installs Harbor from the official offline bundle under a systemd unit with
Trivy scanning, then applies authentication, proxy-cache registries, projects,
an optional outbound webhook and the scheduled scan through the Harbor API.

```bash
ansible-playbook -i inventories/<env>/hosts.yml playbooks/harbor.yml --tags harbor
```

## Requirements

Install collections before running (repo `requirements.yml`, or ad-hoc):

    ansible-galaxy collection install -r requirements.yml

| Collection | When | Used for |
|---|---|---|
| `community.hashi_vault` | When `harbor_webhook_vault_secret` is set | Vault lookup for the webhook secret |

Nothing else beyond `ansible.builtin`.

## Key variables

Full list: `defaults/main.yml`. Contract: `meta/argument_specs.yml`.

**Required** = value must be correct for a successful run.
**Optional** = safe to leave default / empty.
**When X** = required only if that feature is on.

| Req | Variable | Default | Purpose |
|---|---|---|---|
| **Required** | `harbor_admin_password` | `""` | Harbor admin password |
| **Required** | `harbor_db_password` | `""` | Harbor PostgreSQL password |
| **Required** | `harbor_hostname` | `""` | FQDN clients use; empty falls back to `inventory_hostname` |
| Optional | `harbor_container_engine` | `docker` | `docker` or `podman` |
| Optional | `harbor_installer_source` | `url` | `url`, `controller` or `remote` |
| Optional | `harbor_installer_checksum` | `""` | `<algo>:<hex>`, checked for every source |
| Optional | `harbor_version` | `v2.14.2` | Release to install |
| Optional | `harbor_data_dir` | `/opt/harbor` | Install root |
| Optional | `harbor_tls_mode` | `none` | `none` (TLS upstream) or `provided` |
| Optional | `harbor_auth_mode` | `db_auth` | `db_auth` or `oidc_auth` |
| Optional | `harbor_local_projects` | `dev`, `prod`, `shared` | Locally hosted projects |
| Optional | `harbor_registry_endpoints` | see defaults | Upstream proxy caches |
| Optional | `harbor_backup_enabled` | `true` | Nightly database + config backup |
| When TLS | `harbor_tls_certificate` | `""` | Certificate path on the target |
| When TLS | `harbor_tls_private_key` | `""` | Key path on the target |
| When OIDC | `harbor_oidc_name` | `""` | Name on the login button |
| When OIDC | `harbor_oidc_endpoint` | `""` | Issuer URL |
| When OIDC | `harbor_oidc_client_id` | `""` | Client ID |
| When OIDC | `harbor_oidc_client_secret` | `""` | Client secret |
| When OIDC | `harbor_oidc_admin_group` | `""` | Group granted Harbor admin |
| When OIDC | `harbor_oidc_user_claim` | `""` | Claim carrying the username |
| When webhook | `harbor_webhook_enabled` | `false` | Register the outbound webhook |
| When webhook | `harbor_webhook_url` | `""` | Receiver endpoint |
| When webhook | `harbor_webhook_secret` | `""` | Bearer token, or use the Vault fallback |
| When air-gapped | `harbor_installer_local_path` | `""` | Bundle path on the controller, for `source: controller` |

## Minimum configuration

```yaml
# group_vars/harbor.yml
---
# Required
harbor_hostname: registry.example.internal
harbor_admin_password: "{{ vault_harbor_admin_password }}"
harbor_db_password: "{{ vault_harbor_db_password }}"

# When OIDC (only because this example enables it)
harbor_auth_mode: oidc_auth
harbor_oidc_name: "Single Sign-On"
harbor_oidc_endpoint: "https://sso.example.internal/realms/main"
harbor_oidc_client_id: harbor
harbor_oidc_client_secret: "{{ vault_harbor_oidc_client_secret }}"
harbor_oidc_admin_group: registry-admins
```

## Usage

```yaml
- name: Deploy Harbor
  hosts: harbor
  roles:
    - role: harbor
      tags: [harbor]
```

Run:

```bash
ansible-playbook -i inventories/<env>/hosts.yml playbooks/harbor.yml --tags harbor
```

Secrets are read from inventory; supply them however the surrounding repo
does (an encrypted vars file, or a lookup against a secret store).

## Air-gapped installs

The offline bundle contains every container image, so once it reaches the target
nothing else needs egress. Set `harbor_installer_source` to `controller` (copied
from the Ansible host) or `remote` (staged by other means); either way
`harbor_installer_checksum` is verified on the target.

```yaml
harbor_installer_source: controller
harbor_installer_local_path: /srv/artifacts/harbor-offline-installer-v2.14.2.tgz
harbor_installer_checksum: "sha256:<hex>"

# Pull-through caches need upstream registries that do not exist air-gapped.
harbor_registry_endpoints: []

# Trivy cannot refresh its database without egress.
harbor_trivy_config:
  skip_update: true
  skip_java_db_update: true
  offline_scan: true
```

An OIDC provider on the local network still works; only internet egress is absent.

## Preconditions

- A container engine with a working compose implementation is installed. On
  Podman that means `podman-docker`, a `docker-compose` binary and an enabled
  `podman.socket`: Harbor's prepare step shells out to `docker run`.
- `harbor_installer_cache_dir` has room for the offline installer, which is
  several hundred megabytes.
- When `harbor_auth_mode` is `oidc_auth`, the OIDC client already exists at the
  provider with `<external_url>/c/oidc/callback` registered as a redirect URI,
  and the groups claim is included in the issued token.

## Behaviour

- Authentication is applied through the configurations API, not `harbor.yml`.
  Upstream `harbor.yml` has no `auth_mode` or `oidc_*` keys, so values written
  there are discarded silently.
- Harbor rejects an `auth_mode` change once local users other than admin exist;
  the verify phase fails rather than reporting a mode that was not applied.
- On Podman the generated compose file's log driver is retargeted after every
  `prepare`, because Harbor emits Docker's `syslog` driver, which Podman does not
  implement. The rewritten file is validated with `compose config`.
- On Podman the container networks are reloaded before the health gate. A
  firewalld reload discards published-port rules while containers keep reporting
  healthy, so the host side of every port stops answering silently.
- Harbor's `prepare` step re-renders the runtime configuration only when
  `harbor.yml`, the payload version or `harbor_force_reinstall` changes; it is
  followed by a stack restart.
- Upstream `install.sh` is not used. Its `docker --version` check rejects the
  `podman-docker` shim outright, and its teardown guard always evaluates true,
  so it runs `compose down -v` on every invocation.
- The OIDC client secret is never returned by the API, so drift detection cannot
  see it. Set `harbor_oidc_rotate_secret` for one run to push a new one.
- Project metadata and webhook policies are reconciled on every run; values
  changed outside Ansible are overwritten.
- Projects and registries are created but never deleted. Removing an entry from
  inventory leaves the live object in place.

## Out of scope

- Does not install or configure the container engine.
- Does not create DNS records, issue certificates, or configure a reverse proxy.
- Does not create the OIDC client at the identity provider.
- Does not manage robot accounts, project members or replication rules.
- Does not restore backups; the backup script only produces them.

## Expected result

- `harbor.service` is enabled and active.
- The API reports every component healthy.
- Every declared project and upstream registry exists, and when enabled, each
  project carries the webhook policy.
- Verify: `--tags verify` re-reads all of the above and asserts it.

## Tag safety

`--tags auth`, `--tags bootstrap` and `--tags webhooks` reach the API phases
without the install phase, so configuration changes apply without restarting the
registry. They require Harbor to be already installed and running.
