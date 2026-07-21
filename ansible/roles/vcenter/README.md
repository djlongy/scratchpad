# vcenter

## TL;DR

Configures VMware **vCenter**: datacenter/cluster, ESXi host registration,
config-as-code **VDS + dvPortGroups**, service accounts, optional **FreeIPA /
OpenLDAP identity source**, optional **ESXi host day-2** (NTP/DNS/syslog/SSH/VSS
via the vCenter API), and optional **VCSA TLS** (REST deploy; ACME+Cloudflare is
one stripable package). Every capability is a boolean gate — air-gapped estates
leave certs/LDAP off or delete `tasks/optional/acme_cloudflare/`.

```bash
ansible-playbook -i inventories/<env>/hosts.yml playbooks/10_vm_vcenter.yml --tags topology
```

## Requirements

Install collections before running (repo `requirements.yml`, or ad-hoc):

    ansible-galaxy collection install -r requirements.yml

| Collection | When | Used for |
|---|---|---|
| `community.vmware` | always | topology, host register, host day-2 |
| `community.hashi_vault` | When `vcenter_use_hashicorp_vault` | credential fallback |
| `community.general` | When `svc_accounts` | `random_string` for passwords |
| `community.crypto` | When certs + ACME package | ACME issue |
| `community.general` | When certs + ACME package | `cloudflare_dns` DNS-01 |

## Key variables

Full list: `defaults/main.yml`.

**Required** = value must be correct. **Optional** = safe at default.
**When X** = only if that gate/feature is on.

| Req | Variable | Default | Purpose |
|---|---|---|---|
| **Required** | `vcenter_hostname` | `""` | vCenter FQDN |
| **Required** | `vcenter_datacenter` / `vcenter_cluster` | `""` | Legacy bootstrap names |
| Optional | `vcenter_manage_topology` | `true` | Master gate for `vcenter_config` apply |
| Optional | `vcenter_manage_vds` / `vcenter_manage_portgroups` | `true` | Sub-gates for VDS / dvPortGroups |
| Optional | `vcenter_manage_svc_accounts` | `true` | Custom roles + permission grants |
| Optional | `vcenter_svc_create_local_sso_users` | `false` | dir-cli local SSO create (needs VCSA SSH) |
| Optional | `vcenter_manage_ldap` | **`false`** | FreeIPA/OpenLDAP SSO identity source |
| When LDAP | `vcenter_ldap_domain` / `_alias` / `_primary_url` / bind | `""` | Directory + bind DN |
| When LDAP | `vcenter_ldap_use_ssl` + `_ssl_cert_files` | `false` / `[]` | LDAPS + CA PEMs |
| Optional | `vcenter_host_ssh_enabled` | `false` | ESXi TSM-SSH desired state |
| Optional | `vcenter_vcsa_ssh_enabled` | `false` | VCSA bash shell / SSH |
| Optional | `vcenter_manage_host_ntp` (and `_dns`/`_ssh`/…) | `false` | ESXi day-2 via vCenter API |
| Optional | `vcenter_manage_certs` | **`false`** | Load cert phase at all |
| Optional | `vcenter_cert_replace` | **`false`** | Allow Machine SSL mutation |
| When topology | `vcenter_config` | `{}` | Declarative VDS/portgroups/hosts/… |
| When host_* | `vcenter_esxi_hosts` | `[]` | Host list (`hostname` keys) |
| When replace | `vcenter_cert_deploy_method` | `rest` | `rest` or `vecs_ssh` |
| When replace | `vcenter_cert_provider` | `files` | `files` or `csr` |
| When ACME package | `vcenter_cert_issue_acme` | `false` | ACME+Cloudflare unit |
| When ACME package | `vcenter_acme_email` / `vcenter_cloudflare_zone` | `""` | LE account + DNS zone |

### Feature gates (ship / air-gap)

