# freeipa_server_docker

Run a **FreeIPA server as a container** (official `quay.io/freeipa/freeipa-server` image) on
a Docker/Podman host, with a persistent `/data` volume. The `<noun>_<purpose>` sibling of
[`freeipa_server`](../freeipa_server/) — mirroring `hashicorp_vault` + `hashicorp_vault_docker`.

This role is **thin**: it owns only the container lifecycle. All declarative configuration
(IAM, DNS, hardening, backup) is delegated to the existing `freeipa_server` role, run
**inside** the container via the `community.docker.docker` connection plugin — the container
is a full IPA server, so every native `freeipa.ansible_freeipa` module works there in server
context. **Validated E2E** against a live realm (AlmaLinux 9, `ipa-server-4.13.1`).

## TL;DR

**Most common: redeploy the container after an image bump.** Set `freeipa_server_deployment: container`, bump the image tag, and re-run — the container self-upgrades `ipa-server-upgrade` against the `/data` volume.

```bash
ansible-playbook -i inventories/<env>/hosts.yml playbooks/freeipa_container.yml [--tags deploy] [--limit <host>]
```

## Why containerize

Update FreeIPA independently of the host OS: swap the image tag → the container runs
`ipa-server-upgrade` against the `/data` volume automatically (in-major only,
`almalinux-9` → newer `almalinux-9`). Portable, redeployable, `docker restart`-recoverable.

## Deployment toggle

`playbooks/freeipa_container.yml` selects the substrate from ONE variable:

```yaml
freeipa_server_deployment: package    # (default) FreeIPA as host RPMs — the freeipa_server role
freeipa_server_deployment: container  # FreeIPA as a container — this role
```

## Install modes (`freeipa_server_docker_install_mode`)

| Mode | Does |
|---|---|
| `existing` (default) | Boot a populated `/data` — never (re)installs. Safe/idempotent. |
| `fresh` | `ipa-server-install` a NEW realm in the container. |
| `replica` | `ipa-replica-install --setup-ca --setup-dns` against a live master — the **lift-and-shift** path (exact dataset synced by replication, no `/data`-copy risk). |

## Decommission a node (`freeipa_server_state: absent`)

Declarative removal, mirroring `freeipa_server_is_primary`. Set `freeipa_server_state: absent` on a
host (host_vars) and re-run the play across the group — the node is `server-del`'d from a surviving
master (mode-aware) and its container removed, then the play ends for it. Works for package and
container hosts. **Refuses** to remove the last server. If the node is the **CA renewal master** it
also refuses, unless you set `freeipa_server_decommission_transfer_renewal: true` — then it moves
the renewal master (+ CRL generation) to the surviving master first, self-contained (no external
role). `*_decommission_wipe_data: true` also clears `/data`. Reducing 3 replicas → 2 = flip the
third host's flag, re-run.

**Requirements / notes:** the surviving-master selection filters peers to the **same realm** by
comparing `freeipa_server_domain` — so that var **must be set in inventory group_vars** (a role-
default `domain` fallback isn't in `hostvars` for out-of-play peers → they're excluded → a
fail-**closed** "no surviving master" refusal, which is safe but blocks the run). `server-del
--force` is idempotent (returns "Deleted IPA server" even when the entry is already absent —
verified live on IPA 4.13), so a re-run is safe. The decommission flag has been live-exercised
end-to-end (throwaway replica: join → migrate → `state: absent` → clean removal, `failed=0`).

## Rolling package → container migration (playbook)

`playbooks/freeipa_migrate_to_container.yml` migrates one node in place, keeping its
original FQDN, by composing the two self-contained roles (Stage 1 `freeipa_server` decommission →
Stage 2 `freeipa_server_docker` replica). Run once per node; keep ≥1 master up:

```bash
ansible-playbook -i inventories/example/hosts.yml playbooks/freeipa_migrate_to_container.yml \
  -e freeipa_migrate_node=idm01 -e freeipa_migrate_source=idm02.example.com
```

## Phases (tags)

