# artifactory

## TL;DR

Portable, API-driven configuration of a JFrog Artifactory Enterprise instance. Desired
state lives in **group_vars** (one inventory per environment); three modes: `apply`
(reconcile group_vars into the instance — the default), `backup` (capture live state to
reference files, never applied), `compare` (diff live vs desired).

```bash
ansible-playbook playbooks/artifactory.yml -i inventories/<env> -e artifactory_url=https://artifactory.example.com
```

## Requirements

Install collections before running (repo `requirements.yml`, or ad-hoc):

    ansible-galaxy collection install -r requirements.yml

| Collection | When | Used for |
|---|---|---|
| `community.hashi_vault` | When bootstrap stores the minted token (`artifactory_bootstrap_vault_store`) | Read/write the durable admin token in HashiCorp Vault |
| `ansible.utils` | When `artifactory_mode: backup` | XML→YAML system-config descriptor transform (`from_xml` filter) |

## Key variables

Full list: `defaults/main.yml`. Contract: `meta/argument_specs.yml`.

**Required** = value must be correct for a successful run (defaults often work).
**Optional** = safe to leave default / empty; phase stays off or uses built-ins.
**When X** = required only if that feature is on.

| Req | Variable | Default | Purpose |
|---|---|---|---|
| **Required** | `artifactory_url` | `""` | Base platform URL, no trailing slash |
| Optional | `artifactory_mode` | `apply` | `apply` \| `backup` \| `compare` |
| Optional | `artifactory_access_token` | env `ARTIFACTORY_TOKEN` | Admin bearer token (preferred over basic auth) |
| Optional | `artifactory_admin_username` / `_password` | `admin` / `""` | Basic-auth fallback (env `ARTIFACTORY_USER`/`_PASSWORD`) |
| Optional | `artifactory_env` | `default` | Labels the per-environment reference-capture folder |
| Optional | `artifactory_reconcile_existing` | `true` | Update existing objects on apply (vs create-if-absent) |
| Optional | `artifactory_prune` | `false` | Full reconcile — deletes objects absent from desired state |
| When pruning a protected env | `artifactory_confirm_prune` | `false` | Must be `true` to prune an env in `artifactory_protected_envs` (default `[prod]`) |
| Optional | `artifactory_bootstrap_enabled` | `true` | Self-mint an admin token when none is supplied (fresh instance) |
| When bootstrapping a protected env | `artifactory_confirm_bootstrap` | `false` | Must be `true` before bootstrap restarts Access on a protected env |
| Optional | `artifactory_manage_<section>` | `true` (most) | Per-section switch: repositories/groups/users/permissions/projects/vault/ldap/sso/xray/… |
| Optional | `artifactory_local_repositories` / `_remote_` / `_virtual_` / `_federated_` | `[]` | Desired-state repo definitions (full `/api/repositories` shape) |
| Optional | `artifactory_groups` / `_users` / `_permissions` / `_projects` | `[]` | Desired-state security + project objects |

## Usage

```yaml
- hosts: localhost
  gather_facts: false
  roles:
    - role: artifactory
      vars:
        artifactory_url: "https://artifactory.example.com"
        artifactory_mode: apply
```

Run it:

```bash
# Apply desired state from an env's group_vars (apply is the default mode)
ansible-playbook playbooks/artifactory.yml -i inventories/<env>

# Capture live config to REFERENCE files (never applied)
ansible-playbook playbooks/artifactory.yml -i inventories/<env> -e artifactory_mode=backup

# Drift: live state vs group_vars
ansible-playbook playbooks/artifactory.yml -i inventories/<env> -e artifactory_mode=compare

# Scope a run to specific sections
ansible-playbook playbooks/artifactory.yml -i inventories/<env> --tags repositories,projects
ansible-playbook playbooks/artifactory.yml -i inventories/<env> -e artifactory_manage_groups=false -e artifactory_manage_users=false
```

## Preconditions

- Target instance already reachable at `artifactory_url` — the role configures it, it
  does not stand one up.
- Zero-touch admin-token bootstrap needs a self-hosted instance (not SaaS, not a pure
  localhost target) running Access ≥ 7.38.4.

## Behaviour

- **Manages**: repositories (local/remote/virtual/federated), groups, users, permission
  targets, projects (+ roles/members/repo attach), lifecycle environments, HashiCorp
  Vault connections, LDAP, SSO (SAML/OAuth/Crowd), Xray (policies/watches/ignore
  rules/reports), replications, webhooks, access tokens (opt-in mint), and the global
  system config descriptor (self-hosted only — blocked on SaaS).
- **Cannot round-trip** (secrets/computed, by design): user passwords, access-token
  secrets, SSO/Vault/replication credentials, LDAP bind passwords, repo `revision`. On
  apply, missing user passwords are generated and written to
  `artifactory_generated_users_file` (mode 0600), never echoed. LDAP bind passwords must
  be supplied at apply time via `artifactory_ldap_manager_password` (single setting) or
  `artifactory_ldap_manager_passwords` (map keyed by LDAP setting) — the API returns
  them masked and wipes the stored value if omitted on update.
- **Zero-touch admin-token bootstrap**: when no `artifactory_access_token` is supplied
  and `artifactory_bootstrap_enabled` is true, the role self-provisions a durable admin
  token via JFrog's automatic-admin-token mechanism: it places a trigger file, restarts
  Access (`artifactory_bootstrap_method: docker` or `native`), reads the transient
  token, mints a durable one, and stores it (Vault + a 0600 sidecar). The next run
  supplies the token from Vault and skips bootstrap entirely. A **supplied** token that
  is rejected (401/403) hard-fails by default (fail-secure); set
  `artifactory_bootstrap_on_auth_failure: true` to instead discard and re-mint.
- **Prune** (`artifactory_prune: true`) is a full reconcile — it deletes server objects
  of a managed type absent from the desired list. Off by default, honours `--check`,
  never touches protected built-ins (`admin`, `anonymous`, `readers`, `DEV`/`PROD`,
  `default` project — see `vars/main.yml`), limited to `artifactory_prune_sections`.
- **Surgical remove**: add `state: absent` to any desired-state object to delete just
  that object, independent of `artifactory_prune`.
