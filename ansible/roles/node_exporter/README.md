# node_exporter

## TL;DR

Installs and runs Prometheus Node Exporter as a systemd service, exposing
system metrics for Prometheus to scrape. The release tarball is downloaded
once on the controller and pushed to hosts, so a fleet-wide run makes a
single GitHub request rather than one per host.

```bash
ansible-playbook -i inventories/<env>/hosts.yml playbooks/<playbook>.yml --limit <host>
```

## Requirements

Install collections before running (repo `requirements.yml`, or ad-hoc):

    ansible-galaxy collection install -r requirements.yml

| Collection | When | Used for |
|---|---|---|
| `ansible.posix` | When RHEL family | opens `node_exporter_port/tcp` via firewalld |
| `community.general` | When Debian family | opens `node_exporter_port/tcp` via ufw |

## Key variables

Full list: `vars/main.yml` (`defaults/main.yml` is empty — these are pinned
constants, not per-inventory overrides). No `meta/argument_specs.yml`.

| Req | Variable | Default | Purpose |
|---|---|---|---|
| Optional | `node_exporter_version` | `1.10.2` | Pinned release version; bump to upgrade |
| Optional | `node_exporter_user` | `node_exporter` | System user the service runs as |
| Optional | `node_exporter_group` | `node_exporter` | System group |
| Optional | `node_exporter_port` | `9100` | Listener port (opened in firewalld/ufw) |
| Optional | `node_exporter_install_dir` | `/usr/local/bin` | Binary destination |
| Optional | `node_exporter_textfile_dir` | `/var/lib/node_exporter/textfile_collector` | Textfile collector directory |
| Optional | `node_exporter_arch_map` | `{x86_64: amd64, aarch64: arm64, armv7l: armv7}` | Maps `ansible_architecture` to the release asset arch string |

If `node_exporter_version` is left unset, the role queries the GitHub
releases API once (`run_once`, controller-only) to resolve the latest tag —
pin the version to avoid that call on fleet-wide runs.

## Usage

```yaml
- name: Install Prometheus Node Exporter
  hosts: <group>
  become: true
  roles:
    - role: node_exporter
```

Run it:

```bash
ansible-playbook -i inventories/<env>/hosts.yml playbooks/<playbook>.yml --limit <host>
```

## Behaviour

- Opens `node_exporter_port/tcp` automatically — firewalld on RHEL family,
  `ufw` on Debian family. No variable disables this; it follows the host's
  detected OS family.
