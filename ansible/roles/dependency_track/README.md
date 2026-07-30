# dependency_track

## TL;DR

Deploys Dependency-Track (API server + frontend + PostgreSQL) as a Docker
Compose stack managed by a systemd unit, with optional OIDC SSO and a nightly
PostgreSQL backup cron.

```bash
ansible-playbook -i inventories/<env>/hosts.yml playbooks/dependency_track.yml --tags install
```

## Requirements

None beyond `ansible.builtin`.

## Key variables

Full list: `defaults/main.yml`. Contract: `meta/argument_specs.yml`.
Public variables use the short `dtrack_*` prefix (estate convention).

**Required** = value must be correct for a successful run.
**Optional** = safe to leave default.
**When OIDC** = required only when `dtrack_oidc_enabled` is true.

| Req | Variable | Default | Purpose |
|---|---|---|---|
| **Required** | `dtrack_pg_password` | — | DB password — supply from Vault |
| **Required** | `dtrack_api_base_url` | — | Public API URL the frontend calls from the browser |
| **When OIDC** | `dtrack_oidc_enabled` | `false` | Turns SSO on for API server and frontend |
| **When OIDC** | `dtrack_oidc_issuer` | `""` | Issuer URL of the IdP application |
| When OIDC | `dtrack_oidc_client_id` | `dependency-track` | Client ID registered with the IdP |
| When OIDC | `dtrack_oidc_teams_claim` | `groups` | Claim carrying team membership |
| Optional | `dtrack_version` | `4.13.6` | API server + frontend image tag |
| Optional | `dtrack_data_dir` | `/opt/dtrack` | Install root |
| Optional | `dtrack_apiserver_port` | `8080` | API server host port |
| Optional | `dtrack_frontend_port` | `8081` | Frontend host port |
| Optional | `dtrack_pg_version` | `16` | Bundled PostgreSQL major version |
| Optional | `dtrack_pg_database` / `dtrack_pg_username` | `dtrack` | DB name / user |
| Optional | `dtrack_readiness_timeout` | `300` | Seconds to wait for the API after a restart |
| Optional | `dtrack_backup_dir` | `/opt/backups/dtrack` | Backup target |
| Optional | `dtrack_backup_retention_days` | `14` | Backup prune age (days) |
| Optional | `dtrack_backup_minute` / `dtrack_backup_hour` | `30` / `2` | Backup cron schedule |

## Minimum configuration

```yaml
# group_vars/dependency_track.yml
---
# Required
dtrack_pg_password: "{{ vault_secret_dtrack_pg_password }}"
dtrack_api_base_url: "https://dtrack-api.example.internal"

# When OIDC (only because this example enables it)
dtrack_oidc_enabled: true
dtrack_oidc_issuer: "https://idp.example.internal/application/o/dependency-track/"
```

## Usage

```yaml
- hosts: dependency_track
  roles:
    - dependency_track
```

```bash
ansible-playbook -i inventories/<env>/hosts.yml playbooks/dependency_track.yml --tags install
ansible-playbook -i inventories/<env>/hosts.yml playbooks/dependency_track.yml --tags backup
```

## Preconditions

- Docker and the Compose plugin must already be installed and running
  (apply the `docker` role first).
- When `dtrack_oidc_enabled` is true, an OIDC application must already exist at
  `dtrack_oidc_issuer` with client ID `dtrack_oidc_client_id`, and its redirect
  URI must accept `/static/oidc-callback.html` on the frontend URL. SSO login
  fails without it, though local application login still works.

## Behaviour

- A change to the compose file or the unit file restarts the whole stack via
  handler — the database goes down with it, so expect brief downtime.
- Every run waits for the API server to answer `/api/version` before finishing.
  First boot runs the schema migration and can take several minutes.
- The compose file holds the database password and is written mode `0600` with
  diff output suppressed.

## Out of scope

- Does not create API keys or teams for SBOM uploaders.
- Does not restore the database; the cron only produces dumps.
- Does not configure a reverse proxy, DNS, or TLS termination.
- Does not create the OIDC application in the identity provider.
