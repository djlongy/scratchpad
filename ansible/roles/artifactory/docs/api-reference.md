# Artifactory Enterprise — REST API Surface for Configuration-as-Code

Map of every configuration endpoint the `artifactory` role uses or could use,
with status, shape, and the verbs needed for backup/restore/CRUD.

> **SaaS vs self-hosted.** "Artifactory Online" (SaaS/cloud) blocks a handful of
> endpoints that JFrog manages for you — most importantly the global **config
> descriptor** (`/api/system/configuration`) and the mail/reverse-proxy config.
> On **self-hosted Enterprise** those work. Every row below is tagged `[both]`,
> `[self-hosted]`, or `[saas-blocked]`. Self-hosted targets support the
> descriptor; SaaS does not.

Auth: `Authorization: Bearer <admin access token>` on every call. A token scoped
to Artifactory admin can read `/artifactory/...` but may get **403** on some
`/access/...` admin endpoints (users/groups/config) — those need a
**platform-admin** token. Observed 403s are noted.

Services and base paths:

| Service | Base | Purpose |
|---|---|---|
| Artifactory | `/artifactory/api` | repos, security (users/groups/perms), SSO, replications |
| Access | `/access/api/v1` | projects, environments, tokens, LDAP, **HashiCorp Vault**, certs |
| Xray | `/xray/api/{v1,v2}` | policies, watches, ignore rules, indexing |
| Event | `/event/api/v1` | webhooks |

---

## 1. System / identity (read-only)

| Method | Path | Avail | Shape / notes |
|---|---|---|---|
| GET | `/artifactory/api/system/ping` | both | `OK` (text) |
| GET | `/artifactory/api/system/version` | both | `{version, revision, addons[]}` |
| GET | `/artifactory/api/system/license` | both | `{type, validThrough, licensedTo, subscriptionType}` |
| GET | `/artifactory/api/system/service_id` | both | `jfrt@...` (text) |
| GET | `/artifactory/api/system/security/certificates` | both | `[]` list of imported TLS certs |
| GET | `/artifactory/api/storageinfo` | both | `{repositoriesSummaryList[], fileStoreSummary, binariesSummary}` |
| GET | `/access/api/v1/system/version` | both | `{name, revision, timestamp}` |
| GET | `/access/api/v1/cert/root` | both | root CA cert (text) |
| GET | `/artifactory/api/system` (system info) | **saas-blocked** (400) | self-hosted only |
| GET | `/artifactory/api/system/licenses` (HA) | **saas-blocked** (400) | self-hosted HA cluster |

## 2. Global configuration descriptor `[self-hosted]`

The single XML/YAML document holding **general settings, proxies, backups, mail
server, property sets, repository layouts, security-general, sumo/bintray**.

| Method | Path | Avail | Notes |
|---|---|---|---|
| GET | `/artifactory/api/system/configuration` | self-hosted (400 on SaaS) | returns XML descriptor |
| POST | `/artifactory/api/system/configuration` | self-hosted | full XML replace (heavy-handed) |
| PATCH | `/artifactory/api/system/configuration` | self-hosted | `Content-Type: application/yaml` — additive, supported, **the** way to manage proxies/backups/layouts/property-sets/general as code |

Role: `artifactory_system_config_yaml` (PATCH) + `artifactory_system_config_xml`
(captured raw, optional full-replace). On SaaS the role tolerates the 400 and
skips this section.

## 3. Repositories `[both]`

| Method | Path | Shape / notes |
|---|---|---|
| GET | `/artifactory/api/repositories` | summary list `[{key, type, url, packageType}]` |
| GET | **`/artifactory/api/repositories/configurations`** | **all repos, full config, one call** → `{LOCAL[], REMOTE[], VIRTUAL[], FEDERATED[]}`. Best source for backup. |
| GET | `/artifactory/api/repositories/{key}` | full config for one repo (has lowercase `rclass`) |
| PUT | `/artifactory/api/repositories/{key}` | **create** (201). Body = full repo config object. |
| POST | `/artifactory/api/repositories/{key}` | **update** (200) |
| DELETE | `/artifactory/api/repositories/{key}` | **delete** (200) — surgical removal |

`rclass`: `local | remote | virtual | federated`. `packageType`: docker, maven,
npm, pypi, generic, helm, go, nuget, cargo, debian, rpm, conan, … Remote repos
carry `url` + upstream creds; virtual repos carry `repositories[]` +
`defaultDeploymentRepo`. **Multitenant** = per-tenant locals per stage + shared
remotes + per-stage virtuals aggregating them (see `examples/multitenant.yml`).

