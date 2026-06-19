# artifactory

Portable, API-driven configuration of a **JFrog Artifactory Enterprise** instance.
Desired state lives in **group_vars** (one inventory per environment); captures
are reference-only. Three modes:

- **`apply`** — reconcile the desired state from group_vars into the instance
  (provision from scratch, or brownfield add/update/remove). The default mode.
- **`backup`** — read the entire live configuration over the REST API and write
  it to per-env REFERENCE files (never applied).
- **`compare`** — diff the live state against the group_vars desired state.

A capture's section shapes match the role's group_vars exactly, so copying a
section from a reference file into group_vars is valid apply input with no
translation. All you need is the instance URL and an admin API token.

> Why not just import the config JSON/XML? Because copying
> `artifactory.config.latest.xml` + `artifactory.repository.config.latest.json` only
> restores repos + system settings — never users, groups, permissions, projects,
> Xray, or SSO (those live in the database, not the files). This role rebuilds the
> **whole** estate via API, and removes/changes things surgically.

Built for JFrog Artifactory Enterprise; ansible-lint clean at the `production`
profile. See [`docs/api-reference.md`](docs/api-reference.md) for the API surface.

## Requirements

- Ansible 2.15+ (runs from the controller; the role talks HTTP, no host access needed).
- An **admin access token** (preferred) or admin user/password. A platform-admin
  token is needed for the Access-managed sections (projects, environments, LDAP,
  Vault); an Artifactory-only admin token still does repos/groups/users/permissions/
  SSO/Xray and simply skips what it can't reach.

## Quick start

Desired state lives in **group_vars**, one inventory per env. `apply` is the
default mode, so only `backup`/`compare` are passed explicitly.

```bash
export ARTIFACTORY_TOKEN='<admin token>'          # role reads this env var

# 1. Capture an env's live config to REFERENCE files (NOT applied):
#    writes files/state/prod/artifactory.config.reference.yml (+ .system-config.reference.yml)
ansible-playbook playbooks/artifactory.yml -i inventories/prod -e artifactory_mode=backup

# 2. Apply the desired state from that env's group_vars (apply = default):
ansible-playbook playbooks/artifactory.yml -i inventories/prod

# 3. Drift: live state vs the env's group_vars
ansible-playbook playbooks/artifactory.yml -i inventories/prod -e artifactory_mode=compare
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

- **Greenfield**: `mode: apply` against an empty box with full group_vars → builds
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

## Environments — per-env group_vars

**Desired state is group_vars, one inventory per environment.** The role applies
ONLY group_vars; captures are reference-only and never replayed. `artifactory_env`
(set in each inventory's group_vars) just labels where a backup writes that env's
reference files. Layer it: shared config in `group_vars/all`, per-env differences
in `inventories/<env>/group_vars`. Same playbook, pick the env by inventory:

```bash
# Apply dev / test / prod — each picks up its own group_vars
ansible-playbook playbooks/artifactory.yml -i inventories/dev      # or test / prod

# Capture an env's live state to REFERENCE files (NOT applied):
ansible-playbook playbooks/artifactory.yml -i inventories/prod -e artifactory_mode=backup
#   writes files/state/prod/:
#     artifactory.config.reference.yml                 # bundled capture (repos / security / …)
#     artifactory.system-config.reference.yml   # system config, named-root
#   → copy the sections you want into group_vars to manage them

