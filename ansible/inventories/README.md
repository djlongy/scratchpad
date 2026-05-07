# Inventories

One subdirectory per environment / source. Each contains a `hosts.yml`
and `group_vars/` for non-secret tunables, with `vault.yml.example`
showing the secret variable names callers must define
(`ansible-vault encrypt` the real `vault.yml`).

| Subdir | Purpose | Used by |
|---|---|---|
| [`vault/`](vault/)   | 3-node lab inventory for the Vault Raft cluster | `playbooks/vault_cluster.yml` (role: `vault_container`) |
| [`swarm/`](swarm/)   | Docker Swarm bootstrap group with overlay subnet registry | `playbooks/mattermost_swarm.yml` and friends (roles: `swarm_stack`, `mattermost_swarm*`) |
| [`vmware/`](vmware/) | vSphere dynamic inventory plugin configs (community.vmware + vmware.vmware) | any playbook that targets vSphere VMs |
