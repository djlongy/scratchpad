# freeipa_server_rbac_roles — reference

Ansible-module-style documentation for the **thin RBAC overlay** input of the
`freeipa_server` role. Declare abstract ROLES; the role compiles them into its native
`freeipa_idam_*` lists at apply time. Nothing else is generated — sudo rules, hostgroups,
DNS, automember and IPA delegation roles stay plain native entries.

## Synopsis

- A role is a **plain IPA usergroup** (created with the literal `name` you declare) that is
  **nested into** the existing policy groups listed in `member_of`. A user in the role group
  becomes an *indirect* member of every target group, so the native HBAC/sudo rules that
  target those groups apply unchanged.
- Granting a role = adding a login to the entry's `members` list — a one-line diff. The
  user's own `groups:` list is never touched.
- Optionally, a role may carry its own **role-scoped HBAC rules** (`hbac_rules`): the
  compiler injects `usergroup: [<the role group>]` into each and merges them onto
  `freeipa_idam_hbac_rules`.
- **WYSIWYG**: every name is used verbatim. There are no naming templates — scope
  (tenant/environment/service) lives in the names you declare.
- Validated **fail-fast before any apply**: unknown keys, missing target groups, unknown
  users, protected-group collisions and duplicate/conflicting rule names all abort the run
  with a message naming the entry.

## Requirements

- The `member_of` target groups must already exist natively (declared in
  `freeipa_idam_usergroups` or already present in the realm export) — the overlay nests
  into them, it does not invent them.
- Users listed in `members` must exist natively (`freeipa_idam_users` or the realm).

## Parameters

`freeipa_server_rbac_roles` — **list** of role entries. Default: `[]` (overlay off).

| Parameter | Type | Required | Default | Description |
|---|---|---|---|---|
| `name` | str | **yes** | — | The role group name, created verbatim (e.g. `role-acme-prod-platform-admin`). Must not collide with a protected FreeIPA built-in (`admins`, `editors`, `ipausers`, `trust admins`) or be declared twice. |
| `description` | str | no | — | Description set on the role group. |
| `member_of` | list of str | **yes** | — | EXISTING policy/target group names the role group is nested into (paste them from the `--tags export` snapshot). At least one is required — a role granting nothing is rejected. |
| `members` | list of str | no | `[]` | User logins granted the role (become indirect members of every `member_of` group). Each login must exist natively. |
| `hbac_rules` | list of dict | no | `[]` | OPTIONAL role-scoped HBAC rules — see the suboptions below. Each rule is bound to the role: the compiler injects `usergroup: [<name>]`. |

### `hbac_rules[]` suboptions

| Parameter | Type | Required | Choices / Default | Description |
|---|---|---|---|---|
| `name` | str | **yes** | — | The HBAC rule name, declared explicitly (WYSIWYG — no generated names). A rule name may live under exactly ONE role and must not collide with a natively declared rule. |
| `description` | str | no | — | Description on the rule. |
| `hostgroup` | list of str | no | — | Host groups the rule applies to. Not combinable with `hostcategory: all`. |
| `host` | list of str | no | — | Individual hosts the rule applies to. Not combinable with `hostcategory: all`. |
| `service` | list of str | no | — | HBAC services (e.g. `sshd`, `cockpit`). Not combinable with `servicecategory: all`. |
| `servicegroup` | list of str | no | — | HBAC service groups. Not combinable with `servicecategory: all`. |
| `hostcategory` | str | no | `all` / `""` | `all` opens the rule to every host (`""` clears a previously set category). Mutually exclusive with `host`/`hostgroup` — IPA rejects explicit members on an all-category axis. |
| `servicecategory` | str | no | `all` / `""` | `all` opens the rule to every service (`""` clears it). Mutually exclusive with `service`/`servicegroup`. |
| `user` | list of str | no | — | EXTRA specific users on the rule beyond the role members (edge case). |
| `state` | str | no | `present` / `enabled` / `disabled` / `absent` | Rule lifecycle. `enabled`/`disabled` is reconciled in a separate operational-state pass (the module forbids members alongside those states). `absent` deletes the rule by name — the role strips every other declared field from the call (ipahbacrule rejects them with `state: absent`). With reconcile mode on you rarely need it: simply removing the rule from the list prunes it; `state: absent` is for targeted deletion in additive (non-reconcile) runs. |

