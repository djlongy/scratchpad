# ssh_agent_key

Unlock/lock a private SSH key in `ssh-agent`, **straight from memory** — the key is fed
to `ssh-add` over stdin, so it never touches disk, never appears on a command line, and
(`no_log`) never appears in task output. The key lives encrypted in Ansible Vault
(`--ask-vault-pass` / `-e @vault.yml`); if the key itself also has a passphrase, set
`ssh_agent_key_passphrase` (from a vaulted var) and the role unlocks it with that.

Two entry points, designed to bracket a play:

- **`unlock`** — load the key at the start (`pre_tasks`)
- **`lock`** — remove it at the end (`post_tasks`)

`run_once` + `delegate_to: localhost` are baked into both, so call them bare.

## ssh-agent in 30 seconds (read this first)

`ssh-agent` is a small background program that holds private keys **in memory**. When
you run `ssh` (or Ansible does), ssh doesn't read your key itself — it asks the agent
"can you sign this for me?". That's why a key loaded into the agent works without any
key file on disk.

**How does `ssh` find the agent?** Through a **socket** — a special file the agent
creates (something like `/tmp/ssh-XXXX/agent.123`). The path to that file lives in the
environment variable **`SSH_AUTH_SOCK`**. Every tool in the SSH family (`ssh`,
`ssh-add`, `scp`, Ansible's connections) reads that variable to know where the agent
is. No `SSH_AUTH_SOCK`, no agent — that's the whole mechanism.

So for this role, "the sock" is simply **the address of the agent**:

- If your shell already has an agent running, the play inherits `SSH_AUTH_SOCK`
  automatically and the role uses it without touching anything.
- If there's **no** agent, the role spawns one. A brand-new agent means a brand-new
  socket path that only the role knows — so it has to (a) hand that path to every later
  `ssh-add` it runs, (b) hand it to the play's own SSH connections, and (c) print it
  for you. That's the **only** reason a socket variable (`ssh_agent_key_sock`) exists
  in this role.

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
        ssh_agent_key_passphrase: "{{ vault_ssh_key_passphrase }}"   # only if the key has one
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
| `ssh_agent_key_content` | `""` | **Required (unlock).** Private key text, from a vaulted var. |
| `ssh_agent_key_passphrase` | `""` | Passphrase to unlock the key, if it has one. From a vaulted var. |
| `ssh_agent_key_lifetime` | `0` | Seconds; `0` = until locked/agent stops, `>0` = `ssh-add -t` auto-expiry. |
| `ssh_agent_key_public` | `""` | **lock only.** Public key line, to remove a key unlock didn't add. |
| `ssh_agent_key_sock` | *output* | Set by unlock **only when it spawned an agent** — that agent's socket path. |

## What happens, step by step

### unlock

1. **Check the key was provided.** If `ssh_agent_key_content` is empty you get a clear
   "wire your vaulted variable" message, instead of a cryptic `ssh-add` error later.

2. **List what's in the agent right now** (`ssh-add -L`). Two later steps compare
   against this "before" picture. If there's no agent at all, that's fine here — the
   next step deals with it.

3. **Try to add the key** (`ssh-add -`). The private key is piped in over **stdin** —
   it never becomes a file and never appears in a process list. If an agent was
   inherited from your shell, the key lands there and we're done.

   **If the key has a passphrase:** `ssh-add` has no terminal to prompt on, so it uses
   ssh's built-in fallback — it runs the program named in the `SSH_ASKPASS` env var
   and takes its output as the answer. Ours is `files/askpass.sh`, three lines that
   echo `$SSH_AGENT_KEY_PASSPHRASE` — a variable the task places in **ssh-add's own
   process environment**, filled from your vaulted `ssh_agent_key_passphrase`. The
   helper contains no secret, the role reads nothing from your shell's environment,
   and with a raw key it is never even called. (It also refuses `ssh-add`'s "try
   again" re-prompt, so a wrong passphrase fails in a second instead of looping
   forever.)

   **Rescue — only runs if that failed** (usually: no agent running):

   - **Spawn an agent** (`ssh-agent -s`). This agent is a normal background process —
     it **keeps running after the play finishes**.
   - **Save its socket path** in `ssh_agent_key_sock`. Remember: a new agent has a new
     address, and nothing else on the system knows it yet. Every later `ssh-add` in
     the role uses this variable to talk to the right agent.
   - **Add the key again**, this time pointed at the new agent.
   - **Tell the play's SSH connections about it.** This is the subtle one: when
     Ansible SSHes to your hosts, those ssh processes get `ansible-playbook`'s
     environment — **not** the environment of a task. So they'd never see the new
     agent on their own, and the key would be loaded somewhere the play can't use.
     The role appends `-o IdentityAgent=<sock>` to `ansible_ssh_common_args`, which
     tells ssh explicitly which agent to ask.
   - **Print the reuse line** — `export SSH_AUTH_SOCK=<sock>`. Paste that in your
     terminal and your shell (and your next ansible run) will find the same agent
     instead of spawning another.

4. **List the agent's keys again.** Adding a key that's already loaded is a harmless
   no-op (the agent de-duplicates), so the honest `changed` signal is: did the key
   list actually change? First run shows `changed`, re-runs show `ok`.

5. **Remember what we added.** Agents delete keys *by public key*, not by name. The
   one new line between the "before" and "after" listings is the public key of what
   we just loaded — it's saved so lock knows exactly what to remove.

### lock

6. **Remove that key** — the saved public line is piped to `ssh-add -d /dev/stdin`
   (again: nothing on disk). If unlock didn't add anything — the key was already in
   the agent before the play, or unlock never ran — the task **skips**, because lock
   only cleans up what unlock loaded. To remove a key that came from somewhere else,
   pass its public line in `ssh_agent_key_public`. A spawned agent is left running;
   only the key is taken out of it.

## Notes

- **Passphrase-protected keys** need `ssh_agent_key_passphrase` set (from a vaulted
  var) — without it, `ssh-add` fails immediately (no terminal to ask on). Needs
  OpenSSH ≥ 8.4 (`SSH_ASKPASS_REQUIRE`); RHEL 9 ships 8.7.
- **State is play-scoped.** unlock remembers what it added in a host fact on the
  play's first host — run unlock and lock in the *same play* (the pattern above).
- **`IdentitiesOnly yes` defeats the agent.** If `~/.ssh/config` pins
  `IdentityFile` + `IdentitiesOnly yes` for a host, ssh ignores agent keys. Verify
  with `ssh -v <host>`: an agent key offers as `... agent`, an on-disk key as
  `... explicit`.
- Set `ansible_pipelining: true` if you also want Ansible's own module payloads off
  disk; the key itself never touches disk either way.
- One key per unlock/lock pair. Call the role once per key for several.
