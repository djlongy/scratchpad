# ssh_agent_key

Unlock/lock a private SSH key in `ssh-agent`, **straight from memory** — the key is fed
to `ssh-add` over stdin, so it never touches disk, never appears on a command line, and
(`no_log`) never appears in task output. The key is a **raw, passphrase-less** key that
lives encrypted in Ansible Vault; the vault password (`--ask-vault-pass` /
`-e @vault.yml`) is what protects it at rest.

Two entry points, designed to bracket a play:

- **`unlock`** — load the key at the start (`pre_tasks`)
- **`lock`** — remove it at the end (`post_tasks`)

`run_once` + `delegate_to: localhost` are baked into both, so call them bare.

## Usage

```yaml
- name: Deploy with the vaulted key unlocked for the duration of the play
  hosts: web
  gather_facts: false            # facts would need SSH before the key is unlocked
  pre_tasks:
    - name: Unlock the deploy key
      ansible.builtin.import_role:
        name: ssh_agent_key
        tasks_from: unlock
      vars:
        ssh_agent_key_content: "{{ vault_ssh_private_key }}"
    - name: Gather facts now the key is available
      ansible.builtin.setup:
  roles:
    - deploy
  post_tasks:
    - name: Lock the deploy key
      ansible.builtin.import_role:
        name: ssh_agent_key
        tasks_from: lock
```

Run it like any other play — `ansible-playbook site.yml --ask-vault-pass`.
Load-only (no lock) also works: `roles: [ssh_agent_key]` defaults to unlock.

## Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `ssh_agent_key_content` | `""` | **Required (unlock).** Raw private key text, from a vaulted var. |
| `ssh_agent_key_lifetime` | `0` | Seconds; `0` = until locked/agent stops, `>0` = `ssh-add -t` auto-expiry. |
| `ssh_agent_key_public` | `""` | **lock only.** Public key line, to remove a key unlock didn't add. |
| `ssh_agent_key_sock` | *output* | Set by unlock when it spawned an agent — the socket path to reuse it. |

## What each task does, and why it exists

### unlock

1. **Check the key material was provided** — fail immediately with a clear message
   instead of a cryptic `ssh-add` error when the vaulted var isn't wired.
2. **List the agent's current keys** (`ssh-add -L`) — the "before" list that steps 4–5
   compare against.
3. **Add the key** (`ssh-add -`) — the private key goes in over **stdin**: no key file
   on disk, nothing on a command line. This tries the agent the play inherited from
   your shell (`SSH_AUTH_SOCK`) first.
   **Rescue — no agent running:** spawn one (`ssh-agent -s`), keep its socket in
   `ssh_agent_key_sock`, and add the key again. Two things worth knowing:
   - The spawned agent **keeps running after the play** — the role prints the
     `export SSH_AUTH_SOCK=...` line so you (or the next run) can reuse it instead of
     spawning another.
   - Ansible's own SSH connections inherit `ansible-playbook`'s environment, not a
     task's — so the rescue also points the play's connections at the new agent
     (`IdentityAgent` in `ansible_ssh_common_args`). Without that, the key would load
     into an agent nothing in the play could see.
4. **List keys again** — re-adding a key that's already loaded is a no-op (the agent
   dedupes), so the honest `changed` signal is whether the agent's key list actually
   changed. Re-runs report `ok`, first runs report `changed`.
5. **Remember the key we added** — agents delete keys *by public key*. The one new
   line between the two listings is the public key of what we loaded; lock uses it.

### lock

6. **Remove the key unlock added** — feeds that public line to `ssh-add -d /dev/stdin`
   (still nothing on disk). If unlock added nothing — the key was already in the agent
   before the play, or unlock never ran — the task **skips**: lock only removes what
   unlock loaded. To remove a key loaded some other way, pass `ssh_agent_key_public`.
   An agent unlock spawned is left running; only the key is removed.

## Notes

- **Raw keys only.** A passphrase-protected key fails (`ssh-add` has no terminal to ask
  on). Store the key passphrase-less inside Ansible Vault — encryption at rest comes
  from the vault, not from a key passphrase.
- **State is play-scoped.** unlock remembers what it added in a host fact on the play's
  first host — run unlock and lock in the *same play* (the pattern above).
- **`IdentitiesOnly yes` defeats the agent.** If `~/.ssh/config` pins
  `IdentityFile` + `IdentitiesOnly yes` for a host, ssh ignores agent keys. Verify with
  `ssh -v <host>`: an agent key offers as `... agent`, an on-disk key as `... explicit`.
- Set `ansible_pipelining: true` if you also want Ansible's own module payloads off
  disk; the key itself never touches disk either way.
- One key per unlock/lock pair. Call the role once per key for several.
