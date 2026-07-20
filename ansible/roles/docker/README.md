# docker

Install and configure **Docker CE** on EL (RHEL/AlmaLinux 8/9) and Debian/Ubuntu — from the official upstream repo or from any custom mirror (e.g. Artifactory for air-gapped fleets).

**What the role does:**

- Configures the Docker CE package repository (upstream or custom/air-gap)
- Imports/trusts the GPG key (rpm_key on EL; apt signed-by on Debian/Ubuntu)
- Installs Docker CE packages (overridable list + optional version pin)
- Renders `/etc/docker/daemon.json` from variables — only the keys whose variable is set are emitted; no stale defaults
- Ensures `/etc/docker` and the data-root directory exist with correct permissions
- Enables and starts the `docker` systemd service
- Optionally adds users to the `docker` group for unprivileged socket access
- Optionally provisions a dedicated service account (e.g. `svc-docker`), owns the data-root with it, and grants the docker group write ACLs on a shared path (e.g. `/opt`)

## Tagging model

Follows the **refinement** model: a no-tag run executes every phase. Tags narrow scope for fast iteration.

| Tag | Phase |
|---|---|
| `install` | Repo config, GPG key import, package install |
| `configure` | `daemon.json`, data-root directory, service enable/start |
| `users` | Docker-group members, optional service account, optional shared-path ACLs |

## Minimal usage

```yaml
# site.yml / playbook
- hosts: docker_hosts
  roles:
    - docker
```

All defaults work for an internet-connected host. The data-root is `/opt/docker`, logging is `json-file` with 50m/5 rotation.

## Air-gap Artifactory example

Prerequisite: Artifactory has a **remote** Docker CE repo (e.g. `docker-ce-remote`) proxying `download.docker.com`, and a **local** Docker V2 registry (e.g. port 5000 or the `docker-local` virtual repo).

```yaml
# inventories/prod/group_vars/docker_hosts.yml
docker_repo_baseurl: "https://artifactory.example.com/artifactory/docker-ce-remote/linux/centos/{{ ansible_distribution_major_version }}/$basearch/stable"
docker_repo_gpgkey: "https://artifactory.example.com/artifactory/docker-ce-remote/linux/centos/gpg"
docker_repo_sslverify: false        # set true when Artifactory has a valid CA-signed cert

# Tell Docker the local registry is reachable (HTTP or self-signed TLS)
docker_insecure_registries:
  - "artifactory.example.com:5000"

# Pull-through mirror for Docker Hub images
docker_registry_mirrors:
  - "https://artifactory.example.com/artifactory/docker-hub-remote"

docker_data_root: "/opt/docker"
docker_live_restore: true
```

On Debian/Ubuntu the same vars apply — the role picks the correct apt vs. yum path automatically.

## Subnet / insecure-registry daemon.json example

Setting a custom bridge IP, address pool, and an insecure registry:

```yaml
docker_bip: "172.17.0.1/24"
docker_default_address_pools:
  - base: "172.18.0.0/16"
    size: 24
docker_insecure_registries:
  - "artifactory.example.com:5000"
```

Rendered daemon.json:

```json
{
  "log-driver": "json-file",
  "storage-driver": "overlay2",
  "live-restore": true,
  "data-root": "/opt/docker",
  "log-opts": {
    "max-size": "50m",
    "max-file": "5"
  },
  "bip": "172.17.0.1/24",
  "default-address-pools": [
    {
      "base": "172.18.0.0/16",
      "size": 24
    }
  ],
  "insecure-registries": [
    "artifactory.example.com:5000"
  ]
}
```

## Variable reference

