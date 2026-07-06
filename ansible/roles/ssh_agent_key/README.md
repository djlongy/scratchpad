# ssh_agent_key

Load a private SSH key into a running `ssh-agent` **straight from memory** — the key is
fed to `ssh-add` over stdin and is never written to disk, never appears in `argv`, and
(with `no_log`) never appears in task output.

Run it on the control node with the key supplied from an Ansible Vault variable, so the
private key only ever exists encrypted at rest and in the agent's memory.

## How it works

- **Inherited agent first.** On macOS your login shell shares a persistent `launchd`
  ssh-agent (`SSH_AUTH_SOCK` is always set). A key added into that inherited socket
  stays loaded and is usable from your terminal after the play ends.
- **Throwaway agent fallback.** If no agent is reachable (`SSH_AUTH_SOCK` unset — common
  over plain SSH / tmux / CI), a `block`/`rescue` spawns a throwaway agent so the run is
  self-sufficient. A `debug` note flags that a spawned agent is **not** visible to an
  interactive shell — an Ansible child process cannot export `SSH_AUTH_SOCK` up into your
  terminal. On macOS this path should effectively never fire.
- **Idempotent.** The agent's identity list is snapshotted before and after the add; the
  task reports `changed` only when the identity set actually changed (re-adding an
  already-present key is a harmless no-op — the agent dedupes).

## Never on disk

`ssh-add -` reads the key from **stdin**; the `command` module's `stdin:` parameter feeds
it directly from the vaulted variable. To keep Ansible's own module payload off disk too,
run with **pipelining enabled** (`pipelining = True` in `ansible.cfg`, or
`ansible_pipelining: true` on the play) so the AnsiballZ module — which embeds task args —
is piped to the interpreter instead of being written to `~/.ansible/tmp`.

## Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `ssh_agent_key_content` | `""` | **Required.** Private key text (PEM / OpenSSH). Wire from a vaulted var. |
| `ssh_agent_key_auth_sock` | `"{{ lookup('env', 'SSH_AUTH_SOCK') }}"` | Agent socket to load into. |
| `ssh_agent_key_comment` | `"ansible"` | Cosmetic label in the completion message. |
| `ssh_agent_key_lifetime` | `0` | Seconds; `0` = until agent restart, `>0` = `ssh-add -t`. |

## Minimal usage

```yaml
- name: Load an SSH key from Ansible Vault into the ssh-agent
  hosts: localhost
  connection: local
  gather_facts: false
  vars:
    ansible_pipelining: true
  roles:
    - role: ssh_agent_key
      vars:
        ssh_agent_key_content: "{{ vault_ssh_private_key }}"   # from a vaulted var
```

```bash
ansible-playbook -i inventories/hosts.yml playbooks/load_ssh_agent_key.yml
ssh-add -l          # verify it's loaded
```

## Runnable example

A complete, self-contained walkthrough (its own `ansible.cfg`, `inventory.yml`, an
encrypted-var stub, and `site.yml`) lives in [`examples/load-key/`](examples/load-key/) —
`cd` in and follow its README to create the vaulted key and load it.

## Notes

- **`IdentitiesOnly yes` defeats the agent.** If `~/.ssh/config` pins
  `IdentityFile … + IdentitiesOnly yes` for a host, ssh uses **only** that on-disk key
  and ignores the agent — the loaded key won't be used until you relax those lines (or
  retire the on-disk key). Verify with `ssh -v <host>`: an agent key shows as
  `Offering public key: … agent`; an on-disk key shows `… explicit`.
- Single key per invocation. Call the role once per key if you need several loaded.
- Per-environment isolation: give each environment its own keypair and wire a different
  `ssh_agent_key_content` per env from inventory, so a leaked key can't open every host.
