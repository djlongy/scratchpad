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
| LDAP | `/access/api/v1/ldap/settings` | C R U D | ✅ (minus bind password) |
| SSO — SAML / OAuth / Crowd | `/api/saml/config`, `/api/oauth`, `/api/crowd` | R U | ✅ (minus secrets) |
| Xray policies / watches / ignore rules / indexing | `/xray/api/v2/*`, `/xray/api/v1/*` | C R U D | ✅ |
| Replications | `/api/replications` | C R U D | ✅ (minus creds) |
| Webhooks | `/event/api/v1/subscriptions` | C R U D | ✅ |
| Access tokens (mint only) | `/access/api/v1/tokens` | C | ❌ secret |
| Global config descriptor (proxies/backups/mail/layouts/property-sets) | `/api/system/configuration` | R U | self-hosted only |

**Cannot round-trip** (secrets/computed, by design): user passwords, access-token
secrets, SSO/Vault/replication credentials, LDAP bind passwords, repo `revision`.
On apply, missing user passwords are generated and written to
`artifactory_generated_users_file` (mode 0600).

### LDAP bind passwords

The Access API returns `search.manager_password` **masked** (asterisks), rejects
the masked value if you send it back (400), and **wipes** the stored password if
the field is omitted on update. So the backup file keeps the asterisk
placeholder, and on apply you supply the real secret out-of-band, ideally
straight from Vault, so the export never needs hand-editing. Two ways:

**Single LDAP setting (the common case)** — one scalar, no dict, no keys:

```yaml
artifactory_ldap_manager_password: "{{ enc_svc_bind_artifactory_password }}"
```

**Multiple LDAP settings** — key each password to its setting (the map entry
overrides the scalar for that setting):

```yaml
artifactory_ldap_manager_passwords:
  corp-ldap: "{{ lookup('community.hashi_vault.hashi_vault', 'secret=…:corp') }}"
  partner-ldap: "{{ lookup('community.hashi_vault.hashi_vault', 'secret=…:partner') }}"
```

Resolution per setting: `artifactory_ldap_manager_passwords[<key>]` first, else
the scalar `artifactory_ldap_manager_password`. Any setting with a `manager_dn`
but no usable password from either source (only the masked/absent value in the
body) is **skipped with a warning** — the role never pushes asterisks and never
wipes a stored bind password.

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

## GitOps workflow — As-Built vs Desired

The intended operating loop for using this role as IaC with audit + approval:

1. **Pull (As-Built)** — a scheduled/on-demand `mode: backup` writes the full
   live config to an export ("as-built"). Commit it to an `as-built/` area of
   the repo (or attach it as a CI artifact). Exports are deterministic — keys
   sorted, stable layout — so two exports diff cleanly.
2. **Compare & cherry-pick** — diff the as-built export against the desired
   state in `group_vars`/state files (`git diff` / `diff -u`; the export's
   blank-line-per-object layout keeps the diff readable). Whatever drift you
   want to KEEP, copy into the desired state in a merge request; whatever you
   don't, leave out.
3. **Approve** — the MR review is the approval gate; history is the audit log.
4. **Push (Apply)** — on merge, CI runs `mode: apply` with the desired state.
   With `artifactory_prune: true` the instance is fully reconciled (server
   objects not in the desired state are deleted — protected built-ins
   excepted); without it, apply is additive/corrective only.

Secrets never live in either file: bind passwords come from Vault via
`artifactory_ldap_manager_passwords`, user passwords are generated or supplied
at apply time, token secrets are minted and exported to 0600 sidecars.

`_meta` in the export records provenance (source URL, version) plus
`trash_can`: repo-named folders whose deleted content still sits in the trash
can (`auto-trashcan`). A name appearing there explains "the UI shows it under
Trash" — its repo CONFIG may already be gone, so it won't be in the repo
lists; only its content lingers until the retention period expires. There is
no public REST API for the repository-trash view itself (the UI uses internal
`/ui/api` endpoints that reject access tokens).

### System config descriptor (self-hosted only)

When the global config descriptor XML is captured, it is externalized to
sidecars next to the export instead of bloating the state file
(`artifactory_export_system_config_files: true`):

| File | Purpose |
|---|---|
| `<export>.system-config.xml` | raw descriptor — authoritative, used by the opt-in DR re-apply path (`artifactory_apply_system_config_xml`) |
| `<export>.system-config.parsed.yml` | XML→YAML **reference** for humans drafting `artifactory_system_config_yaml` IaC blocks (needs `xmltodict` on the controller; skipped with a note if absent) |

The state file carries `artifactory_system_config_xml_file` pointing at the
XML sidecar. The parsed YAML is a reference only — xmltodict conventions
(`'@'`-prefixed attributes, strings everywhere) mean it is NOT directly valid
`artifactory_system_config_yaml` input; cherry-pick and clean the blocks you
want to manage.

## Trimming empty values

By default (`artifactory_export_omit_empty: true`) the export omits keys whose
value is "empty" — `null`, `''`, `[]`, or `{}` — so the As-Built vars stay
small and free of noise like `description: ''` or `propertySets: []`. This is
done by the bundled **`drop_empty`** filter (`filter_plugins/data_shaping.py`),
a recursive prune that runs before serialization, so even deeply-nested empties
go. Falsy-but-real settings (`false`, `0`) are always kept — they are
meaningful, not absence. Because absent == empty == the API default on apply,
trimming is lossless for round-trip. Set `artifactory_export_omit_empty: false`
for a fully faithful dump that records every field. `drop_empty` is a pure
data transform (format-agnostic — it serves the JSON export path too); it is
kept in its own file so `to_pretty_yaml` stays purely about formatting.

## Export formatting

YAML exports are written by the role's bundled `to_pretty_yaml` filter
(`filter_plugins/yaml_pretty.py`): list items are indented two spaces under
their parent key, and blank lines separate sibling nodes down to a chosen
depth, so each logical block is visually distinct in a multi-thousand-line
file. `tasks/backup.yml` calls it with fixed literals
(`gap_depth=2, gap_blocks_only=true`); edit those to retune. Parameters
(filter defaults shown): `indent=2`, `width=200`, `sort_keys=false`, `gap_depth=1`,
`gap_blocks_only=true`. `sort_keys=false` means keys keep the order the API
returned them in (omit it to preserve order; pass `sort_keys=true` only if you
want alphabetical) — preserving order makes merge-request diffs cleaner.

| `gap_depth` | Blank lines between |
|---|---|
| `0` | nothing (plain `to_nice_yaml` layout, just indented lists) |
| `1` | root keys only — the default |
| `2` | …plus each root key's children (2nd-level keys / top-level list entries) |
| `3`+ | …one level deeper per increment |

`gap_blocks_only` (default `true`) refines the above: a blank line goes
between two siblings only when at least one is a multi-line block, so adjacent
single-line siblings (scalar-only config maps, single-line list items like
`environments`) stay packed instead of being shredded by gaps. Set `false` to
gap every sibling.

Gaps are produced by dumping each sibling subtree separately (never by
regexing emitted lines), so multi-line string values containing `key:`-shaped
text are immune. Blank lines are insignificant in YAML; the file re-imports
identically. (PyYAML can't indent block sequences via `to_nice_yaml`
arguments — it requires the filter's `Dumper.increase_indent` override, which
is why this lives in a plugin.)

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