| Gate | Default | What it enables |
|---|---|---|
| `vcenter_manage_datacenter` | true | Legacy datacenter/cluster tasks |
| `vcenter_manage_esxi_register` | true | Add ESXi hosts to vCenter |
| `vcenter_manage_svc_accounts` | true | Roles + object permissions (API) |
| `vcenter_svc_create_local_sso_users` | **false** | Local SSO users via dir-cli (needs VCSA SSH) |
| `vcenter_manage_ldap` | **false** | FreeIPA/OpenLDAP identity source (`sso-config`) |
| `vcenter_manage_tenancy` | **false** | Soft multi-tenancy (folders + RPs + RBAC) |
| `vcenter_manage_topology` | true | Config-as-code pipeline |
| `vcenter_manage_vds` | true | Distributed switches |
| `vcenter_manage_portgroups` | true | Distributed port groups |
| `vcenter_manage_host_*` | false | ESXi day-2 (NTP/DNS/…) |
| `vcenter_host_ssh_enabled` | **false** | ESXi SSH service on |
| `vcenter_vcsa_ssh_enabled` | **false** | VCSA appliance SSH on |
| `vcenter_manage_certs` | **false** | Load cert phase |
| `vcenter_cert_replace` | **false** | Mutate Machine SSL |

### Work-safe TLS contract

**Safe default for an existing vCenter (self-signed / manual cert):** leave both
gates false. Topology, export, VDS, hosts, and svc accounts never read cert
vars and never call the TLS replace API.

| `manage_certs` | `cert_replace` | Behaviour |
|---|---|---|
| `false` | (ignored) | Cert phase not loaded. Zero TLS impact. |
| `true` | `false` | Optional **report only** (GET current cert). No PUT/VECS/ACME. Missing PEM/ACME/Cloudflare vars **do not fail**. |
| `true` | `true` | May replace when near expiry (or `cert_force_replace`). Requires material for the chosen provider. |

Self-signed / enterprise certs are **not** force-replaced just for being
non-LE. Only expiry (or explicit `vcenter_cert_force_replace: true` + ACME
wanting LE) triggers mutation when replace is on.

### TLS cert deploy (when replace is enabled, vCenter 8.0.3+)

Default deploy path is **REST**, no VCSA SSH:

| Var | Default | Meaning |
|---|---|---|
| `vcenter_cert_deploy_method` | `rest` | `PUT …/certificate-management/vcenter/tls` |
| `vcenter_cert_provider` | `files` | PEMs on controller (`files`) or CSR on VCSA (`csr`) |
| `vcenter_cert_issue_acme` | **`false`** | Optional **ACME + Cloudflare** package |
| `vcenter_cert_file` / `_key_file` / `_root_file` | `""` | FreeIPA / pre-issued PEMs |
| `vcenter_cert_signed_file` | `""` | After signing a CSR, point here and re-run |
| `vcenter_cert_force_replace` | `false` | Replace even if not near expiry |
| `vcenter_cert_deploy_method: vecs_ssh` | — | Explicit VECS-CLI fallback (needs SSH) |

FreeIPA / private CA: `provider: csr`, sign in IDM, set signed + root paths,
then `manage_certs` + `cert_replace` true.

Public LE: optional package + `issue_acme: true` + email/zone + both gates.

### SSH security baseline

ESXi host SSH (`TSM-SSH`) and VCSA appliance SSH default **off**. Topology,
VDS, portgroups, folders, resource pools, role grants, and **default cert
deploy** use the **vCenter API only** — no host or appliance SSH.

If you must create local SSO users with dir-cli, use `vecs_ssh` cert deploy, or
add an LDAP identity source:

1. Temporarily enable SSH on the VCSA in the UI.
2. Set `vcenter_vcsa_ssh_enabled: true` and provide `vcenter_vcsa_ssh_*` via
   ansible-vault.
3. Set only the feature you need for that run (`svc_create_local_sso_users`,
   `cert_deploy_method: vecs_ssh`, or `vcenter_manage_ldap`).
4. Disable SSH again when finished.

Prefer LDAP identity sources + `principal` / `group` permission grants so
appliance SSH stays off after the identity source is linked.

### FreeIPA / OpenLDAP identity source (`vcenter_manage_ldap`)

Default **false**. There is no community.vmware module for SSO identity sources;
the role uses VCSA `sso-config.sh -add_identity_source` over a temporary SSH
window (Broadcom KB 319662). FreeIPA is type `openldap`.

```bash
# Enable gates + vars in inventory, temporary VCSA SSH on, then:
ansible-playbook -i inventories/infra/hosts.yml playbooks/10_vm_vcenter.yml --tags ldap
# Then grant roles (API) once the source is live:
ansible-playbook … --tags svc_accounts
```

