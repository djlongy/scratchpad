# splunk_forwarder

## TL;DR

Installs the Splunk Universal Forwarder on Linux hosts as a generic,
defaults-driven, host-installed role.

```bash
ansible-playbook -i inventories/<env>/hosts.yml playbooks/<playbook>.yml --tags configure
```

## Requirements

None beyond `ansible.builtin`.

## Key variables

Full list: `defaults/main.yml`. Contract: `meta/argument_specs.yml`.

**Required** = value must be correct for a successful run (defaults often work).
**Optional** = safe to leave default / empty; phase stays off or uses built-ins.
**When X** = required only if that feature is on.

| Req | Variable | Default | Purpose |
|---|---|---|---|
| **Required** | `splunk_forwarder_package_url` | `""` | Full URL of the UF `.tgz` (org mirror or Splunk download) |
| Optional | `splunk_forwarder_home` | `/opt/splunkforwarder` | Installation directory (tarball extraction destination) |
| Optional | `splunk_forwarder_user` | `splunk` | System user that owns and runs the forwarder |
| Optional | `splunk_forwarder_group` | `splunk` | Primary group for the forwarder user |
| Optional | `splunk_forwarder_admin_user` | `admin` | Admin username written to `user-seed.conf` on first run |
| When first run | `splunk_forwarder_admin_password` | `""` | Admin password for `user-seed.conf`; supply via Vault lookup |
| Optional | `splunk_forwarder_accept_license` | `true` | Pass `--accept-license` to boot-start and first-start commands |
| When deployment client | `splunk_forwarder_deployment_server` | `""` | Deployment server address (`hostname:8089`); writes `deploymentclient.conf` when set |
| When forwarding | `splunk_forwarder_forward_servers` | `[]` | Indexer TCP targets (`hostname:9997`); writes `outputs.conf` when non-empty |

## Minimum configuration

```yaml
# group_vars/splunk_forwarder_hosts.yml
---
# Required
splunk_forwarder_package_url: "https://service.example.internal"
```

## Usage

```yaml
- name: Install Splunk Universal Forwarder
  hosts: splunk_forwarder_hosts
  become: true
  roles:
    - role: splunk_forwarder
```

Run it:

```bash
ansible-playbook -i inventories/<env>/hosts.yml playbooks/<playbook>.yml --tags configure
```

## Preconditions

- `splunk_forwarder_package_url` must point to a reachable `.tgz` — host it
  on an internal mirror or use the official Splunk download.

## Behaviour

- Seeds admin credentials via `user-seed.conf` only on first run, guarded
  by the absence of `etc/passwd` in `splunk_forwarder_home` — reruns don't
  reseed.
- Registers with systemd via `splunk enable boot-start` once, guarded by
  the presence of the generated unit file.
- Writes `deploymentclient.conf` / `outputs.conf` only when the
  corresponding var is set / non-empty.

## Tag safety

A no-tags run is a full idempotent reconcile: `install -> configure ->
service`. Running `--tags configure` or `--tags service` alone on a host
that has never completed `install` fails — the tarball must be extracted
first.
