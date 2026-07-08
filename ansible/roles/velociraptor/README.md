# velociraptor

Installs the [Velociraptor](https://www.rapid7.com/products/velociraptor/) DFIR
endpoint client on Linux hosts as a systemd service.

This role is a **generic, defaults-driven engine**.  It handles idempotent binary
download, version-pinned install with a stable symlink, secure client config
deployment, and systemd lifecycle management.  It does **not** ship an
organisation-specific server URL or enrollment config — you must supply those via
variables (see _Minimal configuration_ below).

---

## TL;DR

**Most common: push a client-config update.** Update the vaulted `velociraptor_client_config`, then re-run scoped to `--tags configure` (a full no-tag run does install → configure → service).

```bash
ansible-playbook -i inventories/<env>/hosts.yml playbooks/security/velociraptor_client.yml --tags configure
```

---

## Requirements

- Ansible 2.14+
- `ansible.builtin`, `ansible.posix`, `community.general` collections
- systemd on the target host
- Internet access to GitHub (or an internal mirror via `velociraptor_binary_url`)

---

## Variable reference

| Variable | Default | Description |
|---|---|---|
| `velociraptor_binary_url` | `""` | Explicit download URL.  Takes priority over the constructed URL.  Set this for air-gapped environments or internal mirrors. |
| `velociraptor_version` | `""` | Semver without leading `v` (e.g. `"0.72.4"`).  Used to construct the GitHub URL and version-stamp the binary filename.  Required when `velociraptor_binary_url` is empty. |
| `velociraptor_download_base` | `"https://github.com/Velocidex/velociraptor/releases/download"` | Base URL for constructed downloads.  Override to point at an internal proxy. |
| `velociraptor_binary_checksum` | `""` | Optional checksum for `get_url` (e.g. `"sha256:abc123..."`).  When empty, checksum verification is skipped. |
| `velociraptor_install_dir` | `/opt/velociraptor` | Directory for the versioned binary and stable `velociraptor` symlink. |
| `velociraptor_config_dir` | `/etc/velociraptor` | Directory for `client.config.yaml` (mode `0700`; config `0600`). |
| `velociraptor_service_name` | `velociraptor_client` | systemd unit name (without `.service`). |
| `velociraptor_client_config` | `""` | Inline YAML content for `client.config.yaml`.  Store as an Ansible Vault secret.  Takes priority over `velociraptor_client_config_src`. |
| `velociraptor_client_config_src` | `""` | Absolute path to the config file on the Ansible controller.  Used when `velociraptor_client_config` is empty. |

### Architecture mapping (`vars/main.yml`)

| `ansible_architecture` | Velociraptor arch |
|---|---|
| `x86_64` | `amd64` |
| `aarch64` | `arm64` |

Override `velociraptor_arch_map` in a play or inventory to add other architectures.

---

## Minimal configuration

At a minimum you must provide a binary source and a client config.  Both should
live in inventory or group_vars, never in the role itself.

```yaml
# inventories/prod/group_vars/linux_endpoints.yml

velociraptor_version: "0.72.4"
velociraptor_binary_checksum: "sha256:<sha256sum from GitHub release page>"

# Client config vaulted — generate with: velociraptor config repack --quiet ...
velociraptor_client_config: !vault |
  $ANSIBLE_VAULT;1.1;AES256
  ...
```

```yaml
# playbooks/security/velociraptor_client.yml
---
- name: Deploy Velociraptor endpoint client
  hosts: linux_endpoints
  become: true
  roles:
    - role: velociraptor
```

### Air-gap / internal mirror example

```yaml
velociraptor_binary_url: "https://artifacts.example.com/velociraptor/velociraptor-0.72.4-linux-amd64"
velociraptor_version: "0.72.4"      # still required to version-stamp the dest filename
velociraptor_binary_checksum: "sha256:<expected>"
velociraptor_client_config_src: "files/velociraptor/client.config.yaml"
```

---

## Tags

| Tag | Phase |
|---|---|
| `install` | Download binary, create symlink |
| `configure` | Deploy `client.config.yaml` |
| `service` | Install systemd unit, enable + start |

Run a subset with `--tags install,configure` etc.

---

## Notes

- **Binary source is required.**  The role will `assert` and fail if neither
  `velociraptor_binary_url` nor `velociraptor_version` is set.
- **Client config is optional at deploy time** (warn-only when missing), but the
  service will not start successfully without one.
- The versioned binary filename (`velociraptor-<version>`) means bumping
  `velociraptor_version` automatically retriggers the download — no cache-busting
  needed.
- `no_log: true` is set on all config-deployment tasks to prevent the config
  content appearing in Ansible output or logs.