| Var | Default | Notes |
|---|---|---|
| `vcenter_manage_ldap` | **false** | Master gate |
| `vcenter_ldap_type` | `openldap` | or `adldap` |
| `vcenter_ldap_domain` / `_alias` | `""` | e.g. `idm.example.com` / `IDM` |
| `vcenter_ldap_primary_url` | `""` | `ldaps://…:636` preferred |
| `vcenter_ldap_base_user_dn` / `_group_dn` | `""` | Empty → FreeIPA layout from domain |
| `vcenter_ldap_bind_username` / `_password` | `""` | Full bind DN; password or Vault |
| `vcenter_ldap_use_ssl` | false | Sets `-useSSL true` + uploads PEMs |
| `vcenter_ldap_ssl_cert_files` | `[]` | Controller-local FreeIPA CA PEMs |
| `vcenter_ldap_force_recreate` | false | Delete domain then re-add |
| `vcenter_ldap_validate` | true | API probe via `vcenter_domain_user_group_info` |

Commented FreeIPA example: `inventories/infra/group_vars/vcenter/main.yml`.
After the source exists, leave `manage_ldap` false and use
`vcenter_service_accounts` with `principal` / `group` for RBAC (no SSH).

### Soft multi-tenancy (`vcenter_manage_tenancy`)

Work-style isolation on a shared VC (folders **and** resource pools):

| Object | Layout |
|---|---|
| VM folders | `Datacenter/vm/tenants/<tenant>/` |
| Resource pools | one pool named `<tenant>` under each `vcenter_tenancy_resource_pool_parents` entry (cluster or standalone host) |

| Principal set | Rights |
|---|---|
| `vcenter_platform_admins` | Admin @ `rootFolder` (full estate) |
| tenant `admin_principals` | Admin @ that tenant’s folder **and** each of that tenant’s RPs |

**Multi-tenant users:** list the same principal under multiple tenants’  
`admin_principals` — they get Admin on each corresponding folder + RP  
(not root). FreeIPA OpenLDAP cannot grant to IDM *groups* on this VC;  
inventory expands group members to `IDM\uid` principals.

```bash
ansible-playbook -i inventories/infra playbooks/10_vm_vcenter.yml --tags tenancy
```

Inventory: `inventories/<env>/group_vars/vcenter/tenancy.yml`.

## Minimum configuration

```yaml
# group_vars/vcenter_hosts.yml
---
# Required
vcenter_hostname: service.example.internal
```

## Usage

```yaml
- name: Configure vCenter
  hosts: localhost
  gather_facts: false
  roles:
    - role: vcenter
```

Run:

```bash
export ANSIBLE_VAULT_PASSWORD=$(cat ~/secrets/vault-password.txt)
ansible-playbook -i inventories/<env>/hosts.yml playbooks/10_vm_vcenter.yml --tags topology
ansible-playbook -i inventories/<env>/hosts.yml playbooks/10_vm_vcenter.yml --tags vds,portgroups
```

## Air-gap / strip ACME + Cloudflare

**ACME and Cloudflare are one package** — they are not used for topology, VDS,
portgroups, host day-2, svc accounts, or FreeIPA/files/csr cert deploy.

### A. Config only (no code delete)

```yaml
vcenter_manage_certs: false        # default — no TLS phase at all
vcenter_cert_replace: false        # default — never mutate Machine SSL
vcenter_cert_issue_acme: false     # default — skip optional ACME package
```

### B. Surgical code strip (ship without ACME/Cloudflare at all)

Search for the banner `OPTIONAL ACME + CLOUDFLARE` and delete every match:

1. Delete directory `tasks/optional/acme_cloudflare/` (issue, DNS-01, trust
   roots, shell ref, LE PEMs).
2. Delete the `>>> BEGIN/END OPTIONAL ACME + CLOUDFLARE` block in
   `defaults/main.yml`.
3. Delete the matching block in `tasks/vault_auth.yml`.
4. Delete the matching block in `tasks/certs_main.yml`.
5. Drop ACME/Cloudflare rows from this README Requirements table.

Markers use the same banner text in every file so a single search finds them.

