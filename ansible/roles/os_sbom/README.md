# os_sbom

## TL;DR

Catalogs the OS packages of every Linux host into a CycloneDX SBOM with Syft and
uploads it to an SBOM consumer under a multi-tenant × multi-environment project
tree. Role = mechanism; inventory = policy (repo, endpoints, identity).

    ansible-playbook -i inventories/<env>/hosts.yml playbooks/ops_os_sbom.yml

## Requirements

None beyond `ansible.builtin`.

## Key variables

Full list: `defaults/main.yml`. Contract: `meta/argument_specs.yml`.

**Required** = value must be correct for a successful run (defaults often exist).
**Optional** = safe to leave default / empty.
**When X** = required only if that feature is on.

| Req | Variable | Default | Purpose |
|---|---|---|---|
| **Required** | `os_sbom_env` | `{{ env }}` | Environment token in the project path |
| **Required** | `os_sbom_syft_download_url` | `""` | Generator package URL; empty → composed from the repo policy below |
| **Required** | `os_sbom_upload_url` | `$OS_SBOM_UPLOAD_URL` | SBOM consumer API base URL |
| **Required** | `os_sbom_upload_api_key` | `$OS_SBOM_UPLOAD_API_KEY` | SBOM consumer API key |
| When composed URL | `os_sbom_repo_base_url`, `os_sbom_repo`, `os_sbom_syft_repo_path` | `""` | Compose the download URL as `base/repo/path` |
| When auth ≠ `none` | `os_sbom_repo_password` | `$REPO_TOKEN` | Repo token or password |
| Optional | `os_sbom_repo_auth` | `basic` | `basic` \| `bearer` \| `none` (anonymous repo) |
| Optional | `os_sbom_tenancy` | `{{ tenancy }}` | Tenant token; the segment is dropped when empty |
| Optional | `os_sbom_syft_version` | `1.20.0` | Pinned generator release |
| Optional | `os_sbom_syft_package_format` | `tar.gz` | `tar.gz` \| `zip` \| `rpm` |
| Optional | `os_sbom_syft_rpm_disable_gpg_check` | `false` | RPM only: skip package signature verification |
| Optional | `os_sbom_version_mode` | `os_release` | Project version from `os_release` \| `date` \| `fixed` |
| Optional | `os_sbom_parent_enabled` / `os_sbom_root_enabled` | `true` | Portfolio layers above the host project |
| Optional | `os_sbom_keep_remote` | `false` | Keep the SBOM file on the target after upload |

## Minimum configuration

    # group_vars/all.yml
    ---
    # Required identity (tenancy is optional; its path segment is dropped when unset)
    env: prod

    # Required: where the generator package is fetched from
    os_sbom_repo_base_url: https://packages.example.internal/repo
    os_sbom_repo: generic-tools
    os_sbom_syft_repo_path: >-
      anchore/syft/releases/download/v{{ os_sbom_syft_version }}/syft_{{
      os_sbom_syft_version }}_linux_{{ os_sbom_syft_arch }}.tar.gz

    # Required: where the SBOM is uploaded
    os_sbom_upload_url: https://sbom.example.internal

    # Credentials come from the environment — never commit values
    os_sbom_repo_password: "{{ lookup('env', 'REPO_TOKEN') }}"
    os_sbom_upload_api_key: "{{ lookup('env', 'OS_SBOM_UPLOAD_API_KEY') }}"

Set `os_sbom_repo_auth: none` instead of `os_sbom_repo_password` when the package
repo serves anonymous downloads.

## Usage

    - name: Generate and upload OS SBOMs
      hosts: linux
      roles:
        - role: os_sbom
          tags: [os_sbom]

Run:

    export REPO_TOKEN=…
    export OS_SBOM_UPLOAD_API_KEY=…
    ansible-playbook -i inventories/<env>/hosts.yml playbooks/ops_os_sbom.yml --tags os_sbom

## Behaviour

Names are built from identity tokens, joined with `/`, and empty segments are
dropped. A non-empty `os_sbom_project_name` / `os_sbom_parent_name` /
`os_sbom_root_name` overrides the corresponding `*_name_parts` list.

| Layer | Parts | Version |
|---|---|---|
| Root (optional) | `os-hosts` / `<tenancy>` | `portfolio` |
| Parent | `os-hosts` / `<tenancy>` / `<env>` | `portfolio` |
| Project | `os` / `<tenancy>` / `<env>` / `<host>` | OS release (default) |

| tenancy | env | host | Project |
|---|---|---|---|
| `acme` | `prod` | `app-01` | `os/acme/prod/app-01` |
| `globex` | `dev` | `api-01` | `os/globex/dev/api-01` |
| _(empty)_ | `staging` | `bastion-01` | `os/staging/bastion-01` |

- Host project tags are `os-host`, `tenancy:<tenancy>`, `env:<env>`, `<distro>`;
  empty and bare `key:` tags are dropped.
- The root and parent portfolios are created at the consumer before the BOM is
  submitted; the host project is created by BOM auto-create.
- The generator binary is installed only when missing or version-mismatched.
- The SBOM file is deleted from the target after a successful upload unless
  `os_sbom_keep_remote` is true.
- Non-Linux hosts and members of `os_sbom_skip_os_families` are skipped, so the
  role is safe to apply to a mixed inventory.

## Out of scope

- Does not scan application dependencies — OS package databases only.
- Does not create the consumer's teams, permissions, or API keys.
- Does not publish or mirror the generator package to the repo it downloads from.

## Tag safety

`--tags upload` submits whatever SBOM already exists on the target; run it alone
only after a prior `scan`.
