# Ansible Vault Env Unlock

Unlock Ansible Vault using an environment variable instead of storing the password on disk.

## Setup

### 1. (Optional) Add the shell function to your shell profile

The script will prompt for the password automatically if `VAULT_PASSWORD` is not set, so this step is optional. Use it if you want to enter the password once and reuse it across multiple playbook runs in the same session.

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
# Option A: Just run a playbook — you'll be prompted for the password
ansible-playbook playbooks/my-playbook.yml

# Option B: Set the password once, reuse across multiple runs
vault-unlock
ansible-playbook playbooks/first.yml
ansible-playbook playbooks/second.yml   # no prompt
```

## How It Works

- `ansible.cfg` points `vault_password_file` at `vault-env-client.sh`
- Ansible calls the script, which echoes `$VAULT_PASSWORD` back
- If the variable is unset, the script prompts interactively via `/dev/tty`
- The optional `vault-unlock` function pre-sets the env var so you're only prompted once per session

## Notes

- The password lives only in the shell session's environment — not on disk
- Closing the terminal clears the variable
- Works with `ansible-playbook`, `ansible-vault view/edit/encrypt/decrypt`, and any Ansible command that reads `ansible.cfg`
