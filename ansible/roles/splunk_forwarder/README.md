# splunk_forwarder

Installs the Splunk Universal Forwarder on Linux hosts as a generic,
defaults-driven engine. This is a **host-installed** role — for Docker Swarm
deployments use the separate `splunk_swarm` role.

## TL;DR

**Most common: re-configure an installed forwarder.** Edit `splunk_forwarder_deployment_server` / `splunk_forwarder_forward_servers`, then run `--tags configure` to rewrite `deploymentclient.conf` / `outputs.conf`; a bare run is a full install → configure → service reconcile.

```bash
ansible-playbook -i inventories/<env>/hosts.yml playbooks/L5_apps/splunk_forwarder.yml --tags configure
```

## What it does

- Installs the UF from a `.tgz` tarball via `ansible.builtin.unarchive`
- Creates a `splunk` system user/group and sets ownership of `splunk_forwarder_home`
- Seeds admin credentials via `user-seed.conf` on first run only (guarded by the
  absence of `etc/passwd`)
- Optionally writes `deploymentclient.conf` (when `splunk_forwarder_deployment_server` is set)
- Optionally writes `outputs.conf` (when `splunk_forwarder_forward_servers` is non-empty)
- Registers the forwarder with systemd via `splunk enable boot-start` (once, guarded
  by the presence of the unit file)
- Ensures the `SplunkForwarder` service is enabled and running

## Variable contract

| Variable | Default | Description |
|---|---|---|
| `splunk_forwarder_package_url` | `""` | **Required.** Full URL of the UF `.tgz` (org mirror or Splunk download). |
| `splunk_forwarder_home` | `/opt/splunkforwarder` | Installation directory (tarball extraction destination). |
| `splunk_forwarder_user` | `splunk` | System user that owns and runs the forwarder. |
| `splunk_forwarder_group` | `splunk` | Primary group for the forwarder user. |
| `splunk_forwarder_admin_user` | `admin` | Splunk admin username written to `user-seed.conf` on first run. |
| `splunk_forwarder_admin_password` | `""` | Splunk admin password for `user-seed.conf`. Supply via Vault lookup. |
| `splunk_forwarder_accept_license` | `true` | Pass `--accept-license` to boot-start and first-start commands. |
| `splunk_forwarder_deployment_server` | `""` | Deployment server address (`"hostname:8089"`). Writes `deploymentclient.conf` when set. |
| `splunk_forwarder_forward_servers` | `[]` | List of indexer TCP targets (`"hostname:9997"`). Writes `outputs.conf` when non-empty. |

## Minimal example

```yaml
# inventories/prod/group_vars/log_shippers.yml
splunk_forwarder_package_url: >-
  https://mirror.example.com/splunk/splunkforwarder-9.3.0-51ccf43db5bd-Linux-x86_64.tgz
splunk_forwarder_deployment_server: "deploy.example.com:8089"
splunk_forwarder_admin_password: >-
  {{ lookup('community.hashi_vault.hashi_vault',
     'secret=kv-prod/data/apps/splunk/runtime:admin_password') }}
```

```yaml
# playbooks/L5_apps/splunk_forwarder.yml
---
- name: Install Splunk Universal Forwarder
  hosts: log_shippers
  become: true
  roles:
    - role: splunk_forwarder
```

## Org responsibilities

The following are **not** supplied by this role and must be provided by the
organisation operating Splunk:

- **Package URL** — the role asserts `splunk_forwarder_package_url` is non-empty
  and fails with a descriptive message if it is not set. Host the tarball on an
  internal mirror or use the official Splunk download URL.
- **Deployment server / indexer addresses** — set `splunk_forwarder_deployment_server`
  and/or `splunk_forwarder_forward_servers` in inventory group_vars.
- **Admin password** — supply `splunk_forwarder_admin_password` via a Vault lookup
  in group_vars or host_vars. Never store in plaintext.

## Tags

| Tag | Runs |
|---|---|
| `install` | User/group, tarball extraction, first-run credential seed |
| `configure` | `deploymentclient.conf`, `outputs.conf` |
| `service` | Systemd boot-start registration, service enable/start |
