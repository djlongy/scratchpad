# OS-level SBOM pipeline (Syft → SBOM consumer)

## Goal

Inventory every managed Linux host's **OS packages** as a CycloneDX SBOM and
upload them to an SBOM consumer (for example Dependency-Track) so operators can
open a tenant/env portfolio and see CVEs per host — not only container image
SBOMs from CI.

## Shape

```
CI schedule / operator workstation
  └─ ansible-playbook -i inventories/<env>/hosts.yml playbooks/ops_os_sbom.yml
       └─ role os_sbom (per Linux host, serial 20%)
            ├─ install pinned generator from package-repo policy
            ├─ syft scan /  (OS package catalogers only)
            └─ PUT /api/v1/bom  (parent + tags + isLatestProjectVersion)
```

## Multi-tenant × multi-env project model

Identity from inventory (`tenancy`, `env`, hostname). Empty path segments drop.

| Layer | Default name | Version |
|---|---|---|
| Root (optional) | `os-hosts/<tenancy>` | `portfolio` |
| Parent | `os-hosts/<tenancy>/<env>` | `portfolio` |
| Child | `os/<tenancy>/<env>/<hostname>` | OS release (default) |

Examples:

| tenancy | env | host | Project |
|---|---|---|---|
| `acme` | `mgt` | `vault-01` | `os/acme/mgt/vault-01` under `os-hosts/acme/mgt` |
| `globex` | `prod` | `app-01` | `os/globex/prod/app-01` under `os-hosts/globex/prod` |

Same env label under two tenants never collides. Override `*_name_parts` or full
names in inventory for a different org scheme; set `os_sbom_root_enabled: false`
for a flat env-only tree.

## Product-agnostic split

| Plane | Holds |
|---|---|
| **Role** (`roles/os_sbom`) | Mechanism: install binary, catalog packages, POST CycloneDX |
| **Inventory** | Policy: package-repo URL, upload URL, path parts, identity |
| **Env / CI** | Secrets only: `REPO_TOKEN`, `OS_SBOM_UPLOAD_API_KEY` |

See `inventories/example/group_vars/all/os_sbom.yml.example`.

## Why Ansible

- Hosts often sit behind jump hosts; fleet SSH is already inventory-driven.
- Identity (`tenancy`, `env`) already lives in group_vars.
- Privilege for package DBs is per-task become inside the role (no play-level become).

Container/image SBOMs remain a separate CI path (image build templates). This
pipeline is **host OS packages only**.

## Operator run

```bash
export REPO_USER=…
export REPO_TOKEN=…
export OS_SBOM_UPLOAD_API_KEY=…

cd ansible
ansible-playbook -i inventories/example/hosts.yml playbooks/ops_os_sbom.yml
# limit / tags as needed: --limit bastion --tags scan
```

## Files

- `roles/os_sbom/`
- `playbooks/ops_os_sbom.yml`
- `inventories/example/group_vars/all/os_sbom.yml.example`
- `tests/unit/roles/test_os_sbom_project_model.py`

## Out of scope

- Offline re-scan with a separate CVE tool (the consumer correlates against NVD/OSV).
- Windows / network-appliance SBOMs.
- Deploying the SBOM consumer itself (see `roles/dependency_track` if present).
