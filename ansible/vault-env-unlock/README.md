# Ansible Vault Env Unlock

Unlock Ansible Vault using an environment variable instead of storing the password on disk.

## Setup

### 1. Add the shell function to your shell profile

The `vault-unlock` function prompts for the password once and caches it in `VAULT_PASSWORD` for the rest of your shell session. This is the recommended approach — it avoids re-prompting on every playbook run and ensures a wrong password is caught early on the first run rather than silently cached.

**Zsh** (`~/.zshrc`):

```zsh
vault-unlock() {
  read -rs 'pw?Vault Password: '
  echo
  export VAULT_PASSWORD="$pw"
  echo "VAULT_PASSWORD set for this shell session"
}
```

**Bash** (`~/.bashrc`):

```bash
vault-unlock() {
  read -rsp 'Vault Password: ' pw
  echo
  export VAULT_PASSWORD="$pw"
  echo "VAULT_PASSWORD set for this shell session"
}
```

Reload your shell: `source ~/.zshrc` or `source ~/.bashrc`

### 2. Copy the password script into your Ansible project

```bash
cp vault-env-client.sh /path/to/your/ansible/scripts/
chmod +x /path/to/your/ansible/scripts/vault-env-client.sh
```

### 3. Update `ansible.cfg`

```ini
[defaults]
vault_password_file = scripts/vault-env-client.sh
```

## Usage

```bash
# Set the password once per session (hidden input)
vault-unlock

# Run as many playbooks as needed — no further prompts
ansible-playbook playbooks/first.yml
ansible-playbook playbooks/second.yml
```

If you forget to run `vault-unlock`, the script will prompt interactively as a fallback. However, this prompts on every playbook run since the password can't be cached back into the parent shell from a subprocess.

## How It Works

- `vault-unlock` prompts once and exports `VAULT_PASSWORD` for the shell session
- `ansible.cfg` points `vault_password_file` at `vault-env-client.sh`
- Ansible calls the script, which echoes `$VAULT_PASSWORD` back
- If the variable is unset, the script falls back to an interactive prompt via `/dev/tty`

## Notes

- The password lives only in the shell session's environment — not on disk
- Closing the terminal clears the variable
- Works with `ansible-playbook`, `ansible-vault view/edit/encrypt/decrypt`, and any Ansible command that reads `ansible.cfg`
