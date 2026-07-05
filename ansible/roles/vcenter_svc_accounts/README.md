# vcenter_svc_accounts

Provision dedicated, **least-privilege vCenter SSO service accounts** with scoped
custom Roles (RBAC), so your automation stops authenticating as
`administrator@vsphere.local`. Data-driven: describe the accounts you want in
`vcenter_service_accounts` and the role converges them.

For each entry it:

1. **creates a local SSO user** via `dir-cli` over VCSA SSH — created **once**, never
   rotated on re-run;
2. **writes the generated password to HashiCorp Vault** *before* the account is usable,
   so a live credential never exists without being recoverable from Vault;
3. **converges a custom vCenter Role** to *exactly* the entry's privilege list; and
4. **grants that Role to the principal at the vCenter root** (recursive).

Idempotent: users are created once; the Role + permission grant re-converge every run.

## Why

The SSO administrator is all-powerful and shared. Automation should hold only the
privileges it actually needs. This role ships two ready-made, least-privilege sets:

| Account | Role | Scope |
|---|---|---|
| `svc-ansible` | `svc-automation` | Full VM + network + datastore + resource + folder lifecycle. Excludes `Authorization.*`, `Certificate.*`, licensing, host add/remove, and SSO/user admin. |
| `svc-inventory` | `svc-readonly-inventory` | Read-only (`System.*` is implicit) + `VirtualMachine.GuestOperations.Query` for inventory grouping on guest facts. |

Tune or add accounts by editing `vcenter_service_accounts` and the `vcenter_priv_*`
privilege lists in `defaults/main.yml` (or override them from inventory).

## Requirements

- **Controller:** `community.vmware` + `pyvmomi`, `community.hashi_vault`,
  `community.general` (for `random_string`), and the **`sshpass`** binary.
- **vCenter:** SSO administrator credentials (to manage Roles/permissions and run
  `dir-cli`) and **SSH enabled on the VCSA appliance** (`dir-cli` lives on the
  appliance, so user creation is driven over SSH).
- **Vault:** a reachable HashiCorp Vault with a KV v2 mount; a token available via
  `VAULT_TOKEN` or `~/.vault-token`. The generated passwords are written here.

## Usage

Run on the control node (a `localhost` play):

```yaml
# provision_vcenter_svc_accounts.yml
- hosts: localhost
  gather_facts: false
  roles:
    - vcenter_svc_accounts
```

```yaml
# group_vars / play vars — supply credentials from a secret store, never hardcode
vcenter_svc_hostname: "vcenter.example.com"
vcenter_svc_admin_username: "administrator@vsphere.local"
vcenter_svc_admin_password: "{{ lookup('community.hashi_vault.hashi_vault',
    'secret=secret/data/vsphere/admin:password') }}"
vcenter_svc_vcsa_ssh_user: "root"
vcenter_svc_vcsa_ssh_password: "{{ lookup('community.hashi_vault.hashi_vault',
    'secret=secret/data/vsphere/vcsa-ssh:password') }}"

# where generated svc-account passwords are stored (KV v2)
vcenter_svc_vault_addr: "https://vault.example.com:8200"
vcenter_svc_vault_mount: "secret"
vcenter_svc_vault_path_prefix: "vsphere/vcenter"
```

```bash
ansible-playbook provision_vcenter_svc_accounts.yml
```

After a run, each account's credentials are at
`<mount>/<prefix>/<account>` (e.g. `secret/vsphere/vcenter/svc-ansible`) with
`username` and `password` fields — point your other automation at those.

## Adding an account

```yaml
vcenter_service_accounts:
  - account: svc-backup
    role_name: svc-backup
    description: "Snapshot-only backup service account"
    privileges:
      - VirtualMachine.State.CreateSnapshot
      - VirtualMachine.State.RemoveSnapshot
      - VirtualMachine.Provisioning.DiskRandomRead
```

## Notes & gotchas

- **Password length is capped at 20.** The vSphere SSO / vmdir password policy rejects
  passwords longer than 20 characters (`LDAP error: Constraint violation`). 20
  mixed-class characters is ample.
- **`dir-cli` reads the admin password from `/dev/tty`, not stdin.** The role supplies
  it non-interactively with `--password` (kept out of logs via `no_log`). Piping the
  password to stdin is silently ignored.
- **Re-runs report the two RBAC modules as `changed`.** `vmware_local_role_manager` and
  `vmware_object_role_permission` re-assert on every run — cosmetic, harmless. User
  creation correctly skips when the account already exists.
- **Secrets never printed.** Every task touching a credential sets `no_log: true`; the
  password is written to Vault before the account is created.

## Variables

See `defaults/main.yml` for the full surface (target/credential vars, Vault target,
RBAC behaviour, password shape, the account list, and the `vcenter_priv_*` privilege
sets) and `meta/argument_specs.yml` for the typed contract.
