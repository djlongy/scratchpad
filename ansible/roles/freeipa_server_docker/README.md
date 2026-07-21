# freeipa_server_docker

## TL;DR

Runs a FreeIPA server as a container (`quay.io/freeipa/freeipa-server`) on a
Docker/Podman host, with a persistent `/data` volume. Owns only the container
lifecycle — declarative config (IAM, DNS, hardening, backup) runs **inside**
the container via the `community.docker.docker` connection plugin, in a
follow-up play.

```bash
ansible-playbook -i inventories/<env>/hosts.yml playbooks/freeipa_container.yml -e freeipa_server_deployment=container
```

Redeploy after an image bump: bump `freeipa_server_docker_image`'s tag and re-run — the
container self-upgrades `ipa-server-upgrade` against `/data`.

## Requirements

Install collections before running (repo `requirements.yml`, or ad-hoc):

    ansible-galaxy collection install -r requirements.yml

| Collection | When | Used for |
|---|---|---|
| `community.docker` | When `freeipa_server_docker_runtime: docker` (default) | container lifecycle (`docker_container`) + the `community.docker.docker` connection plugin used by `register` |
| `containers.podman` | When `freeipa_server_docker_runtime: podman` | container lifecycle (`podman_container`) |
| `ansible.posix` | When `freeipa_server_docker_manage_firewall` | `firewalld` port management |
| `community.hashi_vault` | When Vault credential fallback | admin/DM password lookup |

## Key variables

Full list: `defaults/main.yml`. Contract: `meta/argument_specs.yml`.

**Required** = value must be correct for a successful run (defaults often work).
**Optional** = safe to leave default / empty; phase stays off or uses built-ins.
**When X** = required only if that feature is on.

| Req | Variable | Default | Purpose |
|---|---|---|---|
| **Required** | `freeipa_server_docker_domain` | `{{ freeipa_server_domain \| default(domain \| default('')) }}` | FreeIPA primary DNS domain |
| Optional | `freeipa_server_docker_image` | `quay.io/freeipa/freeipa-server:almalinux-9` | Container image; pin the OS tag to keep upgrades in-major |
| Optional | `freeipa_server_docker_data_dir` | `/var/lib/ipa-data` | Host path bind-mounted at `/data` — the entire persistent dataset |
| Optional | `freeipa_server_docker_runtime` | `docker` | `docker` \| `podman` |
| Optional | `freeipa_server_docker_install_mode` | `existing` | `existing` (boot populated `/data`) \| `fresh` \| `replica` |
| When replica | `freeipa_server_docker_replica_server` | `""` | FQDN of the live master to enrol against |
| Optional | `freeipa_server_docker_forwarders` | inherits `freeipa_server_forwarders` | Upstream DNS forwarders |
| When admin ops | `freeipa_server_docker_admin_password` | inherits `freeipa_server_admin_password` | Admin password — declared var wins |
| When Vault fallback | `freeipa_server_docker_vault_secret` | inherits `freeipa_server_vault_secret` | HashiCorp Vault KV path for admin/DM passwords |
| Optional | `freeipa_server_docker_skip_mem_check` | `true` | Pass `--skip-mem-check` (cgroup-v2 RAM probe is unreliable in containers) |
| Optional | `freeipa_server_docker_state` | inherits `freeipa_server_state` (`present`) | `absent` decommissions the node |
| Optional | `freeipa_server_docker_decommission_transfer_renewal` | `false` | On decommission, move CA renewal master + CRL to survivor instead of refusing |
| Optional | `freeipa_server_docker_decommission_wipe_data` | `false` | Also remove `/data` on decommission |
| Optional | `freeipa_server_docker_register_container` | `true` | `add_host` the container so a following play can run `freeipa_server` inside it |
| Optional | `freeipa_server_docker_manage_firewall` | `true` | Open the IPA port set on the host firewall |

## Usage

```yaml
- name: FreeIPA server — container
  hosts: freeipa
  become: true
  roles:
    - role: freeipa_server_docker
      when: freeipa_server_deployment | default('package') == 'container'
```

Run it:

```bash
ansible-playbook -i inventories/<env>/hosts.yml playbooks/freeipa_container.yml \
  -e freeipa_deploy_target=<host> -e freeipa_server_deployment=container
```

## Preconditions

- The VM host must already run its own time sync (chrony) — the container
  installs with `--no-ntp` and relies on host time.
- FreeIPA needs roughly 1.2 GB+ RAM available; size the VM at 4 GB+ for IPA +
  Dogtag CA. The installer's own memory probe is skipped (`--skip-mem-check`),
  so an undersized host will still attempt the install rather than fail fast.
- `replica` install mode needs a live, already-installed master
  (`freeipa_server_docker_replica_server`) reachable from the host.
- Validated on AlmaLinux 9 / cgroups v2 / Docker.

## Behaviour

- **Runtime install** — `prereqs` installs Docker or Podman itself (reusing
  the repo's `docker`/`podman` roles per `freeipa_server_docker_runtime`),
  creates `/data`, and opens the IPA port set on `firewalld` — best-effort,
  never blocks the run if firewalld is absent/disabled on a lab host.
- **Install modes**:

  | Mode | Does |
  |---|---|
  | `existing` (default) | Boots a populated `/data`, never (re)installs — safe/idempotent |
  | `fresh` | `ipa-server-install` a new realm in the container |
  | `replica` | `ipa-replica-install --setup-ca --setup-dns` against a live master — exact dataset synced by replication |

- **Container constraints** — `--privileged` is not supported by the image;
  systemd needs `--cgroupns=host` plus `-v /sys/fs/cgroup:/sys/fs/cgroup:rw`,
  the image's own `/run`/`/tmp` tmpfs, and `-h <fqdn>`.
- **Native-module management** — the `community.docker.docker` connection
  needs the container's docker daemon reachable. For a remote container,
  export `DOCKER_HOST=ssh://<user>@<vm-host-ip>` on the controller before
  running a follow-up play against the registered container host. That host
  uses `ansible_user: root` and a `/tmp`-rooted `ansible_remote_tmp` — both
  required, since the image has no `ansible` user and `~` doesn't expand
  under `docker exec`.
- **Credentials** — resolve declared-var-first with Vault as an optional
  fallback: set `freeipa_server_docker_admin_password`/`_dm_password`
  directly and the Vault lookup is never evaluated; leave them empty and set
  `freeipa_server_docker_vault_secret` (fields `_admin_password_field`/
  `_dm_password_field`) to use Vault instead. Neither set → the role fails
  fast with a clear message.
- **Decommission** (`freeipa_server_docker_state: absent`) — set the flag on
  a host (host_vars) and re-run the play across the group: the node is
  `server-del`'d from a surviving master (mode-aware) and its container
  removed, then the play ends for it. Refuses to remove the last server, and
  refuses on the CA renewal master unless
  `freeipa_server_docker_decommission_transfer_renewal: true` is set, which
  moves the renewal master (+ CRL generation) to the surviving master first.
  Surviving-master selection filters peers by `freeipa_server_domain`, so
  that var must be set in inventory group_vars — a missing value excludes
  out-of-play peers and fails closed with "no surviving master" (safe, but
  blocks the run).
