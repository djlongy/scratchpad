# ssh_agent_key

## TL;DR

Unlocks/locks a private SSH key in `ssh-agent` straight from memory — the
key is fed to `ssh-add` over stdin, so it never touches disk, never appears
on a command line, and (`no_log`) never appears in task output. The key
lives encrypted in Ansible Vault; if it also has a passphrase, set
`ssh_agent_key_passphrase` and the role unlocks it with that. `unlock` and
`lock` are fully independent entry points — each just takes the same
vaulted key.

```bash
ansible-playbook -i inventories/<env>/hosts.yml playbooks/load_ssh_agent_key.yml
```

## Requirements

None beyond `ansible.builtin`.

## Key variables

Full list: `defaults/main.yml`. Contract: `meta/argument_specs.yml`.

**Required** = value must be correct for a successful run (defaults often work).
**Optional** = safe to leave default / empty; phase stays off or uses built-ins.
**When X** = required only if that feature is on.

| Req | Variable | Default | Purpose |
|---|---|---|---|
| **Required** | `ssh_agent_key_content` | `""` | Private key text, from a vaulted var (unlock and lock, unless `ssh_agent_key_public` is set for lock) |
| When passphrase | `ssh_agent_key_passphrase` | `""` | Passphrase to unlock the key, if it has one |
| Optional | `ssh_agent_key_lifetime` | `0` | Seconds; `0` = until locked/agent stops, `>0` = `ssh-add -t` auto-expiry (unlock only) |
| Optional | `ssh_agent_key_public` | `""` | Explicit public line to remove/check — skips in-memory derivation, no private key needed for lock |
| Optional (output) | `ssh_agent_key_sock` | *unset* | Set by unlock only when it spawned an agent — that agent's socket path |

## Usage

Bracketing one play:

```yaml
- name: Deploy with the vaulted key unlocked for the duration of the play
  hosts: web
  gather_facts: false            # facts would need SSH before the key is unlocked
  pre_tasks:
    - name: Unlock the deploy key
      ansible.builtin.import_role:
        name: ssh_agent_key
        tasks_from: unlock
      vars: &deploy_key
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
      vars: *deploy_key
```

Run it:

```bash
ansible-playbook -i inventories/<env>/hosts.yml site.yml --ask-vault-pass
```

Load-only also works: `roles: [ssh_agent_key]` defaults to unlock. For a
standalone lock in its own playbook, run `tasks_from: lock` on `localhost`.

## Preconditions

- Passphrase-protected keys need OpenSSH ≥ 8.4 (`SSH_ASKPASS_REQUIRE`) on
  the control node — RHEL 9 ships 8.7.

## Behaviour

- **Agent discovery.** If the play's shell already has an agent running,
  the role inherits `SSH_AUTH_SOCK` and uses it. If there's none, the role
  spawns one (`ssh-agent -s`, persists after the play), saves its socket in
  `ssh_agent_key_sock`, and appends `-o IdentityAgent=<sock>` to
  `ansible_ssh_common_args` so the play's own SSH connections can reach it.
- **Unlock** pipes the private key to `ssh-add -` over stdin. A passphrase
  is supplied via `SSH_ASKPASS`, never via a prompt or the shell
  environment. Idempotent: a key already loaded (checked via `ssh-add -L`)
  reports `ok`, not `changed`.
- **Lock has no saved state.** The public half of a key pair is derivable
  from the private half, so lock re-derives it in memory (via the bundled
  `ssh_agent_key_pubkey` filter, or directly from `ssh_agent_key_public` if
  you pass it) and pipes it to `ssh-add -d /dev/stdin` — same input as
  unlock, opposite action.
- To lock a spawned agent in a later run, export the printed
  `SSH_AUTH_SOCK` line first (or pass `ssh_agent_key_sock` as a var) — the
  play only knows a spawned agent's address while it's running.
- **Known failure mode:** if `~/.ssh/config` pins `IdentityFile` +
  `IdentitiesOnly yes` for a host, ssh ignores agent keys entirely. Verify
  with `ssh -v <host>`: an agent key offers as `... agent`, an on-disk key
  as `... explicit`.
- Set `ansible_pipelining: true` if you also want Ansible's own module
  payloads off disk; the key itself never touches disk either way.
- One key per unlock/lock call — call the role once per key for several.