Strip before write: `revision` (computed).

## 4. Security — users, groups, permissions `[both]`

| Method | Path | Notes |
|---|---|---|
| GET | `/artifactory/api/security/users` | list `[{name, uri, realm}]` |
| GET | `/artifactory/api/security/users/{name}` | detail — **never returns password**; `{name,email,admin,profileUpdatable,internalPasswordDisabled,groups,realm,...}` |
| PUT | `/artifactory/api/security/users/{name}` | create (needs `password`) |
| POST | `/artifactory/api/security/users/{name}` | update (password optional) |
| DELETE | `/artifactory/api/security/users/{name}` | delete |
| GET | `/artifactory/api/security/groups` + `/{name}` | `{name,description,autoJoin,adminPrivileges,realm,userNames[],policyManager,...}` |
| PUT/DELETE | `/artifactory/api/security/groups/{name}` | upsert / delete |
| GET | `/artifactory/api/v2/security/permissions` + `/{name}` | v2 target `{name, repo{repositories[],actions{users{},groups{}}}, build, releaseBundle}` |
| PUT/DELETE | `/artifactory/api/v2/security/permissions/{name}` | upsert / delete. Actions: `read,write,annotate,delete,manage,managedXrayMeta,distribute` |
| GET | `/artifactory/api/security/permissions` (v1) | legacy, still present |
| GET | `/artifactory/api/security/lockedUsers` | `[]` |
| GET | `/artifactory/api/security/apiKey` | `{blockCreateApiKey}` (API keys deprecated) |
| — | `/artifactory/api/security/keys/trusted` | **403** (Enterprise multi-key only) |

## 5. Authentication / SSO integrations `[both — dedicated endpoints]`

These have their **own endpoints** and work even on SaaS (unlike the descriptor).

| Method | Path | Shape |
|---|---|---|
| GET/POST | `/artifactory/api/saml/config` | `{enableIntegration, loginUrl, logoutUrl, serviceProviderName, certificate, useEncryptedAssertion, syncGroups, groupAttribute, emailAttribute, noAutoUserCreation, allowUserToAccessProfile, autoRedirect}` |
| GET/POST | `/artifactory/api/oauth` | `{enabled, persistUsers, availableTypes[], providers[], allowUserToAccessProfile}` |
| GET/POST | `/artifactory/api/crowd` | `{enableIntegration, sessionValidationInterval, useDefaultProxy, noAutoUserCreation, directAuthentication, ...}` |
| GET | `/access/api/v1/ldap/settings` | LDAP settings list (Access-managed). `[]` when none. **LDAP groups** under `/access/api/v1/ldap/groups`. |

> Older `/artifactory/api/system/configuration` also carries LDAP/SAML; prefer
> the dedicated endpoints above for SaaS compatibility.

## 6. Projects, roles, members, stages (Access) `[both]`

| Method | Path | Notes |
|---|---|---|
| GET | `/access/api/v1/projects` | list (empty on fresh) |
| GET | `/access/api/v1/projects/{key}` | detail `{project_key, display_name, description, admin_privileges, storage_quota_bytes}` |
| POST | `/access/api/v1/projects` | create |
| PUT | `/access/api/v1/projects/{key}` | update |
| DELETE | `/access/api/v1/projects/{key}` | delete |
| GET | `/access/api/v1/projects/{key}/roles` | built-in + custom roles (9 built-ins incl. "AppTrust Manager", "Developer", "Contributor", …) |
| POST/PUT/DELETE | `/access/api/v1/projects/{key}/roles[/{name}]` | manage custom roles |
| GET/PUT/DELETE | `/access/api/v1/projects/{key}/users/{name}` | member user → `{roles[]}` |
| GET/PUT/DELETE | `/access/api/v1/projects/{key}/groups/{name}` | member group → `{roles[]}` |
| PUT | `/access/api/v1/projects/_/attach/repositories/{repoKey}/{projectKey}?force=true` | assign repo to project |
| GET | **`/access/api/v1/environments`** | **global lifecycle stages** → `[{name:DEV},{name:PROD}]` |
| POST/DELETE | `/access/api/v1/environments` | create/delete custom stages |

**Project-per-tenant** model: one project per team; project key auto-prefixes
repos; members mapped to roles per stage; environments drive promotion.

