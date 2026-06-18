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

`apply` is the default mode, so only `backup`/`compare` are passed explicitly.

```bash
export ARTIFACTORY_TOKEN='<admin token>'          # role reads this env var

# 1. Back up everything for an environment
#    (writes roles/artifactory/files/state/prod/artifactory.yml)
ansible-playbook playbooks/artifactory.yml \
  -e artifactory_url=https://acme.jfrog.io \
  -e artifactory_mode=backup \
  -e artifactory_env=prod                          # or .json with -e artifactory_export_format=json

# 2. Rebuild an empty instance from that environment's saved state (apply = default)
ansible-playbook playbooks/artifactory.yml \
  -e artifactory_url=https://newbox.example.com \
  -e artifactory_env=prod

# 3. Provision a designed estate (no prior backup needed)
ansible-playbook playbooks/artifactory.yml \
  -e artifactory_url=https://newbox.example.com \
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
  ansible-playbook playbooks/artifactory.yml -e artifactory_url=… \
    -e '{"artifactory_local_repositories":[{"key":"old-repo","state":"absent"}]}'
  ```
- **Prune (full reconcile)**: `-e artifactory_prune=true` deletes server objects of a
  managed type that aren't in your desired list. **Dangerous** — off by default,
  honours `--check` (dry-run reports what it would delete), never touches protected
  built-ins (`admin`, `anonymous`, `readers`, `DEV`/`PROD`, `default` project — see
  `vars/main.yml`), and is limited to `artifactory_prune_sections`.

Deletes run in reverse dependency order (virtual repos before their members; Xray
watches before the policies they reference).

## Environments & promotion

Captured state is **per environment**. Set `artifactory_env` (in each env's
`group_vars`); the role resolves one file per env — no separate input/output var,
the **mode** decides direction (`backup` writes it, `apply`/`compare` read it):

```
roles/artifactory/files/state/<env>/artifactory.yml      # the state file
                                    artifactory.system-config.xml / .parsed.yml
                                    artifactory.drift.yml        # mode: compare
```

The resolved path is `artifactory_state_file` (defaults to
`{{ artifactory_state_dir }}/{{ artifactory_env }}/artifactory.<ext>`); override it
only for a one-off non-standard location, and use an **absolute** path if you do.
Each backup stamps `_meta.environment`, so the role knows where a capture came from.

**Apply is additive by default** (`artifactory_prune: false`): create-or-update only,
never deleting objects absent from the state file — so promoting one env's state onto
another can only add/update, never silently drop config. Applying a *different*
environment's state onto a **protected** env (`artifactory_protected_envs`, default
`[prod]`), or pruning a protected env, **fails unless `artifactory_confirm_promote: true`**
— promotion into prod is always a conscious act; same-env apply and prod→dev are free.

Promotion uses `artifactory_promote_from` — the SOURCE env to apply onto the
target `artifactory_env`. The source is resolved against the absolute state dir,
so it's never a fragile relative path.

```bash
# 1. Capture prod  ->  files/state/prod/artifactory.yml
ansible-playbook playbooks/artifactory.yml -e artifactory_url=$PROD \
  -e artifactory_mode=backup -e artifactory_env=prod

# 2. Clone prod -> dev  (apply prod's capture onto dev; dev not protected = no prompt)
ansible-playbook playbooks/artifactory.yml -e artifactory_url=$DEV \
  -e artifactory_env=dev -e artifactory_promote_from=prod

#    …surgical changes in dev, then capture dev -> files/state/dev/artifactory.yml…

# 3. Promote dev -> prod  (GUARD: dev state onto protected prod requires confirm)
ansible-playbook playbooks/artifactory.yml -e artifactory_url=$PROD \
  -e artifactory_env=prod -e artifactory_promote_from=dev \
  -e artifactory_confirm_promote=true
```

To remove objects added to prod out-of-band: `mode: compare` shows drift vs the
committed baseline, then a deliberate `artifactory_prune=true` (plus
`artifactory_confirm_promote=true` for prod) prunes the unmanaged ones.

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
   live config to `files/state/<env>/artifactory.yml`. In a **private** repo,
   commit it for a diffable history; **this public repo gitignores captures**
   (they carry real hostnames, LDAP DNs and emails) — point
   `artifactory_state_dir` at a private location for committed history. Exports
   are deterministic — keys sorted, stable layout — so two diff cleanly.
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

`_meta` in the export records provenance (environment, source URL, Artifactory
version, capture timestamp) plus
`trash_can`: repo-named folders whose deleted content still sits in the trash
can (`auto-trashcan`). A name appearing there explains "the UI shows it under
Trash" — its repo CONFIG may already be gone, so it won't be in the repo
lists; only its content lingers until the retention period expires. There is
no public REST API for the repository-trash view itself (the UI uses internal
`/ui/api` endpoints that reject access tokens).

### System config descriptor — resilient, cross-version restore (self-hosted only)

The global config descriptor only **reads** as XML, and the raw-XML
full-replace POST is a dead end across instances/versions: it's bound to an xsd
schema version (`xmlns=…/xsd/X.Y.Z`, tied to the Artifactory release) **and**
carries passwords encrypted under the *source* instance's master key — so
promoting prod→dev, or restoring across an up/downgrade, fails (a schema 400 or
a master-key 500). The supported, version-tolerant path is the **YAML PATCH**
(`application/yaml`): stable keys, additive, validated per-key.

So when the descriptor is captured (`artifactory_export_system_config_files:
true`) the role externalizes two sidecars next to the export:

| File | Purpose |
|---|---|
| `<export>.system-config.xml` | raw descriptor — full fidelity; the opt-in same-version DR re-apply path (`artifactory_apply_system_config_xml`) |
| `<export>.system-config.apply.yml` | **PATCH-ready** YAML config (the `descriptor_to_config` transform: flattened to the PATCH schema, list sections as keyed maps, native types) with master-key-encrypted secrets and role-managed blocks (repos/replications/keyPairs) **stripped**. This is the resilient restore input. |

The state file points at both **by filename only** (not absolute paths), so the
export is portable across checkouts; on apply the role resolves them next to the
state file.

**On apply** (default, no extra flags): if you didn't hand-author
`artifactory_system_config_yaml`, the role **auto-loads** the `.apply.yml`
sidecar — no copy-paste into `group_vars` — and PATCHes it **block by block,
fail-soft**. A block this Artifactory version doesn't accept is reported and
skipped (set `artifactory_fail_fast: true` to make that fatal) — which is
exactly what lets you restore across versions and see precisely what didn't map.

**Secrets** the capture stripped (mail/proxy passwords, ssl keys) can't port —
re-supply only the ones you need via `artifactory_system_config_secrets` (deep-
merged before PATCH, ideally from Vault), same idea as the LDAP bind passwords.

**Raw XML** (`artifactory_apply_system_config_xml: true`) stays available for
same-version DR but now refuses up front if the captured descriptor's schema
version ≠ the target's — so it can't silently 500.

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

Set `artifactory_export_format: json` to write the state as JSON instead of YAML (the
resolved state file then ends in `.json`). It re-imports the same way — apply reads the
same per-environment file. Real estates produce large files (thousands of lines) —
that's expected; it's the full config.

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
