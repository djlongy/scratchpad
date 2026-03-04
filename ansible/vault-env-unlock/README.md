# Ansible Environment Unlock Functions

Shell functions to set credentials via environment variables instead of storing them on disk. Prompts once per session with hidden input.

## Ansible Vault (`unlock-ansible`)

### Setup

#### 1. Add the shell function to your shell profile

The `unlock-ansible` function prompts for the password once and caches it in `VAULT_PASSWORD` for the rest of your shell session. This is the recommended approach — it avoids re-prompting on every playbook run and ensures a wrong password is caught early on the first run rather than silently cached.

**Zsh** (`~/.zshrc`):

```zsh
unlock-ansible() {
  read -rs 'pw?Vault Password: '
  echo
  export VAULT_PASSWORD="$pw"
  echo "VAULT_PASSWORD set for this shell session"
}
```

**Bash** (`~/.bashrc`):

```bash
unlock-ansible() {
  read -rsp 'Vault Password: ' pw
  echo
  export VAULT_PASSWORD="$pw"
  echo "VAULT_PASSWORD set for this shell session"
}
```

#### 2. Copy the password script into your Ansible project

```bash
cp vault-env-client.sh /path/to/your/ansible/scripts/
chmod +x /path/to/your/ansible/scripts/vault-env-client.sh
```

#### 3. Update `ansible.cfg`

```ini
[defaults]
vault_password_file = scripts/vault-env-client.sh
```

### Usage

```bash
# Set the password once per session (hidden input)
unlock-ansible

# Run as many playbooks as needed — no further prompts
ansible-playbook playbooks/first.yml
ansible-playbook playbooks/second.yml
```

If you forget to run `unlock-ansible`, the script will prompt interactively as a fallback. However, this prompts on every playbook run since the password can't be cached back into the parent shell from a subprocess.

### How It Works

- `unlock-ansible` prompts once and exports `VAULT_PASSWORD` for the shell session
- `ansible.cfg` points `vault_password_file` at `vault-env-client.sh`
- Ansible calls the script, which echoes `$VAULT_PASSWORD` back
- If the variable is unset, the script falls back to an interactive prompt via `/dev/tty`

---

## VMware Dynamic Inventory (`unlock-vmware`)

### Setup

Add the shell function to your shell profile. Host and username have defaults — only the password is prompted.

**Zsh** (`~/.zshrc`):

```zsh
unlock-vmware() {
  export VMWARE_HOST="${VMWARE_HOST:-vcsa01.yourdomain.local}"
  export VMWARE_USER="${VMWARE_USER:-ansible@vsphere.local}"
  read -rs 'pw?vCenter Password: '
  echo
  export VMWARE_PASSWORD="$pw"
  echo "VMWARE_HOST=$VMWARE_HOST  VMWARE_USER=$VMWARE_USER  VMWARE_PASSWORD=set"
}
```

**Bash** (`~/.bashrc`):

```bash
unlock-vmware() {
  export VMWARE_HOST="${VMWARE_HOST:-vcsa01.yourdomain.local}"
  export VMWARE_USER="${VMWARE_USER:-ansible@vsphere.local}"
  read -rsp 'vCenter Password: ' pw
  echo
  export VMWARE_PASSWORD="$pw"
  echo "VMWARE_HOST=$VMWARE_HOST  VMWARE_USER=$VMWARE_USER  VMWARE_PASSWORD=set"
}
```

### Usage

```bash
# Set vCenter credentials once per session
unlock-vmware

# Use VMware dynamic inventory
ansible-inventory -i plugins/inventory/vmware_vms.yml --list
ansible-playbook -i plugins/inventory/vmware_vms.yml playbooks/my-playbook.yml

# Override host or user for a different vCenter
VMWARE_HOST=vcsa02.yourdomain.local unlock-vmware
```

### How It Works

- `VMWARE_HOST` and `VMWARE_USER` use defaults if not already set (override by exporting before calling)
- Only the password is prompted interactively
- The `vmware.vmware.vms` inventory plugin reads all three env vars automatically

---

## Notes

- Credentials live only in the shell session's environment — not on disk
- Closing the terminal clears all variables
- Run both functions to unlock Ansible Vault and VMware in one session:
  ```bash
  unlock-ansible && unlock-vmware
  ```
