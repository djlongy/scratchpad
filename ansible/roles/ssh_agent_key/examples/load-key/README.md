# Load an SSH key into ssh-agent from Ansible Vault — runnable example

A minimal, self-contained walkthrough of the `ssh_agent_key` role: take a private SSH key
that lives **only** in an Ansible-Vault-encrypted variable and load it into your running
`ssh-agent`, so it never has to sit on disk to be usable for SSH.

```
load-key/
├── ansible.cfg                    # loads the role from ../../.. + pins pipelining
├── inventory.yml                  # just localhost (the role runs on the control node)
├── group_vars/all/vault.yml.example  # the ONE var to define, then encrypt → vault.yml
└── site.yml                       # loads vault_ssh_private_key into the agent
```

## Run it

```bash
cd examples/load-key

# 1. Create the encrypted var holding your PRIVATE key (see vault.yml.example
#    for the exact shape — a `vault_ssh_private_key: |` block scalar):
cp group_vars/all/vault.yml.example group_vars/all/vault.yml
$EDITOR group_vars/all/vault.yml            # paste your private key
ansible-vault encrypt group_vars/all/vault.yml

# 2. Load it into your ssh-agent (disk-free):
ansible-playbook site.yml --ask-vault-pass

# 3. Verify — the key is now in your agent:
ssh-add -l
```

The key lands in your running ssh-agent (your inherited `SSH_AUTH_SOCK`), so it is usable
from your terminal immediately after the play. Re-running is idempotent — the role only
reports `changed` when the key wasn't already in the agent. To bracket a play instead
(unlock at the start, lock at the end), see the role README's `pre_tasks`/`post_tasks`
pattern (`tasks_from: unlock` / `tasks_from: lock`).

## How the "never on disk" guarantee holds

- The role feeds the key to `ssh-add -` over **stdin** (`no_log`), never a file.
- `ansible.cfg` here sets `pipelining = True` (and `site.yml` pins `ansible_pipelining: true`)
  so Ansible's own module payload — which embeds task args — is piped to the interpreter
  instead of being written to `~/.ansible/tmp`.

Prove it: load the key, delete every on-disk copy, then
`ssh -v -F /dev/null <host>` — the key is offered as `… agent` (source = agent), not
`… explicit` (source = a file).

## Gotcha: `IdentitiesOnly yes` defeats the agent

If your `~/.ssh/config` pins `IdentityFile … + IdentitiesOnly yes` for a host, ssh uses
**only** that on-disk key and ignores the agent — the loaded key won't be used until you
relax those lines (or retire the on-disk key).

## Per-environment isolation

Give each environment its own keypair and wire a different `ssh_agent_key_content` per env
from inventory (e.g. `vault_dev_ssh_key` vs `vault_prod_ssh_key`, ideally stored under
separate Vault mounts/policies). A leaked dev key then can't open prod hosts. Combine with
`ssh_agent_key_lifetime` to auto-expire a prod key from the agent after a set time.
