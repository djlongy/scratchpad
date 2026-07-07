# ssh_agent_key

Unlock/lock a private SSH key in `ssh-agent` **straight from memory** — the key is fed
to `ssh-add` over stdin, so it never touches disk, never appears in `argv`, and (with
`no_log`) never appears in task output. Linux-first: plain OpenSSH agent semantics, no
platform quirks, no `expect`/`pexpect` dependency.

Two entry points, designed to bracket a play:

- **`unlock`** — load the key at the start of the play (`pre_tasks`)
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

Standalone (load only — `roles:` defaults to unlock):

```yaml
- hosts: localhost
  connection: local
  gather_facts: false
  vars:
    ansible_pipelining: true     # keeps Ansible's own module payload off ~/.ansible/tmp
  roles:
    - role: ssh_agent_key
      vars:
        ssh_agent_key_content: "{{ vault_deploy_key }}"
```

## Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `ssh_agent_key_content` | `""` | **Required (unlock).** Private key text (PEM / OpenSSH). Wire from a vaulted var. |
| `ssh_agent_key_passphrase` | `""` | Passphrase for an encrypted key. Env-fed to an `SSH_ASKPASS` helper — never on disk/argv. |
| `ssh_agent_key_auth_sock` | `$SSH_AUTH_SOCK` | Agent socket to use. Unreachable → a throwaway agent is spawned for the play. |
| `ssh_agent_key_lifetime` | `0` | Seconds; `0` = until locked/agent restart, `>0` = `ssh-add -t` auto-expiry. |
| `ssh_agent_key_public` | `""` | **lock only.** Public key line to remove when unlock didn't run in this play. |

## How it works

- **unlock** first loads the key into a short-lived **probe agent** — this validates the
  key + passphrase without touching your real agent, and captures the public key that
  `lock` later deletes by (`ssh-keygen -y` can't read a pipe, so a probe agent is the
  only disk-free way to derive it). The probe agent is killed in `always`.
- **Inherited agent first, spawn as rescue.** If `SSH_AUTH_SOCK` points at a reachable
  agent, the key goes there (Ansible's SSH connections inherit that socket and just
  work). If not — headless/CI — a throwaway agent is spawned and the play's connections
  are pointed at it via `ansible_ssh_common_args` `-o IdentityAgent=...`.
- **Passphrases without expect.** An encrypted key is unlocked via a static
  `SSH_ASKPASS` helper script that echoes an environment variable — the helper contains
  no secret, and the passphrase exists only in the task's process environment.
  `SSH_ASKPASS_REQUIRE=force` needs OpenSSH ≥ 8.4 (RHEL 9 ships 8.7); `DISPLAY` is also
  set so older releases take the askpass path too (no TTY under Ansible).
- **Idempotent.** Re-adding a present key is a no-op (the agent dedupes); unlock reports
  `changed` only when the key's blob wasn't in the agent before. lock treats
  already-gone as ok.
- **lock** kills the spawned agent if unlock created one; otherwise it deletes exactly
  the key unlock loaded (`ssh-add -d /dev/stdin` with the captured public key — public
  material, and still nothing on disk).

## Notes

- `unlock`/`lock` state is carried in host facts on the play's first host — run both in
  the **same play** (the `pre_tasks`/`post_tasks` pattern above). `lock` in a different
  play needs `ssh_agent_key_public`.
- **`IdentitiesOnly yes` defeats the agent.** If `~/.ssh/config` pins
  `IdentityFile` + `IdentitiesOnly yes` for a host, ssh ignores agent keys. Verify with
  `ssh -v <host>`: an agent key offers as `... agent`, an on-disk key as `... explicit`.
- Set `ansible_pipelining: true` (or enable pipelining globally) if you want Ansible's
  own module payloads off disk too — the key itself never touches disk either way.
- One key per unlock/lock pair. Call the role once per key for several.
