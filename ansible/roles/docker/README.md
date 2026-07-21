# docker

## TL;DR

Installs and configures Docker CE on EL (RHEL/AlmaLinux 8/9) and Debian/Ubuntu — from
the official upstream repo or from a custom mirror (e.g. Artifactory for air-gapped
fleets). Configures the repo + GPG key, installs packages, renders `/etc/docker/daemon.json`
from variables (only set keys are emitted), and enables the service.

```bash
ansible-playbook -i inventories/<env>/hosts.yml playbooks/vault_solo_e2e.yml --tags docker
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
| When air-gap / custom mirror | `docker_repo_baseurl` / `_gpgkey` | `""` | Custom repo URL + GPG key; empty = official upstream |
| Optional | `docker_repo_sslverify` | `true` | Set `false` for a self-signed internal mirror |
| Optional | `docker_packages` | `[docker-ce, docker-ce-cli, containerd.io, docker-buildx-plugin, docker-compose-plugin]` | Packages to install |
| Optional | `docker_version` | `""` | Version pin (e.g. `"26.1.4"`); empty = latest |
| Optional | `docker_data_root` | `/opt/docker` | Docker data directory (`daemon.json` `data-root`) |
| Optional | `docker_bip` | `""` | Bridge IP/prefix for `docker0`; empty = Docker default |
| Optional | `docker_insecure_registries` | `[]` | Registries reachable over HTTP or self-signed TLS |
| Optional | `docker_live_restore` | `true` | Keep containers running during daemon restart; set `false` in swarm mode |
| Optional | `docker_users` | `[]` | Users added to the `docker` group |
| When a service account is wanted | `docker_service_user` | `""` | Name of a dedicated service account (e.g. `svc-docker`); empty = none created |
| When `docker_service_user` is set | `docker_manage_data_dir_owner` | `false` | Recursively chown `docker_data_root` to the service account |
| When shared-path ACLs are wanted | `docker_manage_opt_acl` | `false` | Apply docker-group ACLs on `docker_opt_acl_path` (default `/opt`) |

## Usage

```yaml
- hosts: docker_hosts
  roles:
    - role: docker
      tags: [docker]
```

Run it:

```bash
ansible-playbook -i inventories/<env>/hosts.yml playbooks/vault_solo_e2e.yml --tags docker

# Air-gap Artifactory mirror (prerequisite: Artifactory has a remote Docker CE repo
# proxying download.docker.com, and a local Docker V2 registry)
ansible-playbook -i inventories/<env>/hosts.yml playbooks/vault_solo_e2e.yml --tags docker \
  -e docker_repo_baseurl=https://artifactory.example.com/artifactory/docker-ce-remote/linux/centos/9/x86_64/stable \
  -e docker_repo_gpgkey=https://artifactory.example.com/artifactory/docker-ce-remote/linux/centos/gpg \
  -e docker_repo_sslverify=false

# Dedicated service account + shared /opt ACLs
ansible-playbook -i inventories/<env>/hosts.yml playbooks/vault_solo_e2e.yml --tags docker \
  -e docker_service_user=svc-docker -e docker_manage_data_dir_owner=true -e docker_manage_opt_acl=true
```

## Behaviour

The `users` phase (docker-group members, optional service account, optional
shared-path ACLs) runs only when at least one of `docker_users`,
`docker_service_user`, or `docker_manage_opt_acl` is set; otherwise it is a no-op. On
Debian/Ubuntu the same repo/mirror variables apply — the role picks the correct apt vs.
yum path automatically.

## Out of scope

`docker_swarm` has its own Docker CE install phase, gated by
`docker_swarm_install_docker_enabled`. For hosts that are Swarm members, either use
`docker_swarm` exclusively, or apply this role first with `docker_swarm` set to skip
install (`docker_swarm_install_docker_enabled: false`). Set `docker_live_restore:
false` when using this role on swarm hosts — dockerd rejects `swarm init` when
live-restore is on.
