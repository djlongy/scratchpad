# Containerized FreeIPA — runnable example

A minimal, self-contained walkthrough of the `freeipa_server_docker` role: one Docker-capable
VM host, FreeIPA running in the official `quay.io/freeipa/freeipa-server` container with a
persistent `/data` volume, then the `freeipa_server` declarative engine run **inside** the
container for IAM/DNS config.

```
container-deploy/
├── inventory.yml     # one VM host + the deployment toggle and realm vars
├── ansible.cfg       # loads the roles from ../../..
└── site.yml          # play 1: container up + register · play 2: config inside it
```

## Run it

```bash
# The docker connection plugin talks to the VM's docker daemon over SSH:
eval "$(ssh-agent -s)"; ssh-add ~/.ssh/<automation-key>.pem
export DOCKER_HOST=ssh://ansible@10.0.0.11

# First boot — install a NEW realm in the container (or replica, see inventory.yml):
ansible-playbook site.yml \
  -e freeipa_server_docker_install_mode=fresh \
  -e freeipa_server_admin_password='<admin-pw>' \
  -e freeipa_server_dm_password='<dm-pw>'

# Every later run: the default mode ("existing") just boots the populated /data.
ansible-playbook site.yml
```

## The three lifecycle moves

| Move | How |
|---|---|
| **Join an existing realm** | `install_mode: replica` + `freeipa_server_docker_replica_server: <master FQDN>` — the dataset arrives via replication (lift-and-shift). |
| **Upgrade FreeIPA** | Bump the image tag (stay in-major, e.g. `almalinux-9` → newer `almalinux-9`); the container runs `ipa-server-upgrade` against `/data` on start. |
| **Decommission a node** | Set `freeipa_server_state: absent` in that host's host_vars and re-run — `server-del` via a surviving master + container removal, with last-server / CA-renewal-master guards. |

A rolling **package → container migration** of an existing realm (keeping each node's FQDN)
is a separate composed playbook — see `playbooks/freeipa_migrate_to_container.yml` and the
role README.

## Notes

- Give the VM ≥ 4 GB RAM (IPA + Dogtag CA).
- The container uses host networking and `--cgroupns=host` (systemd-in-container); the image
  does **not** support `--privileged`.
- Credentials resolve **Ansible Vault first, HashiCorp Vault fallback** — in real use, put the
  passwords in an Ansible-Vault-encrypted group_vars file instead of `-e`.