# Drift: live state vs your declared group_vars
ansible-playbook playbooks/artifactory.yml -i inventories/dev -e artifactory_mode=compare
```

**Promotion between envs** = curate each env's group_vars (copy what you want from
an env's reference into shared/per-env group_vars). There is no "apply one env's
capture onto another" — desired state is always the explicit group_vars.

**Apply is additive by default** (`artifactory_prune: false`): create-or-update
only, never deleting objects absent from your group_vars. Deletion is deliberate:
per-item `state: absent`, or opt-in `artifactory_prune` — which on a **protected**
env (`artifactory_protected_envs`, default `[prod]`) needs
`artifactory_confirm_prune: true`.

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

1. **Pull (reference)** — a scheduled/on-demand `mode: backup` writes the live
   config to `files/state/<env>/artifactory.config.reference.yml` (+ the system-config
   reference). These are REFERENCE ONLY — never applied. Captures carry real
   hostnames, LDAP DNs and emails, so they are gitignored by default; to keep a
   diffable committed history, point `artifactory_state_dir` at a location where
   committing the captured config is acceptable.
2. **Cherry-pick into group_vars** — diff the reference against your declared
   desired state in `group_vars` (`mode: compare`, or `git diff` / `diff -u`).
   Whatever drift you want to KEEP, copy from the reference into group_vars in a
   merge request; whatever you don't, leave out.
3. **Approve** — the MR review is the approval gate; history is the audit log.
4. **Push (Apply)** — on merge, CI runs `mode: apply` (group_vars = desired
   state). With `artifactory_prune: true` the instance is fully reconciled
   (objects not in group_vars are deleted — protected built-ins excepted);
   without it, apply is additive/corrective only.

Secrets never live in either file: bind passwords come from Vault via
`artifactory_ldap_manager_passwords`, user passwords are generated or supplied
at apply time, token secrets are minted and exported to 0600 sidecars.

`_meta` in the export records provenance (environment, source URL, Artifactory
version, capture timestamp) plus
`trash_can`: repo-named folders whose deleted content still sits in the trash
can (`auto-trashcan`). A name appearing there explains "the UI shows it under
Trash" — its repo CONFIG may already be gone, so it won't be in the repo
lists; only its content lingers until the retention period expires. There is
no documented REST API for the repository-trash view itself (the UI uses internal
`/ui/api` endpoints that reject access tokens).

### System config descriptor — resilient, cross-version restore (self-hosted only)

The global config descriptor only **reads** as XML, and the raw-XML
full-replace POST is a dead end across instances/versions: it's bound to an xsd
schema version (`xmlns=…/xsd/X.Y.Z`, tied to the Artifactory release) **and**
carries passwords encrypted under the *source* instance's master key — so
promoting prod→dev, or restoring across an up/downgrade, fails (a schema 400 or
a master-key 500). The supported, version-tolerant path is the **YAML PATCH**
(`application/yaml`): stable keys, additive, validated per-key.

#### How config-as-code works (PATCH, not POST) — and how to set ANY setting

Two operations get conflated; they are not the same thing:

| | A — POST the whole XML (fails cross-version) | B — PATCH a YAML fragment (always works) |
|---|---|---|
| Call | `POST …/configuration` (`application/xml`) | `PATCH …/configuration` (`application/yaml`) |
| Body | the WHOLE descriptor | only the keys you're changing |
| Semantics | full **replace** | **merge** |
| Schema | xsd-version-stamped | none — you send intent |
| Secrets | carries the source's encrypted secrets | carries none |

A single field (e.g. a docker reverse-proxy method) was always settable with one
B-style call — it was never "blocked". What failed was A. **What PATCH does, step
by step:**

1. Verb `PATCH` = *partial modify* (vs POST/PUT = replace the whole document) —
   this alone bounds the blast radius.
2. Body is a **fragment** — only the keys you're changing. Everything you don't
   mention is left exactly as-is; you carry none of the other settings and none
   of the encrypted secrets.
3. The running binary **merges** it into the live config, writing it into *its
   own* current internal schema. You never name a schema version; it owns that.
4. Deletes are explicit: a key set to `null` is removed; **absence = leave alone,
   `null` = delete**.

**Why A fails but B works** — three independent reasons: the verb (replace vs
merge), the payload (whole doc vs fragment), the format (xsd-stamped XML vs
schema-less YAML). A cross-version XML POST runs the target's schema converters
(mismatch → boot-loop) and tries to decrypt the source's secrets with the wrong
master key (→ 500); a YAML PATCH does neither.

Block-by-block is just this same PATCH repeated once per top-level section (one
for `backups`, one for `reverseProxies`, …) for **fault isolation**. Validated
behaviour: if your fragment holds a key the version doesn't accept, the API
rejects *that whole PATCH* and **names the offending key** — so the role drops
just that key and re-PATCHes; the rest of the block still applies on the retry.

**To manage a NEW setting you do NOT touch the role.** `artifactory_system_config_yaml`
(and `…_overrides`) is a free-form pass-through dict — the role PATCHes whatever
you give it, block by block. Just add the key:

```yaml
artifactory_system_config_yaml:
  someBlock:
    someKey: value        # nothing in the role predefines this — it's passed through