| Variable | Default | Purpose |
|---|---|---|
| `docker_repo_baseurl` | `""` | Custom repo URL. Empty = official upstream. Air-gap: set to Artifactory mirror URL. |
| `docker_repo_gpgkey` | `""` | GPG key URL for the repo. Empty = official Docker key URL. EL: rpm_key; Debian: saved as apt keyring. |
| `docker_repo_gpgcheck` | `true` | Verify GPG signatures on repo packages. |
| `docker_repo_sslverify` | `true` | Verify repo TLS cert. Set `false` for self-signed Artifactory. |
| `docker_packages` | `[docker-ce, docker-ce-cli, containerd.io, docker-buildx-plugin, docker-compose-plugin]` | Packages to install. Override for air-gap package naming. |
| `docker_version` | `""` | Version pin (e.g. `"26.1.4"`). Empty = latest. |
| `docker_data_root` | `"/opt/docker"` | Docker data directory (`daemon.json` `data-root`). |
| `docker_bip` | `""` | Bridge IP/prefix for docker0 (e.g. `"172.17.0.1/24"`). Empty = omitted (Docker default). |
| `docker_default_address_pools` | `[]` | Bridge IPAM pools. List of `{base: CIDR, size: N}`. Empty = omitted. |
| `docker_insecure_registries` | `[]` | Registries with HTTP or self-signed TLS. Added to `daemon.json`. |
| `docker_registry_mirrors` | `[]` | Pull-through mirrors consulted before Docker Hub. |
| `docker_log_driver` | `"json-file"` | Default container log driver. |
| `docker_log_max_size` | `"50m"` | Log file size limit per container (json-file/local only). |
| `docker_log_max_file` | `"5"` | Number of rotated log files per container (json-file/local only). |
| `docker_log_opts` | `{}` | Free-form log-opts merged over max-size/max-file (highest precedence). |
| `docker_storage_driver` | `"overlay2"` | Storage driver. |
| `docker_live_restore` | `true` | Keep containers running during daemon restart. Set `false` in swarm mode. |
| `docker_default_ulimits` | `{}` | Default ulimits for all containers. Empty = omitted. |
| `docker_extra_daemon_options` | `{}` | Arbitrary extra daemon.json keys merged last (wins over all others). |
| `docker_users` | `[]` | Users to add to the `docker` group. |
| `docker_service_user` | `""` | Optional service account (e.g. `svc-docker`). Empty = none created. |
| `docker_service_user_shell` | `/bin/bash` | Login shell for the service account. |
| `docker_service_user_system` | `true` | Create the service account as a system user. |
| `docker_service_user_groups` | `[docker, systemd-journal, adm]` | Supplementary groups for the service account. |
| `docker_manage_data_dir_owner` | `false` | Recursively chown `docker_data_root` to the service account. |
| `docker_data_dir_mode` | `0775` | Mode for `docker_data_root` when ownership is managed. |
| `docker_manage_opt_acl` | `false` | Apply docker-group ACLs on `docker_opt_acl_path`. |
| `docker_opt_acl_path` | `/opt` | Shared path to receive docker-group ACLs. |

> The `users` phase runs when **any** of `docker_users`, `docker_service_user`, or
> `docker_manage_opt_acl` is set; otherwise it is skipped. This absorbs the former
> standalone `docker_user` role — set `docker_service_user: svc-docker` +
> `docker_manage_opt_acl: true` to reproduce its behaviour.

## Service account + shared /opt example

```yaml
# inventories/<env>/group_vars/docker_hosts.yml
docker_service_user: svc-docker            # hyphenated, matches Vault entity naming
docker_manage_data_dir_owner: true         # svc-docker owns /opt/docker
docker_manage_opt_acl: true                # docker group gets rwx ACLs on /opt
docker_users:
  - ansible                                # automation user gets socket access
```

## Relationship to docker_swarm

The `docker_swarm` role has its own Docker CE install phase (`tasks/install_docker.yml`) gated by `docker_swarm_install_docker_enabled`. For hosts that are Swarm members, you can either:

- Use `docker_swarm` exclusively (set `docker_swarm_install_docker_enabled: true`)
- Apply `docker` first then `docker_swarm` with `docker_swarm_install_docker_enabled: false`
- Set `docker_live_restore: false` when using this role on swarm hosts — dockerd rejects `swarm init` when live-restore is on
