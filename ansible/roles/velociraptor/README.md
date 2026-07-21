# velociraptor

## TL;DR

Installs the Velociraptor DFIR endpoint client on Linux hosts as a systemd
service. Set a binary source (`velociraptor_version` or `velociraptor_binary_url`)
and a client config (`velociraptor_client_config`), then run. A no-tag run is a
full idempotent reconcile: install → configure → service.

```bash
ansible-playbook -i inventories/<env>/hosts.yml playbooks/<playbook>.yml --tags velociraptor
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
| When no `_binary_url` | `velociraptor_version` | `""` | Semver without leading `v` (e.g. `"0.72.4"`); builds the download URL and version-stamps the binary filename |
| When no `_version` | `velociraptor_binary_url` | `""` | Explicit download URL; takes priority over the constructed one (air-gap / internal mirror) |
| Optional | `velociraptor_download_base` | `https://github.com/Velocidex/velociraptor/releases/download` | Base URL for constructed downloads |
| Optional | `velociraptor_binary_checksum` | `""` | Checksum for `get_url`, e.g. `sha256:...`; skipped when empty |
| Optional | `velociraptor_install_dir` | `/opt/velociraptor` | Versioned binary + stable `velociraptor` symlink |
| Optional | `velociraptor_config_dir` | `/etc/velociraptor` | Directory for `client.config.yaml` (mode `0700`; config `0600`) |
| Optional | `velociraptor_service_name` | `velociraptor_client` | systemd unit name (also the service account username) |
| Optional | `velociraptor_client_config` | `""` | Inline YAML for `client.config.yaml` (vault it); takes priority over `_src`. Configure phase is skipped when both are empty |
| Optional | `velociraptor_client_config_src` | `""` | Absolute path to the config file on the Ansible controller |

## Usage

```yaml
- name: Deploy Velociraptor endpoint client
  hosts: linux_endpoints
  become: true
  roles:
    - role: velociraptor
```

```yaml
# inventory group_vars
velociraptor_version: "0.72.4"
velociraptor_binary_checksum: "sha256:<sha256sum from GitHub release page>"

# Vaulted — generate with: velociraptor config repack --quiet ...
velociraptor_client_config: !vault |
  $ANSIBLE_VAULT;1.1;AES256
  ...
```

Run it:

```bash
ansible-playbook -i inventories/<env>/hosts.yml playbooks/<playbook>.yml --tags install,configure,service
```

## Behaviour

- Fails fast via an assert if neither `velociraptor_binary_url` nor
  `velociraptor_version` is set.
- A missing `velociraptor_client_config`/`_src` is warn-only at deploy time —
  the service will not start successfully without one.
- The versioned binary filename (`velociraptor-<version>`) means bumping
  `velociraptor_version` retriggers the download.
- All config-deployment tasks set `no_log: true`.
- Only `x86_64` → `amd64` and `aarch64` → `arm64` are mapped by default;
  override `velociraptor_arch_map` in a play or inventory to add others.
