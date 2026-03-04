# Ansible Vault Env Unlock

Unlock Ansible Vault using an environment variable instead of storing the password on disk.

## Setup

### 1. Add the shell function to your shell profile

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
# Set the password for the current shell session (hidden input)
vault-unlock

# Run playbooks as normal — vault decryption is automatic
ansible-playbook playbooks/my-playbook.yml
```

## How It Works

- `vault-unlock` prompts for a password with hidden input and exports it as `VAULT_PASSWORD`
- `ansible.cfg` points `vault_password_file` at `vault-env-client.sh`
- Ansible calls the script, which echoes `$VAULT_PASSWORD` back
- If the variable is unset, the script exits non-zero with a clear error message

## Notes

- The password lives only in the shell session's environment — not on disk
- Closing the terminal clears the variable
- Works with `ansible-playbook`, `ansible-vault view/edit/encrypt/decrypt`, and any Ansible command that reads `ansible.cfg`
