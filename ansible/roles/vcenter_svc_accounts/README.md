# vcenter_svc_accounts

Provisions dedicated, **least-privilege vCenter SSO service accounts** with scoped
custom Roles (RBAC), so automation stops authenticating as
`administrator@vsphere.local`. Data-driven: describe the accounts you want in
`vcenter_service_accounts` and the role converges them.

## TL;DR

Edit the `vcenter_service_accounts` list (accounts + their privilege sets) and
re-run — users are created once (never rotated on re-run), the Role + grant
re-converge every run. Runs as a `localhost` play.

```bash
ansible-playbook -i inventories/<env>/hosts.yml playbooks/vcenter_svc_accounts.yml
```

## Requirements

Install collections before running (repo `requirements.yml`, or ad-hoc):

    ansible-galaxy collection install -r requirements.yml

| Collection | When | Used for |
|---|---|---|
| `community.vmware` | always | Custom Role (RBAC) + permission grant |
| `community.hashi_vault` | always | Writing generated credentials to Vault |
| `community.general` | always | Generating the account password (`random_string`) |

Also requires the **`sshpass`** binary on the controller — `dir-cli` runs over
SSH to the VCSA appliance.

## Key variables

Full list: `defaults/main.yml`. Contract: `meta/argument_specs.yml`.

**Required** = value must be correct for a successful run (defaults often work).
**Optional** = safe to leave default / empty; phase stays off or uses built-ins.
**When X** = required only if that feature is on.

| Req | Variable | Default | Purpose |
|---|---|---|---|
| **Required** | `vcenter_svc_hostname` | `""` | vCenter REST/SOAP endpoint |
| **Required** | `vcenter_svc_admin_username` | `""` | SSO administrator (e.g. `administrator@vsphere.local`) |
| **Required** | `vcenter_svc_admin_password` | `""` | SSO administrator password |
| **Required** | `vcenter_svc_vcsa_ssh_password` | `""` | VCSA appliance SSH password (`dir-cli` runs over SSH) |
| **Required** | `vcenter_service_accounts` | 2 built-in accounts | The accounts to provision: `{account, role_name, privileges, description?}` |
| Optional | `vcenter_svc_vcsa_ssh_user` | `root` | VCSA appliance SSH user |
| Optional | `vcenter_svc_validate_certs` | `false` | Verify the vCenter API TLS certificate |
| Optional | `vcenter_svc_vault_addr` | `""` | HashiCorp Vault URL the generated passwords are written to |
| Optional | `vcenter_svc_vault_mount` | `secret` | Vault KV v2 mount point |
| Optional | `vcenter_svc_vault_path_prefix` | `""` | Vault path prefix — secret lands at `<mount>/<prefix>/<account>` |
| Optional | `vcenter_svc_permission_recursive` | `true` | Propagate the Role grant down the inventory hierarchy |
| Optional | `vcenter_svc_privilege_action` | `set` | Converge the Role to exactly the entry's privilege list |
| Optional | `vcenter_svc_password_length` | `20` | Generated password length (capped — vmdir rejects longer) |

## Usage

```yaml
# playbooks/vcenter_svc_accounts.yml
- hosts: localhost
  gather_facts: false
  roles:
    - vcenter_svc_accounts
```

```yaml
# group_vars / play vars — supply credentials from a secret store, never hardcode
vcenter_svc_hostname: "vcenter.example.com"
vcenter_svc_admin_username: "administrator@vsphere.local"
vcenter_svc_admin_password: "{{ lookup('community.hashi_vault.hashi_vault', 'secret=...') }}"
vcenter_svc_vcsa_ssh_user: "root"
vcenter_svc_vcsa_ssh_password: "{{ lookup('community.hashi_vault.hashi_vault', 'secret=...') }}"
vcenter_svc_vault_addr: "https://vault.example.com:8200"
vcenter_svc_vault_mount: "secret"
vcenter_svc_vault_path_prefix: "vsphere/vcenter"
```

Run it:

```bash
ansible-playbook -i inventories/<env>/hosts.yml playbooks/vcenter_svc_accounts.yml
```

After a run, each account's credentials are at
`<mount>/<prefix>/<account>` (e.g. `secret/vsphere/vcenter/svc-ansible`) with
`username` and `password` fields.

Two ready-made privilege sets ship in `defaults/main.yml`: `vcenter_priv_automation`
(full VM + network + datastore + resource + folder lifecycle, excluding
`Authorization.*`, `Certificate.*`, licensing, host add/remove, and SSO/user
admin) and `vcenter_priv_inventory` (read-only). Add an account by extending
`vcenter_service_accounts`:

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

## Preconditions

- SSH is enabled on the VCSA appliance — `dir-cli` runs there over SSH; the
  role does not enable it.
- vCenter SSO administrator credentials are valid and reachable.
- A reachable HashiCorp Vault with an existing KV v2 mount and a valid token
  (`VAULT_TOKEN` or `~/.vault-token`).

## Behaviour

1. Creates a local SSO user via `dir-cli` over VCSA SSH — created **once**,
   never rotated on re-run.
2. Writes the generated password to Vault *before* the account is usable, so
   a live credential never exists without being recoverable.
3. Converges a custom vCenter Role to *exactly* the entry's privilege list.
4. Grants that Role to the principal at the vCenter root (recursive by
   default — `vcenter_svc_permission_recursive`).

- **Password length is capped at 20** — the vSphere SSO / vmdir password
  policy rejects longer passwords (`LDAP error: Constraint violation`).
- **Re-runs report the two RBAC modules as `changed`.**
  `vmware_local_role_manager` and `vmware_object_role_permission` re-assert on
  every run — cosmetic, harmless. User creation correctly skips when the
  account already exists.
- Every task touching a credential sets `no_log: true` — secrets are never
  printed.
