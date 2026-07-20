# Example: full end-to-end deployment (bare template → configured host)

One inventory, one `ansible-playbook` run — from a bare vCenter template to a fully
configured host: **unlock SSH key → build VM → provision storage → baseline →
firewall → docker**.

## Files

| File | What it does |
|---|---|
| `site.yml` | two plays: (1) load the ansible SSH key into ssh-agent on the controller; (2) build the VM and run every on-guest role over it |
| `inventory.yml` | host `app01` with the full contract: vCenter placement, hardware, two data disks + a storage profile (`/opt`, `/var/lib/docker`) |
| `group_vars/all/vault.yml.example` | template for the vaulted `vault_ssh_private_key` (copy → `vault.yml`, paste key, `ansible-vault encrypt`) |
| `ansible.cfg` | `roles_path`, `remote_user = ansible`, `host_key_checking = False`, `pipelining = True` (keeps the key off disk) |

Everything in `inventory.yml` is a placeholder — swap in your vCenter, datastore,
template, portgroup, IPs and Vault path.

## Run it

```bash
# 1. put your ansible PRIVATE key into the vault
cp group_vars/all/vault.yml.example group_vars/all/vault.yml
# ...paste your key...
ansible-vault encrypt group_vars/all/vault.yml

# 2. deploy end to end (vCenter password is pulled from HashiCorp Vault at run time)
export ANSIBLE_VAULT_PASSWORD=...
ansible-playbook site.yml
```

Day-2:

```bash
ansible-playbook site.yml --tags redeploy -e vsphere_vm_force_redeploy=true   # rebuild the VM, re-run everything
ansible-playbook site.yml --tags create,grow                                  # after bumping a disk size
ansible-playbook site.yml -e vsphere_vm_state=absent --tags destroy -e vsphere_vm_force_destroy=true
```

## How it flows

1. **Play 1 — unlock (controller).** `ssh_agent_key` loads the vaulted private key
   straight into `ssh-agent` — never on disk. Play 2 then authenticates to the new VM
   key-only.
2. **Play 2 — provision + configure (the VM).**
   - `vsphere_vm` clones the VM from the template (the plan is inventory-derived — no
     vCenter state read — and a pre-build report prints what's coming). With
     `vsphere_vm_wait_for_ssh` it blocks until the guest answers SSH, then gathers facts.
   - `storage` turns the two data disks into `/opt` and `/var/lib/docker` (by-size,
     `100%FREE`, FRESH-guarded).
   - `baseline` → `firewalld` → `docker` configure the OS, firewall and container runtime.

## Requirements / gotchas

- **Inherited ssh-agent.** Play 1 loads into the agent your shell already exposes
  (`SSH_AUTH_SOCK`). On macOS that's the launchd agent; elsewhere run `eval $(ssh-agent)`
  first. A *spawned* agent can't export its socket up to the parent, so play 2 wouldn't
  see the key.
- **Template must carry the matching PUBLIC key** in the ansible user's
  `authorized_keys`, or the SSH wait in play 2 times out.
- **`gather_facts: false`** at play 2 is required (the VM doesn't exist at play start);
  the handoff gathers facts once it's up.
- **`become`** lives at the play level — safe because `vsphere_vm` forces
  `become: false` on its own localhost-delegated tasks while the on-guest roles inherit
  `become: true`.

For a minimal version (just VM + storage, no SSH-key/baseline/firewall/docker) see
[`../provision-and-storage/`](../provision-and-storage/).
