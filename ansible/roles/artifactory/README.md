# artifactory

Portable, API-driven configuration of a **JFrog Artifactory Enterprise** instance.
One role, one schema, two directions:

- **`backup`** — read the entire live configuration over the REST API and write it
  to a single YAML/JSON file.
- **`apply`** — reconcile that same schema into a fresh/empty instance (provision
  from scratch) or an existing one (brownfield add/update/remove).

The backup output **is** valid apply input — no translation. All you need is the
instance URL and an admin API token.

> Why not just import the config JSON/XML? Because copying
> `artifactory.config.latest.xml` + `artifactory.repository.config.latest.json` only
> restores repos + system settings — never users, groups, permissions, projects,
> Xray, or SSO (those live in the database, not the files). This role rebuilds the
> **whole** estate via API, and removes/changes things surgically.

Validated live against Artifactory **7.156.2** / Xray **3.147.2** (Enterprise+),
ansible-lint clean at the `production` profile. See [`docs/api-reference.md`](docs/api-reference.md)
for the full, live-tested API surface.

## Requirements

- Ansible 2.15+ (runs from the controller; the role talks HTTP, no host access needed).
- An **admin access token** (preferred) or admin user/password. A platform-admin
  token is needed for the Access-managed sections (projects, environments, LDAP,
  Vault); an Artifactory-only admin token still does repos/groups/users/permissions/
  SSO/Xray and simply skips what it can't reach.

## Quick start

```bash
export ARTIFACTORY_TOKEN='<admin token>'          # role reads this env var

# 1. Back up everything to a file
ansible-playbook playbooks/artifactory.yml \
  -e artifactory_url=https://acme.jfrog.io \
  -e artifactory_mode=backup \
  -e artifactory_export_file=./acme-state.yml      # or .json with -e artifactory_export_format=json

# 2. Rebuild an empty instance from that file
ansible-playbook playbooks/artifactory.yml \
  -e artifactory_url=https://newbox.example.com \
  -e artifactory_mode=apply \
  -e artifactory_import_file=./acme-state.yml

# 3. Provision a designed estate (no prior backup needed)
ansible-playbook playbooks/artifactory.yml \
  -e artifactory_url=https://newbox.example.com \
  -e artifactory_mode=apply \
  -e @roles/artifactory/examples/multitenant.yml
```

## What it manages

| Section | Endpoint(s) | CRUD | Round-trips |
|---|---|---|---|
| Repositories (local/remote/virtual/federated) | `/api/repositories` (+ bulk `…/configurations`) | C R U D | ✅ |
| Groups | `/api/security/groups` | C R U D | ✅ |
| Users | `/api/security/users` | C R U D | ✅ (minus password) |
| Permission targets (v2) | `/api/v2/security/permissions` | C R U D | ✅ |
| Projects + roles + members + repo attach | `/access/api/v1/projects` | C R U D | ✅ |
| Lifecycle environments (stages) | `/access/api/v1/environments` | C R D | ✅ |
| HashiCorp Vault connections | `/access/api/v1/vault/configs` | C R U D | ✅ (minus secrets) |
| LDAP | `/access/api/v1/ldap/settings` | C R U D | ✅ |
| SSO — SAML / OAuth / Crowd | `/api/saml/config`, `/api/oauth`, `/api/crowd` | R U | ✅ (minus secrets) |
| Xray policies / watches / ignore rules / indexing | `/xray/api/v2/*`, `/xray/api/v1/*` | C R U D | ✅ |
| Replications | `/api/replications` | C R U D | ✅ (minus creds) |
| Webhooks | `/event/api/v1/subscriptions` | C R U D | ✅ |
| Access tokens (mint only) | `/access/api/v1/tokens` | C | ❌ secret |
| Global config descriptor (proxies/backups/mail/layouts/property-sets) | `/api/system/configuration` | R U | self-hosted only |

**Cannot round-trip** (secrets/computed, by design): user passwords, access-token
secrets, SSO/Vault/replication credentials, repo `revision`. On apply, missing user
passwords are generated and written to `artifactory_generated_users_file` (mode 0600).

## Modes, state, and surgical changes

- **Greenfield**: `mode: apply` against an empty box with a full state file → builds
  everything in dependency order (repos → security → projects → integrations → xray).
- **Brownfield add/update**: `mode: apply` with only the objects you want — each
  section is a no-op when its list is empty. Existing objects are updated when
  `artifactory_reconcile_existing: true` (default), or left alone when `false`.
- **Surgical remove**: add `state: absent` to any object → it's DELETEd. Run with a
  one-object file to remove exactly one repo/project/group:
  ```bash
  ansible-playbook playbooks/artifactory.yml -e artifactory_url=… -e artifactory_mode=apply \
    -e '{"artifactory_local_repositories":[{"key":"old-repo","state":"absent"}]}'
  ```
- **Prune (full reconcile)**: `-e artifactory_prune=true` deletes server objects of a
  managed type that aren't in your desired list. **Dangerous** — off by default,
  honours `--check` (dry-run reports what it would delete), never touches protected
  built-ins (`admin`, `anonymous`, `readers`, `DEV`/`PROD`, `default` project — see
  `vars/main.yml`), and is limited to `artifactory_prune_sections`.

Deletes run in reverse dependency order (virtual repos before their members; Xray
watches before the policies they reference).

## Scoping a run

```bash
# Only repositories and projects
-e artifactory_manage_groups=false -e artifactory_manage_users=false …
# …or use tags
--tags repositories,projects
```
Per-section switches: `artifactory_manage_{system_config,repositories,groups,users,
permissions,projects,environments,vault,ldap,sso,xray,replications,webhooks,tokens}`.
Tags mirror the section names (plus `backup` / `apply` / `security`).

## SaaS vs self-hosted

JFrog **SaaS** ("Artifactory Online") blocks the global config descriptor
(`/api/system/configuration`) and mail/reverse-proxy config — JFrog manages those.
Everything else (repos, security, projects, SSO via dedicated endpoints, Xray, Vault)
works on both. The role tolerates the SaaS 400/403s and simply omits those sections.
On **self-hosted** Enterprise (typical work dev box) the descriptor works and is
captured/applied.

## JSON output

Set `artifactory_export_format: json` to write the state as JSON instead of YAML. It
re-imports the same way (`artifactory_import_file: state.json`). Real estates produce
large files (thousands of lines) — that's expected; it's the full config.

## Security

- Credentials come from env (`ARTIFACTORY_TOKEN` / `ARTIFACTORY_USER` /
  `ARTIFACTORY_PASSWORD`) or `--extra-vars`; nothing is committed.
- Tasks handling secrets use `no_log: true`.
- Generated user passwords and minted tokens are written to 0600 sidecar files for you
  to move into a secret manager, never echoed.

## Layout

```
roles/artifactory/
├── defaults/main.yml          # the full schema + behavioural defaults
├── vars/main.yml              # API endpoint constants (live-validated)
├── meta/{main,argument_specs}.yml
├── tasks/                     # main → preflight → backup | apply → per-section files
├── examples/multitenant.yml   # project-per-tenant reference design
└── docs/api-reference.md      # full API surface, statuses, shapes
```

## Tested

Live round-trip on the Enterprise+ trial: `backup` (full capture), `apply` greenfield
(repos local/remote/virtual, group, permission, project + member, Xray policy + watch
— all verified created), and `apply` with `state: absent` (all verified deleted, correct
ordering). No test artifacts left behind.