| Tag | Runs on | Does |
|---|---|---|
| `prereqs` | VM host | Install Docker (reuses the `docker` role), open the IPA firewall ports, create `/data` |
| `deploy` | VM host | Run the FreeIPA container (idempotent `community.docker.docker_container`) + wait healthy |
| `register` | VM host | `add_host` the container (docker connection) so a following play runs `freeipa_server` inside it |

## Runtime facts (systemd-in-container, validated on AlmaLinux 9 / cgroups v2 / Docker 29)

- **`--privileged` is NOT supported** by the image. systemd needs `--cgroupns=host` +
  `-v /sys/fs/cgroup:/sys/fs/cgroup:rw`, the image's own `/run`,`/tmp` tmpfs, `-h <fqdn>`.
- The installer's **cgroup-v2 RAM probe fails** under `--cgroupns=host` (root cgroup has no
  `memory.max`); the role passes **`--skip-mem-check`** (`freeipa_server_docker_skip_mem_check`).
- **Host owns time** — the container installs `--no-ntp`; run chrony on the VM host.
- FreeIPA needs ~1.2 GB+ RAM available; give the VM ≥ 4 GB for IPA + Dogtag CA.

## Managing the containerized server with native modules

The `community.docker.docker` connection needs the container's docker daemon. For a **remote**
container, export `DOCKER_HOST` on the controller (like `ANSIBLE_VAULT_PASSWORD`):

```bash
eval "$(ssh-agent -s)"; ssh-add ~/.ssh/<automation-key>.pem
export DOCKER_HOST=ssh://ansible@<vm-host-ip>      # ansible user must be in the docker group
export ANSIBLE_VAULT_PASSWORD=$(cat ~/.vault-pass.txt)
ansible-playbook -i inventories/example/hosts.yml playbooks/freeipa_container.yml \
    -e freeipa_deploy_target=idm02 -e freeipa_server_deployment=container
```

The registered container host uses `ansible_user: root`, `ansible_remote_tmp: /tmp/...`
(both required — the image has no `ansible` user and `~` doesn't expand under `docker exec`).

## Required inventory variables

Inherited from the `freeipa_server_*` estate group_vars where present (the toggle is seamless):

| Variable | Example | Purpose |
|---|---|---|
| `freeipa_server_docker_domain` | `example.com` | IPA domain (→ realm) |
| `freeipa_server_docker_admin_password` | *(Ansible Vault)* | IPA admin password — **primary** credential source (see below) |
| `freeipa_server_docker_dm_password` | *(Ansible Vault)* | Directory Manager password (fresh install only) |
| `freeipa_server_docker_vault_secret` | `kv/data/platform/freeipa/runtime` | **Optional** HashiCorp Vault fallback path for the passwords above |
| `freeipa_server_docker_forwarders` | `[10.0.0.53]` | Upstream DNS forwarders |
| `freeipa_server_docker_replica_server` | `idm01.example.com` | Master to enrol against (`replica` mode) |

See `defaults/main.yml` for the full surface.

## Credentials — Ansible Vault first, HashiCorp Vault fallback

The role (and the `freeipa_server` config engine it drives) resolves each password
**Ansible Vault first, HashiCorp Vault as an optional fallback**, so it runs unchanged in an
environment that has no HashiCorp Vault:

1. **Primary (Ansible Vault):** set `freeipa_server_docker_admin_password` /
   `freeipa_server_docker_dm_password` — normally to a var held in an Ansible-Vault-encrypted
   `group_vars` file (they also inherit `freeipa_server_admin_password` / `_dm_password`). When
   set, the HashiCorp Vault lookup is never evaluated and `community.hashi_vault` is not needed.
2. **Fallback (HashiCorp Vault):** leave the password vars empty and set
   `freeipa_server_docker_vault_secret` to the KV path holding them (fields
   `freeipa_server_docker_admin_password_field` / `_dm_password_field`).
3. **Neither set →** the role fails fast with a clear message (no cryptic Vault connection error).

An Ansible-Vault-only deployment therefore just populates the two password vars and never
references HashiCorp Vault.

## See also

- [`freeipa_server`](../freeipa_server/) — the package-install role + the reused config engine
