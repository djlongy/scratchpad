# ssh_agent_key

Unlock/lock a private SSH key in your running `ssh-agent`, **straight from memory** —
the key is fed to `ssh-add` over stdin, so it never touches disk, never appears on a
command line, and (`no_log`) never appears in task output. Linux-first, no
`expect`/`pexpect` dependency, no agent spawning, no environment lookups.

Two entry points, designed to bracket a play:

- **`unlock`** — load the key at the start (`pre_tasks`) — 6 small tasks
- **`lock`** — remove it at the end (`post_tasks`) — 1 task

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
        ssh_agent_key_content: "{{ vault_deploy_key }}"
        ssh_agent_key_passphrase: "{{ vault_deploy_key_passphrase }}"   # if encrypted
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

Run it exactly like any other play — e.g. `ansible-playbook site.yml --ask-vault-pass`.
Load-only (no lock) also works: `roles: [ssh_agent_key]` defaults to unlock.

## Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `ssh_agent_key_content` | `""` | **Required (unlock).** Private key text, from a vaulted var. |
| `ssh_agent_key_passphrase` | `""` | Passphrase for an encrypted key, from a vaulted var. Empty = unencrypted key. |
| `ssh_agent_key_lifetime` | `0` | Seconds; `0` = until locked/agent restart, `>0` = `ssh-add -t` auto-expiry. |
| `ssh_agent_key_public` | `""` | **lock only.** Public key line, to remove a key unlock didn't add. |

## What each task does, and why it exists

### unlock (6 tasks)

1. **Check the key material was provided** — fail immediately with a clear message
   instead of a cryptic `ssh-add` error when the vaulted var isn't wired.
2. **List the agent's current keys** (`ssh-add -L`) — two jobs in one: it proves the
   agent is reachable, and the "before" list is what steps 5–6 compare against.
3. **Check an ssh-agent is reachable** — `ssh-add` talks to the agent through the
   `SSH_AUTH_SOCK` socket your shell exports, and the play simply inherits it. The role
   deliberately does **not** start an agent for you: an agent spawned inside a task
   lives in that task's environment, so your terminal — and Ansible's own SSH
   connections — can never see it. It would "work" and then nothing could use the key.
   Instead this fails with instructions; for headless/CI runs, start the agent as the
   parent of the run: `ssh-agent ansible-playbook site.yml` (it dies with the run).
4. **Add the key** (`ssh-add -`) — the private key goes in over **stdin**, which is why
   there is no key file and no `expect`: `expect` needs to own the program's input to
   type at it, and stdin is already carrying the key. For encrypted keys, `ssh-add`
   with no terminal asks the program named in `SSH_ASKPASS` for the passphrase —
   that's `files/askpass.sh`, three static lines that echo `$SSH_AGENT_KEY_PASSPHRASE`.
   The task places your **vaulted** passphrase in ssh-add's own process environment,
   where the helper reads it; the role never reads your session environment, and with
   an unencrypted key the helper is never called at all.
5. **List keys again** — re-adding a key that's already loaded is a no-op (the agent
   dedupes), so the honest `changed` signal is whether the agent's key list actually
   changed. Re-runs report `ok`, first runs report `changed`.
6. **Remember the key we added** — agents delete keys *by public key*. The one new
   line between the two listings is the public key of what we loaded; lock uses it.

### lock (1 task)

7. **Remove the key unlock added** — feeds that public line to `ssh-add -d /dev/stdin`
   (still nothing on disk). If unlock added nothing — the key was already in the agent
   before the play, or unlock never ran — the task **skips**: lock only removes what
   unlock loaded. To remove a key loaded some other way, pass `ssh_agent_key_public`.

## Notes

- **State is play-scoped.** unlock remembers what it added in a host fact on the play's
  first host — run unlock and lock in the *same play* (the pattern above).
- **`IdentitiesOnly yes` defeats the agent.** If `~/.ssh/config` pins
  `IdentityFile` + `IdentitiesOnly yes` for a host, ssh ignores agent keys. Verify with
  `ssh -v <host>`: an agent key offers as `... agent`, an on-disk key as `... explicit`.
- Passphrase support needs OpenSSH ≥ 8.4 (`SSH_ASKPASS_REQUIRE`) — RHEL 9 ships 8.7.
- Set `ansible_pipelining: true` if you also want Ansible's own module payloads off
  disk; the key itself never touches disk either way.
- One key per unlock/lock pair. Call the role once per key for several.