## Preconditions

- `vcenter_hostname` reachable; admin credentials via vars or Vault.
- Topology: `vcenter_config` referential integrity (portgroup → declared VDS).
- Host day-2: non-empty `vcenter_esxi_hosts` with names vCenter knows.
- Certs: when `vcenter_manage_certs`. ACME needs the optional package +
  `vcenter_cert_issue_acme: true` and email/zone.

## Behaviour

- Gates short-circuit whole phases; tags refine within an enabled phase.
- `vcenter_config` is declarative: apply converges declared objects only.
- Authoritative prune is `never` + `--tags reconcile` + `vcenter_authoritative`
  + non-empty `vcenter_reconcile_scope`.
- Host day-2 uses the same community.vmware modules as `roles/esxi`, but loops
  `vcenter_esxi_hosts` through the vCenter API (no per-host SSH).
- Distributed networking is `vcenter_config.vds` / `.portgroups`; standard
  vSwitch is separate (`vcenter_manage_host_vss`).

## Out of scope

- svc-account password rotation after first create.
- Non-Cloudflare ACME DNS-01 (this optional package is Cloudflare-only).
- Bare-metal ESXi install.
- Live mutation of an existing LDAP identity source without
  `vcenter_ldap_force_recreate` (default is add-if-missing only).
- Identity Provider Federation / OIDC (SSO external IdP) — only LDAP/OpenLDAP
  / AD-over-LDAP via `sso-config`.

## Expected result

- Topology tags: VDS and dvPortGroups match `vcenter_config`.
- Host settings tags (when gated on): NTP/DNS/SSH converge on listed hosts.
- Certs (when gated on): VCSA presents a renewed Machine SSL certificate
  (REST replace; optional ACME issue).

## Export (adopt an existing vCenter)

Read-only **full** capture via vCenter API (no ESXi SSH). Writes **three
importable dictionaries** (one file + optional split files for blast radius):

| Dictionary | Import target | Apply |
|---|---|---|
| `vcenter_config` | `group_vars` | `--tags topology` (incl. `vds_hosts`) |
| `vcenter_esxi_hosts` | `group_vars` | host day-2 gates / product loops |
| `esxi_host_configs` | `host_vars/<host>.yml` as `esxi_*` | `roles/esxi --tags networking,ntp,dns,ssh` |

```bash
ansible-playbook -i inventories/infra playbooks/10_vm_vcenter.yml --tags export
# → vcenter.config.snapshot.yml   (one file: all three dicts)
# Optional split (blast-radius files): -e vcenter_export_split=true
```

```bash
# Default: pretty block YAML (to_pretty_yaml, gap_depth=2)
ansible-playbook -i inventories/infra/hosts.yml playbooks/10_vm_vcenter.yml --tags export

# Compact one-item-per-line lists (FreeIPA-style yaml_flow)
ansible-playbook ... --tags export \
  -e vcenter_export_list_style=yaml_flow

# JSON list items (FreeIPA-style json)
ansible-playbook ... --tags export \
  -e vcenter_export_list_style=json

# Whole file as JSON (Artifactory-style)
ansible-playbook ... --tags export \
  -e vcenter_export_format=json
```

| Variable | Default | Purpose |
|---|---|---|
| `vcenter_export_output` | `{{ playbook_dir }}/../vcenter.config.snapshot.yml` | Destination path |
| `vcenter_export_format` | `yaml` | `yaml` \| `json` |
| `vcenter_export_list_style` | `block` | `block` \| `json` \| `yaml_flow` (yaml only) |
| `vcenter_export_pretty_gap_depth` | `2` | Blank-line gaps for `block` style |
| `vcenter_export_omit_empty` | `true` | Drop null/`''`/`[]`/`{}` keys |
| `vcenter_export_include_stock` | `false` | Also capture built-in portgroups |

Then copy `vcenter_config:` into `inventories/<env>/group_vars/vcenter.yml` and
reapply with `--tags topology`. `vds_hosts` is not exported — fill uplink
mapping by hand after the snapshot.

## Tag safety

- Do not put a role-level `tags: [...]` on the role invocation — it cascades
  onto `never`-gated export/reconcile.
- `export` / `reconcile` require explicit `--tags` and extra boolean gates.