**Rejected keys (with targeted errors):**

- `usergroup` / `group` — the compiler injects `usergroup: [<the role group>]` itself;
  binding the rule to the role is the point.
- `usercategory` — incompatible with a role-scoped rule: IPA refuses member users/groups
  alongside `usercategory: all`, and every role-scoped rule carries the injected role
  usergroup. Declare an all-users rule in the baseline `freeipa_idam_hbac_rules` instead
  (the baseline dicts support `usercategory`/`hostcategory`/`servicecategory` natively).

## Notes

- The overlay is **additive-declarative**: it merges onto the native lists and goes through
  the same reference validation and (where enabled) reconcile/prune machinery as everything
  else. Baseline wins on conflicts — a generated name colliding with a native entry is an
  error, not a silent merge.
- In **tenants mode** each `tenants/*.yml` file may carry its own
  `freeipa_server_rbac_roles` slice; slices concatenate across files.
- `member_of` nesting is the primary model. Use `hbac_rules` only when a rule genuinely
  belongs to the role itself rather than to a reusable target group.

## Examples

```yaml
# Minimal: one role, nested into two existing policy groups, granted to two users.
freeipa_server_rbac_roles:
  - name: role-acme-prod-platform-admin
    description: "acme/prod platform admins"
    member_of:
      - ug-acme-prod-gitlab-admins
      - ug-acme-prod-docker-operators
    members: [alice, bob]
```

```yaml
# Role-scoped HBAC: SSH to the tenant's hosts comes WITH the role.
freeipa_server_rbac_roles:
  - name: role-acme-prod-operator
    member_of: [ug-acme-prod-app-operators]
    members: [carol]
    hbac_rules:
      - name: hbac-acme-prod-operator-ssh
        hostgroup: [hg-acme-prod]
        service: [sshd]
```

```yaml
# Category axes: every host / every service (usergroup is still the injected role —
# the rule is "role members, anywhere, any service").
freeipa_server_rbac_roles:
  - name: role-acme-prod-breakglass
    member_of: [ug-acme-prod-admins]
    members: [dave]
    hbac_rules:
      - name: hbac-acme-prod-breakglass-any
        hostcategory: all
        servicecategory: all
```

```yaml
# Lifecycle: a disabled (staged) rule + an extra named user beyond the role.
freeipa_server_rbac_roles:
  - name: role-globex-test-observer
    member_of: [ug-globex-test-grafana-readers]
    members: [erin]
    hbac_rules:
      - name: hbac-globex-test-console
        description: "cockpit console (staged, not yet active)"
        hostgroup: [hg-globex-test]
        service: [cockpit]
        user: [contractor1]        # one extra specific user beyond the role
        state: disabled
```

```yaml
# Multi-tenant slices: each tenants/*.yml carries its own roles; they concatenate.
# tenants/acme.yml
freeipa_server_rbac_roles:
  - name: role-acme-prod-platform-admin
    member_of: [ug-acme-prod-gitlab-admins]
    members: [alice]
# tenants/globex.yml
freeipa_server_rbac_roles:
  - name: role-globex-prod-viewer
    member_of: [ug-globex-prod-grafana-readers]
    members: [frank]
```

## See also

- Role `README.md` → *Thin RBAC overlay* — the narrative walkthrough and design rationale.
- `examples/rbac-overlay/` — a runnable example (inventory + group_vars + site.yml).
- Baseline dicts: `freeipa_idam_usergroups`, `freeipa_idam_users`,
  `freeipa_idam_hbac_rules` (supports all three categories), `freeipa_idam_sudo_rules`
  (supports `usercategory`/`hostcategory`/`cmdcategory`/`runasusercategory`/
  `runasgroupcategory`).