```

Two caveats: (1) it must be a real **writable** config-as-code key for your
version — discover the exact names by running `mode: backup` and reading the
`.system-config.reference.yml` file (the precise keys your instance accepts), or
the JFrog YAML-config docs / `artifactory.xsd`; an invalid or read-only key is
simply dropped + reported, never fatal. (2) This is the global **descriptor**
only — **repositories** (left the descriptor at 7.49) and **LDAP/SAML/OAuth/Crowd**
(left at 7.59, now in the Access service) are NOT set here; they have their own
sections/APIs.

**On apply**, the desired config is **group_vars only** —
`artifactory_system_config_yaml` deep-merged with
`artifactory_system_config_yaml_overrides` (per env) and
`artifactory_system_config_secrets`. It is applied by the
`artifactory_config_apply` module (`library/`), which is **version-adaptive**:
the YAML-PATCH schema is only the *writable* properties and that set **drifts
between Artifactory versions**. The module PATCHes each block and, on a 400
naming a key the target doesn't accept (`Key "<x>" is not part of the
configuration`), **drops that key and retries** — the server is the schema
oracle, so the maximal version-compatible subset applies on *any* version, up or
downgrade. Dropped keys are listed in `dropped`, any block it couldn't salvage in
`rejected` (set `artifactory_fail_fast: true` to make a rejection fatal) — so a
restore tells you precisely what didn't map.

A `mode: backup` writes `artifactory.system-config.reference.yml` — the same
`descriptor_to_config` transform (flattened to the PATCH schema, keyed maps,
native types, secrets + role-managed blocks stripped) under a named root, with a
"REFERENCE ONLY" header. It is **never auto-applied** — copy the dict (or the
blocks you want) into group_vars to manage it.

**Secrets** the capture stripped (mail/proxy passwords, ssl keys) can't port —
re-supply only the ones you need via `artifactory_system_config_secrets` (deep-
merged before PATCH, ideally from Vault), same idea as the LDAP bind passwords.

#### One config, many environments (dev / test / prod)

Don't template or copy a per-machine descriptor. Keep the **shared** config in
`group_vars/all` and put only the keys that **differ per environment** (`urlBase`,
`serverName`, a mail host…) in `inventories/<env>/group_vars` via
`artifactory_system_config_yaml_overrides`. The role deep-merges
**base ◁ per-env overrides ◁ secrets** (recursive, so a nested override changes
one leaf and keeps its siblings) and PATCHes the result — same playbook, just
`-i inventories/<env>`:

```yaml
# group_vars/all/artifactory.yml          — shared across every environment
artifactory_system_config_yaml:
  fileUploadMaxSizeMb: 100
  trashcanConfig: {enabled: true, retentionPeriodDays: 14}
  backups:
    backup-daily: {enabled: true, cronExp: "0 0 2 ? * MON-FRI"}

# inventories/prod/group_vars/all/artifactory.yml   — only what's prod-specific
artifactory_system_config_yaml_overrides:
  urlBase: "https://artifactory.prod.example.com/artifactory/"
  serverName: artifactory-prod

# inventories/dev/group_vars/all/artifactory.yml
artifactory_system_config_yaml_overrides:
  urlBase: "https://artifactory.dev.example.com/artifactory/"
  serverName: artifactory-dev
  trashcanConfig: {retentionPeriodDays: 7}   # nested override; `enabled` stays true
```

You template the *data* (mergeable, version-neutral) and let the YAML PATCH
serialise it against whatever schema each box runs — never a hand-templated,
schema-stamped XML file. (The merge is computed into an internal fact, so it
holds even if a base is passed via `--extra-vars`.)

#### Scope — the YAML config-as-code is a deliberate SUBSET

Not everything in `artifactory.config.latest.xml` is applied here, **by JFrog's
design** — so a restore that doesn't reproduce the whole descriptor is expected,
not a bug. The config-as-code surface has shrunk over 7.x:

| Config | Lives in | Managed by (this role) |
|---|---|---|
| General / proxies / mail / backups / property sets / repo layouts / replication toggles / indexer / GC | the descriptor (YAML config-as-code) | this section (YAML PATCH) |
| **Repositories** (left the descriptor in 7.49.x) | `artifactory.repository.config.*.json` / Repositories REST | `artifactory_*_repositories` |
| **LDAP / SAML / OAuth / Crowd / password policy** (left the descriptor in 7.59+) | the **Access** service | `artifactory_ldap_*` (Access API) / `artifactory_*_config` |
| Install (DB, filestore, ports, keys) | `system.yaml` | out of scope — the install role |

Two more reasons a captured key may land in `dropped`: the shipped descriptor's
element **names differ from the YAML/REST key names** (JAXB XML vs the config-as-
code schema — the `artifactory.xsd` is authoritative, and the example XML JFrog
ships is itself incomplete), and many descriptor fields are read-only/computed.
Dropped keys are overwhelmingly these — name divergence and non-writable
internals — not meaningful settings silently lost. Check the `dropped` list; if
a setting you care about is there, set it explicitly in
`artifactory_system_config_yaml` under its config-as-code key name.

> Raw-XML full-replace of the descriptor is deliberately NOT supported: it's
> xsd-version-stamped and carries master-key-encrypted secrets, so it can't port
> across versions/instances (JFrog's own guidance is "do not modify
> `artifactory.config.xml` directly"). The YAML PATCH above is the only path.

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

Set `artifactory_export_format: json` to write the reference capture as JSON
instead of YAML (it then ends in `.json`). Real estates produce large files
(thousands of lines) — that's expected; it's the full config. (Reference only —
desired state is still group_vars.)

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
├── vars/main.yml              # API endpoint constants
├── meta/{main,argument_specs}.yml
├── tasks/                     # main → preflight → backup | apply → per-section files
├── filter_plugins/            # drop_empty, config_diff, descriptor_to_config, to_pretty_yaml
├── library/                   # artifactory_config_apply (version-adaptive system-config PATCH)
├── examples/multitenant.yml   # project-per-tenant reference design
└── docs/api-reference.md      # full API surface, statuses, shapes
```

## Coverage

The role round-trips: `backup` (full capture), `apply` greenfield (repos
local/remote/virtual, groups, permissions, projects + members, Xray policies +
watches), and `apply` with `state: absent` (deletes in correct dependency order).