## 7. HashiCorp Vault integration (Access) `[both]`

| Method | Path | Notes |
|---|---|---|
| GET | **`/access/api/v1/vault/configs`** | list configured HashiCorp Vault connections (`[]` on fresh) |
| GET | `/access/api/v1/vault/configs/{name}` | one connection |
| POST/PUT | `/access/api/v1/vault/configs/{name}` | create/update — `{name, url, auth{type: token|appRole|...}, mounts[]}` |
| DELETE | `/access/api/v1/vault/configs/{name}` | remove |

Lets JFrog pull secrets (e.g. signing keys, remote-repo creds) from HashiCorp
Vault instead of storing them locally.

## 8. Xray — policies, watches, ignore rules, indexing `[both, Enterprise+Xray]`

| Method | Path | Shape |
|---|---|---|
| GET | `/xray/api/v1/system/version` | `{xray_version, xray_revision}` |
| GET | `/xray/api/v2/policies` | `{result:[{name,type,author,created,modified}]}` |
| GET | `/xray/api/v2/policies/{name}` | `{name, type, rules[], ...}` (type: `security`\|`license`\|`operational_risk`) |
| POST/PUT/DELETE | `/xray/api/v2/policies[/{name}]` | manage |
| GET | `/xray/api/v2/watches` (+ `/{name}`) | `{general_data{id,name,active}, project_resources{resources[]}, assigned_policies[], ticket_generation}` |
| POST/PUT/DELETE | `/xray/api/v2/watches[/{name}]` | manage |
| GET | `/xray/api/v1/ignore_rules` | `{data[], total_count}` |
| POST/DELETE | `/xray/api/v1/ignore_rules[/{id}]` | manage |
| GET | `/xray/api/v1/binMgr/{id}/repos` | `{bin_mgr_id, indexed_repos[], non_indexed_repos[]}` — which repos are scanned |
| PUT | `/xray/api/v1/binMgr/{id}/repos` | set indexed repos |
| GET | `/xray/api/v1/violations` | needs `watch_name`/filters (400 without) |
| POST | `/xray/api/v1/reports/{vulnerabilities,licenses,...}` | generate reports |

## 9. Replications & webhooks `[both]`

| Method | Path | Notes |
|---|---|---|
| GET | `/artifactory/api/replications` | global replications list (`[]` fresh) |
| GET | `/artifactory/api/replications/{repoKey}` | per-repo replication config |
| PUT/POST/DELETE | `/artifactory/api/replications/{repoKey}` | manage (push/pull, Enterprise) |
| GET | `/event/api/v1/subscriptions` | webhook subscriptions (`[]` fresh) |
| POST | `/event/api/v1/subscriptions` | create |
| PUT/DELETE | `/event/api/v1/subscriptions/{key}` | update/delete |

## 10. Access tokens `[both]`

| Method | Path | Notes |
|---|---|---|
| GET | `/access/api/v1/tokens` | `{tokens:[...]}` — metadata only, never the secret |
| POST | `/access/api/v1/tokens` | mint (form-encoded: `subject, scope, expires_in, description`). Returns `access_token` once — **not recoverable**, so never part of a backup. |
| DELETE | `/access/api/v1/tokens/{id}` | revoke |

---

## Not available in all editions (noted for completeness)

| Path | Status | Why |
|---|---|---|
| `/artifactory/api/mail`, `.../system/configuration/webServer` | 403 cloud | self-hosted only |
| `/access/api/v1/users`, `/groups`, `/config` | 403 | needs **platform-admin** token (Artifactory-admin token insufficient) |
| `/distribution/...`, `/lifecycle/api/v2/release_bundle/...` | 404 | Distribution / Release Bundles v2 not enabled |
| `/artifactory/api/federation/...` | 404 | needs a federated repo to exist first |
| `/artifactory/api/security/keys/trusted` | 403 | multi-trusted-key is Enterprise+ feature gate |

## Backup → restore round-trip summary

Cleanly round-trips as JSON/YAML (GET shape == write shape): **repositories,
groups, users (minus password), permission targets, projects + roles + members,
environments, SAML/OAuth/Crowd, LDAP, Vault configs, Xray policies/watches/ignore
rules, webhooks, replications.** Cannot round-trip (secrets/computed): **user
passwords, access-token secrets, repo `revision`**. Self-hosted-only: **global
config descriptor (proxies/backups/mail/property-sets/repo-layouts)**.
