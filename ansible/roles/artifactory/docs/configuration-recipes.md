# Configuration recipes

Working role inputs for the trickier sections — the non-obvious shapes the API
actually requires.

## compare (drift detection)

```bash
# Drift: capture live state, diff vs the env's group_vars, write <env>/artifactory.drift.yml
ansible-playbook playbooks/artifactory.yml -i inventories/prod -e artifactory_mode=compare
# add -e artifactory_compare_fail_on_drift=true to gate CI.
```

`config_diff` matches list items by identity key (key/name/project_key/…); the
diff report groups each section into `added` / `changed` / `removed`. Capture
metadata (`_meta`) is excluded via `artifactory_compare_ignore_keys`.

## LDAP groups → privileges (the hard one)

Authentication works with just `artifactory_ldap_settings`. Mapping LDAP group
membership to **privileges** needs ALL of the following — miss any one and the
user authenticates but resolves only to the default `readers` group:

```yaml
artifactory_ldap_groups:
  - name: freeipa-groups
    enabled_ldap: freeipa
    group_base_dn: "cn=groups,cn=accounts"
    group_name_attribute: cn
    group_member_attribute: memberOf   # DYNAMIC reads this ON THE USER, not 'member'
    description_attribute: description  # REQUIRED by the API (UI flags it)
    filter: "(objectClass=groupOfNames)"
    strategy: DYNAMIC                   # zero-maintenance; resolves memberOf per login
    sub_tree: true

# The Artifactory group must be realm: ldap (an internal group never receives
# LDAP members). Create it via the OLD security API (artifactory_ep.groups),
# which honours realm/realmAttributes — the Access v2 API ignores realm.
artifactory_groups:
  - name: artifactory-admins
    description: "LDAP-mapped admins"
    adminPrivileges: true
    realm: ldap
    realmAttributes: "ldapGroupSettingName=freeipa-groups;groupsStrategy=DYNAMIC;groupDn=cn=artifactory-admins,cn=groups,cn=accounts,dc=example,dc=com"
```

Gotcha: Artifactory caches a user's groups at auto-create time. A user added to
the group *after* their first login won't pick up the new privileges until
their Artifactory user is deleted and they log in fresh.

LDAP bind password ("manager password", blank by default) — supply from Vault:

```yaml
artifactory_ldap_manager_passwords:
  corp-ldap: "{{ lookup('community.hashi_vault.hashi_vault', 'secret=<mount>/data/apps/<svc>/runtime:ldap_manager_password') }}"
```

## System config — server name, base URL (safe)

```yaml
artifactory_system_config_yaml:
  serverName: artifactory.example.com
  urlBase: https://artifactory.example.com
```

These round-trip cleanly via the descriptor PATCH (`application/yaml`), applied
per-block and fail-soft. Keys mirror the descriptor element names **flattened** —
there is **no `general:` wrapper** (the PATCH rejects it). Named collections are
**keyed maps**, not lists — e.g. `backups:` is keyed by the backup `key`,
`reverseProxies:`/`proxies:` by `key`, `propertySets:`/`repoLayouts:` by `name`:

```yaml
artifactory_system_config_yaml:
  fileUploadMaxSizeMb: 100
  trashcanConfig: {enabled: true, retentionPeriodDays: 14}
  backups:
    backup-daily: {enabled: true, cronExp: "0 0 2 ? * MON-FRI"}
```

You normally don't hand-write this from scratch: a backup captures the whole
descriptor and writes a PATCH-ready `*.system-config.reference.yml` (named root,
reference only) — copy the dict (or just the blocks you want) into group_vars.
See the role README's "System config descriptor" section.

## ⚠️ Docker access method — do NOT set `dockerReverseProxyMethod: subDomain` on a `direct` proxy

**Footgun:** setting

```yaml
reverseProxies:
  - key: direct
    webServerType: direct           # <-- the problem
    dockerReverseProxyMethod: subDomain
    serverNameExpression: "*.host"
```

puts the UI into an **infinite redirect loop on every page** — Artifactory tries
to redirect the UI into the subdomain form, which loops. The `direct` web-server
type and `subDomain` method are an invalid combination.

The Sub-Domain method works **without** this setting when an external nginx
sidecar rewrites `<repo>.<host>/v2/...` → `/artifactory/api/docker/<repo>/v2/...`,
so **keep Artifactory at the default `path`/`direct`** and let the proxy do the
routing — docker login/push/pull via the subdomain still works.
The trade-off is only cosmetic (the "Set Me Up" tab shows Repository Path). If you
want the UI to *display* Sub Domain, set `webServerType: nginx` (not `direct`) and
generate the matching reverse-proxy config — but `direct + subDomain` must be
avoided.

## Anonymous read-only consumption (token-less pull)

Enable anonymous access in the descriptor, then grant the built-in `anonymous`
user read on the specific repos (no deploy):

```yaml
artifactory_system_config_yaml:
  security:
    anonAccessEnabled: true
artifactory_permissions:
  - name: anon-read-shared
    repo:
      repositories: [docker-local, shared-generic-local]
      actions:
        users:
          anonymous: [read]   # read only — never write/deploy
```

## Xray reports

```yaml
artifactory_xray_reports:
  - name: critical-vulns-docker
    type: vulnerabilities        # vulnerabilities|licenses|violations|operationalRisks
    resources:
      repositories: [{name: docker-local}]
    filters:
      severities: [High, Critical]
```

Idempotent by name (create-if-absent). The list endpoint is a `POST` (with a
pagination body), not a GET. Report *data* only populates once Xray's
vulnerability DB has finished its initial sync.

## OAuth / OIDC SSO

`artifactory_oauth_config` accepts global settings and an optional `providers`
list. The role reads existing providers, creates missing names with
`PUT /artifactory/api/oauth/provider`, updates existing names with `POST` to that
endpoint, then applies global settings through `POST /artifactory/api/oauth`.
It verifies provider fields by readback; unrelated providers are preserved.
The global endpoint alone returns success but does not create providers.

Supply each provider's name, enabled flag, providerType, client id, secret and
provider endpoints. OpenID Connect uses `providerType: openId`; a public PKCE
client uses `pkce: true` and an empty secret. Store confidential client secrets
in Vault and supply them at apply time: readback cannot validate masked secrets.
The role reapplies supplied providers, including secrets, on each run.

Native provider creation and a real Keycloak/FreeIPA PKCE session were tested
on the disposable 7.146.17 lab. Other releases and confidential clients still
need runtime validation. `/access/api/v1/oidc/configurations` configures CI token
exchange, rather than interactive SSO.
