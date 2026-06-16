# Ansible Design Principles

> **Canonical copy lives at `~/.config/skills/ansible-design/REFERENCE.md`** — that
> file is loaded by the global `ansible-design` the tool skill. This copy in the repo
> is a mirror for in-repo discoverability. **Keep both in sync** — when editing,
> update both files in the same commit, or update the skill copy and re-copy here.

A reference for designing maintainable Ansible repositories at scale. Synthesized
from current (2024–2026) community ground truth — Red Hat Communities of Practice,
official Ansible documentation, ansible-lint rules, and the conventions used by
the most-deployed Ansible content (linux-system-roles, DebOps, geerlingguy.*,
robertdebock.*).

Where the community is split, this doc says so. Where I'm presenting a single
defensible school of thought, I cite it. Where something is non-negotiable
(ansible-lint enforced, official guidance), it's marked as such.

**How to use this doc**: read it once, then keep it open as a PR checklist.
Section 14 is a one-page version.

---

## 1. Playbook structure

### 1.1 Playbooks orchestrate roles; roles do the work

Playbooks should be **thin**: a list of roles applied to a target host pattern,
with as little inline task logic as possible.

```yaml
# Idiomatic
- hosts: web_servers
  become: true
  roles:
    - common
    - nginx
    - app
```

Red Hat Communities of Practice (CoP) puts this plainly: *"Don't put too much
logic in your playbook, put it in your roles… try to limit your playbooks to a
list of roles."*¹

There is no authoritative line-count threshold for "too much in a playbook."
ansible-lint's `complexity[tasks]` flags at roughly 100 tasks in a single file.
The pragmatic rule: if a play's `tasks:` section grows beyond a handful of
operations that don't fit cleanly into a role, extract a role.

### 1.2 Don't mix `roles:` and `tasks:` in the same play

This is the **explicit anti-pattern** flagged by Red Hat CoP¹: combining the
`roles:` keyword with a `tasks:` block in the same play hides execution order
(roles run before tasks regardless of source order) and surprises future
readers.

```yaml
# Anti-pattern — roles run first, then tasks; not source-order
- hosts: web_servers
  roles:
    - common
  tasks:
    - debug: msg="this looks like it runs after 'common', but execution order is murky"
```

If you need tasks before or after roles, use `pre_tasks:` / `post_tasks:`
explicitly — those keywords make the ordering visible.

### 1.3 `pre_tasks` and `post_tasks` are for orchestration around roles

The canonical use case is rolling-deploy orchestration: drain a load balancer
before role execution, restore it after.² Ansible's reference `rolling_update.yml`
example uses both keywords heavily — `pre_tasks` to disable monitoring + drain
HAProxy, `post_tasks` to wait for health, then re-enable.

Use them for that. Don't use them as a dumping ground for logic that belongs
inside a role.

### 1.4 Multi-scope playbooks are fine

A playbook can contain multiple plays, each with a different host pattern.
The canonical `lamp_haproxy/site.yml`³ has five plays — `all`, `dbservers`,
`webservers`, `lbservers`, `monitoring` — and is the project's flagship example.

The choice between one playbook with multiple plays vs. multiple playbooks
composed via `import_playbook` is a **style preference**, not a correctness
question:

- **Single playbook, multiple plays**: simpler for tightly-coupled deploys
  (e.g. a LAMP stack that always rolls out together)
- **Top-level playbook + `import_playbook`**: better when layers can be applied
  independently. DebOps uses this pattern — its `site.yml` is purely
  `import_playbook` calls into per-layer playbooks⁴

### 1.5 Variable scoping in playbooks

If a play references `hostvars['other_host']`, that host must have been in the
scope of a prior play in the same run (so its facts were gathered), OR the
referenced data must be static inventory (group_vars / host_vars), which is
always available. **Gathered facts** are the trap.⁵

See §3 for the recommended cross-host data patterns.

---

## 2. Role design

### 2.1 Role names: pure resource nouns, never lifecycle verbs

The community standard is unambiguous: name roles after **what they manage**,
not what action they perform.

| Convention | Examples | Used by |
|---|---|---|
| **Pure noun** | `nginx`, `postgresql`, `firewall`, `vault` | geerlingguy.*, linux-system-roles, robertdebock.*, RHEL System Roles |
| **Noun + qualifier** | `nginx_proxy`, `postgresql_replica`, `docker_server` | DebOps |

Anti-patterns explicitly called out⁶:

- `install_nginx`, `configure_postgres`, `setup_*`
- `sw_*` / `conf_*` / `prov_*` lifecycle prefixes
- `InstallNginx`, `setup-postgres` (any verb-based naming)

`ansible-lint` rule `role-name`⁷ enforces *character composition* (lowercase
alphanumeric + underscore, alphabetic first character) but not naming
philosophy. The philosophy is enforced by community convention — Galaxy
search, Collection naming, and every reference implementation.

### 2.2 Single responsibility = single service, not single lifecycle phase

A common misreading of single-responsibility splits one service into multiple
roles by phase (`install_x`, `configure_x`, `enable_x`). This is **stricter
than mainstream practice** and produces orchestration overhead with no
practical benefit.

The community-standard unit is **one role per service**, with phases as
**internal task files** gated by tags:

```
roles/postgresql/
├── defaults/main.yml
├── meta/
│   ├── main.yml
│   └── argument_specs.yml
├── tasks/
│   ├── main.yml          # imports the others
│   ├── install.yml       # tagged [install]
│   ├── configure.yml     # tagged [configure]
│   ├── users.yml         # tagged [users]
│   ├── databases.yml     # tagged [databases]
│   └── backup.yml        # tagged [backup]
├── handlers/main.yml
├── templates/
├── files/
├── molecule/default/     # per-role tests
└── README.md
```

`tasks/main.yml`:

```yaml
- ansible.builtin.import_tasks: install.yml
  tags: [install]

- ansible.builtin.import_tasks: configure.yml
  tags: [configure]

- ansible.builtin.import_tasks: users.yml
  tags: [users]
  when: postgresql_manage_users | bool

- ansible.builtin.import_tasks: databases.yml
  tags: [databases]
  when: postgresql_manage_databases | bool
```

Full run (`--tags` omitted) installs, configures, and provisions users +
databases. `--tags configure` skips install but re-renders config. `--tags users`
skips install and config and only reconciles users. **One role, all lifecycle
phases, callers pick the slice they want.**

Geerling's `mysql` role⁸ follows this pattern: install + configure + users +
databases + replication in a single role.

### 2.3 Roles are black boxes with documented inputs

Every role exposes its input contract via `defaults/main.yml`. Consumers must
be able to learn what's configurable without reading task code.

**`defaults/main.yml`** — every overridable variable, with comments and
sensible behavioral defaults⁹:

```yaml
# roles/nginx/defaults/main.yml
nginx_listen_port: 80
nginx_listen_port_ssl: 443
nginx_worker_processes: "auto"
nginx_worker_connections: 1024
nginx_keepalive_timeout: 65
nginx_client_max_body_size: "64m"

# Site config — list of dicts; see README for schema
nginx_sites: []

# Where the role should NOT ship a default (env-specific, no sane fallback),
# leave it commented out for discoverability:
# nginx_server_name: ""
```

The Red Hat CoP rule¹: *"Every argument accepted from outside of the role
should be given a default value in `defaults/main.yml`. If there is no
meaningful default, comment it out for discoverability."*

**`vars/main.yml`** — high-precedence non-overridable constants (versions,
fixed paths, package names that the role's task logic depends on):

```yaml
# roles/postgresql/vars/main.yml
postgresql_default_data_dir:
  RedHat: /var/lib/pgsql/data
  Debian: /var/lib/postgresql/{{ postgresql_version }}/main

postgresql_supported_versions: ["13", "14", "15", "16"]
```

`vars/main.yml` sits near the top of the precedence ladder (above play vars,
just below extra-vars and block/task vars).¹⁰ Use it for things the role
**must** control; use `defaults/main.yml` for everything else.

**`meta/argument_specs.yml`** (Ansible 2.11+) — input validation:

```yaml
argument_specs:
  main:
    short_description: "Install and configure nginx"
    options:
      nginx_listen_port:
        type: int
        default: 80
      nginx_sites:
        type: list
        elements: dict
        default: []
        options:
          server_name:
            type: str
            required: true
          root:
            type: str
            required: true
```

Argument specs surface bad inputs at the start of a play instead of mid-run
with a cryptic Jinja error.¹¹

### 2.4 Variable naming: prefix with the role name

Every variable a role consumes should be prefixed with the role name:
`nginx_port` not `port`, `postgresql_data_dir` not `data_dir`. This is
enforced by `ansible-lint var-naming[no-role-prefix]`¹².

**Why**: roles get composed. Two roles each declaring `port` will collide
silently with whichever was loaded last winning. Prefixing makes ownership
obvious and prevents cross-role variable pollution.

### 2.5 No cross-role state coupling

Roles should not depend on facts set by other roles. If `role_a` does
`set_fact: x: 1` and `role_b` reads `x`, you've created an invisible
**run-order dependency**.

```yaml
# Anti-pattern
# role_a/tasks/main.yml
- ansible.builtin.set_fact:
    shared_url: "https://example.internal"

# role_b/tasks/main.yml
- ansible.builtin.uri:
    url: "{{ shared_url }}/api"      # invisible coupling; breaks if role_b runs alone
```

The right pattern: `role_b` declares the input in its `defaults/main.yml`, the
caller wires it explicitly in the playbook.

```yaml
# role_b/defaults/main.yml
role_b_base_url: ""

# role_b/tasks/main.yml
- ansible.builtin.assert:
    that: role_b_base_url | length > 0
    fail_msg: "role_b_base_url must be set"

# Playbook composes both
- hosts: targets
  roles:
    - role: role_a
    - role: role_b
      vars:
        role_b_base_url: "{{ role_a_published_url }}"   # explicit wiring
```

`meta/main.yml` `dependencies:` is the official mechanism for role-to-role
ordering, but current consensus prefers **explicit `include_role` / `import_role`
with `vars:`** because it makes ordering and shared state visible at the call
site.¹³

### 2.6 Test roles with Molecule

Per-role testing with Molecule is the modern community standard.¹⁴ Minimum
viable layout:

```
roles/<role>/molecule/default/
├── molecule.yml      # driver config (podman/docker)
├── converge.yml      # invokes the role
└── verify.yml        # assertions about post-state
```

```bash
cd roles/<role>
molecule test          # full lifecycle: create → converge → verify → destroy
molecule converge      # just apply the role; useful during iteration
molecule verify        # run assertions against an existing converged container
```

Run Molecule in CI on PRs that touch a role.

### 2.7 README per role

Required. Show:
- One-paragraph summary of what the role does
- Minimal-config usage example
- Table of the public variables (or a pointer to `defaults/main.yml`)
- Any role dependencies
- Any non-obvious prerequisites (e.g. "requires PostgreSQL 14+")

---

## 3. Variable scoping

### 3.1 Precedence ladder (the rungs that matter daily)

The full ladder¹⁰ has 22 rungs. The ones you'll actually adjust:

```
LOW PRECEDENCE
  1. role defaults (defaults/main.yml)
  2. inventory group_vars/all
  3. playbook group_vars/all
  4. inventory group_vars/<group>
  5. playbook group_vars/<group>
  6. inventory host_vars/<host>
  7. playbook host_vars/<host>
  8. host facts / cached set_facts
  9. play vars
 10. play vars_files
 11. role vars (vars/main.yml)
 12. block vars
 13. task vars
 14. include_role / import_role params
 15. set_fact / registered vars
 16. extra-vars (--extra-vars / -e)
HIGH PRECEDENCE
```

Two non-obvious points new users miss:

- **`vars/main.yml` is HIGH precedence** (rung 11). It overrides group_vars and
  host_vars. That's why it's for constants, not for "default-ish" values.
- **Extra-vars always wins** (rung 16). In AAP/AWX, job-template extra-vars
  override everything else — design accordingly.

### 3.2 Where each kind of value belongs

| Kind | Lives in |
|---|---|
| Behavioral default (port, timeout, feature flag) | `role/defaults/main.yml` |
| Non-overridable role constant (version, package map) | `role/vars/main.yml` |
| Fleet-wide value (`domain: example.com`) | `inventory/group_vars/all.yml` |
| Per-environment value (`env: prod`) | `inventory/<env>/group_vars/all.yml` |
| Per-group value (`postgresql_version: 16`) | `inventory/<env>/group_vars/<group>.yml` |
| Per-host exception (rare) | `inventory/<env>/host_vars/<host>.yml` |
| Secret | Vault lookup or ansible-vault-encrypted file |
| One-off override | `--extra-vars` |

**Defaults should ship behavioral defaults**, not be left empty. Empty defaults
hurt role discoverability — users can't see what's configurable without reading
task code.¹ The exception is **env-specific values** (IPs, domains, env labels)
that genuinely have no sane fallback — leave those commented out in
`defaults/main.yml` for discoverability and require the inventory to set them.

### 3.3 `hostvars[X]`: when it's right, when it's redundant, when it's a trap

**Self-reference is redundant**:

```yaml
# Redundant — same as `ansible_host`
{{ hostvars[inventory_hostname].ansible_host }}

# Redundant — same as `my_var` (when my_var is in scope)
{{ hostvars[inventory_hostname].my_var }}
```

For the current host, use the variable directly. `hostvars[X]` is for
referencing **other** hosts.

**Cross-host inventory data is safe**:

```yaml
# Works in any play, any scope — inventory data is always loaded
{{ hostvars['db01'].ansible_host }}
```

**Cross-host gathered facts are the trap**:

```yaml
- hosts: web_servers
  tasks:
    - debug: msg="{{ hostvars['db01'].ansible_default_ipv4.address }}"
      # ↑ FAILS if db01 wasn't in a prior play's scope — facts weren't gathered
```

The fix: include the host in a prior play (even a no-op `gather_facts` play),
use `delegate_to:` with `delegate_facts: true`, or — best — encode the value
in inventory so no fact-gathering is needed.⁵

### 3.4 Cluster topology: derive from inventory, don't curate parallel lists

The canonical pattern for "give me the addresses of every node in this cluster"
is to derive from inventory groups:

```yaml
# group_vars/all.yml — nothing here; the inventory IS the source of truth

# Anywhere
vault_peer_addrs: "{{ groups['vault'] | map('extract', hostvars, 'ansible_host') | list }}"
```

This stays in sync with inventory automatically. A hand-curated
`vault_cluster: [{name: ..., addr: ...}]` list in group_vars will drift
the moment someone adds a node to the inventory but forgets the list.

Reserve hand-curated lists in `group_vars/all.yml` for data the inventory
**genuinely can't express** — external IPs, third-party endpoints, fleet-wide
shared constants.

### 3.5 `delegate_to` and `run_once` for the play-once pattern

```yaml
- name: Bootstrap an external API once per playbook run
  ansible.builtin.uri:
    url: https://api.example.com/init
  run_once: true
  delegate_to: localhost
```

This is the standard idiom for one-shot tasks that shouldn't run per-host.

For cross-host fact discovery (gather facts about `db01` while running a play
against `web_servers`):

```yaml
- ansible.builtin.setup:
  delegate_to: db01
  delegate_facts: true
  run_once: true
```

After this, `hostvars['db01']` is populated in the current play.

---

## 4. Tags

### 4.1 The default model: tags are refinement, not enablement

Running with no tags should run everything. Tags **narrow** the work for fast
iteration, they don't gate it.¹⁵ Red Hat CoP¹: *"Don't set tags which can't be
used on their own, or can be destructive if used on their own."*

```yaml
# In the role
- ansible.builtin.import_tasks: install.yml
  tags: [install]
- ansible.builtin.import_tasks: configure.yml
  tags: [configure]
```

Usage:
- `--tags install` → installs only
- `--tags configure` → reconfigures without re-installing
- No tags → everything

### 4.2 The `never` tag is the sanctioned enablement exception

Some operations should require explicit opt-in: data wipes, force-rebuilds,
break-glass procedures. The `never` tag is built for this¹⁶:

```yaml
- name: Wipe and reinitialise PostgreSQL data directory
  ansible.builtin.shell: rm -rf {{ postgresql_data_dir }}/*
  tags: [never, reinit_postgresql]
```

This task **only** runs with `--tags reinit_postgresql`. A normal `--tags`
invocation skips it; running with no tags skips it.

### 4.3 The `always` tag

Runs even when `--tags` is restricted, unless `--skip-tags always` is set.
Useful for assertions and fact-gathering that downstream tagged tasks depend on:

```yaml
- name: Validate required inputs
  ansible.builtin.assert:
    that: postgresql_version in postgresql_supported_versions
  tags: [always]
```

### 4.4 Tag schools: pick one and document it

The community has at least three legitimate tag conventions. None is "correct";
pick one per repo and apply consistently.

**School A: phase tags** (`install`, `configure`, `certs`, `secrets`,
`backup`). Concise. Works well when phases are roughly the same across roles.
Risk: collisions across roles (everyone's `configure` is different).

**School B: role-prefixed phase tags** (`nginx_install`, `nginx_configure`).
Eliminates collisions. More verbose. Used by some large enterprise repos.

**School C: role-toggle + skip tags** (`role::nginx`, `skip::nginx`). Used by
DebOps¹⁷. Coarse-grained: tags select WHICH role runs, not WHICH phase. Pair
with `--skip-tags skip::nginx` to disable a specific role.

**School D: untagged tasks, caller-side tagging** (geerlingguy⁸). Roles ship
no internal tags; the playbook tags the role invocation itself. Works because
geerlingguy roles are small (single-purpose) so phase-level slicing isn't
needed.

If you have no preference, **start with School A or B**. Move to B if you find
yourself wanting to tag-target a specific role's phase in a multi-role
playbook.

### 4.5 Per-task fine-grained tags are sometimes legitimate

Don't tag every task with a unique label as a default habit — that's the
"tags as enablement" anti-pattern. But these uses are real:

- **`never`-gated destructive ops**: each must be uniquely addressable
- **Molecule testing**: `tags: [molecule-notest]` to skip slow/network-bound
  tasks during converge runs
- **Hotfix targeting**: in a long-lived monolithic playbook, a single-task tag
  lets an operator re-run just one fix without re-running a 40-minute role
- **Idempotency-expensive tasks**: large image pulls, slow unarchives — give
  them a skip-tag so iteration loops can avoid them

### 4.6 `roles:` vs `import_role` vs `include_role` — exact tag mechanics

This is the most-misunderstood part of Ansible's tag system. **The behaviors
are not equivalent.**

| Form | Tag propagation | `--tags install` behavior (when inner tasks tagged `install`) |
|---|---|---|
| `roles:` keyword + role-level tag | Compile-time, propagates to all inner tasks | Inner install-tagged tasks run |
| `import_role: tags: [...]` | Compile-time, propagates to all inner tasks | Inner install-tagged tasks run |
| `include_role: tags: [...]` (bare) | **Runtime; tags apply only to the include itself, NOT inner tasks** | **Include is skipped entirely** (it's not tagged `install`) |
| `include_role: tags: [...], apply: { tags: [...] }` | Runtime, `apply` pushes tags onto inner tasks | Inner install-tagged tasks run |

Official `include_role` docs¹⁸ confirm the `apply:` keyword is the explicit
mechanism for tag propagation through a dynamic include.

```yaml
# Pattern that matches import_role behavior with dynamic include
- ansible.builtin.include_role:
    name: nginx
    apply:
      tags: [nginx]
  tags: [nginx]
```

If you don't need dynamic loop-based inclusion, prefer `import_role` (or the
`roles:` keyword) — tag semantics are simpler.

### 4.7 Tag namespacing

When multiple roles ship `install` and `configure` tags, `--tags install` runs
every role's install phase. Sometimes that's what you want. When it isn't,
prefix tags with the role name (School B above) or use the include_role
`apply:` form to wrap a per-role tag layer:

```yaml
- ansible.builtin.include_role:
    name: nginx
    apply:
      tags: [nginx_role]
  tags: [nginx_role]
```

Now `--tags install` runs every role's install phase; `--tags nginx_role,install`
runs only nginx's install phase.

### 4.8 Tag meta-selectors

| Selector | Effect |
|---|---|
| `--tags all` | Run everything (default) |
| `--tags tagged` | Run only tasks with at least one tag |
| `--tags untagged` | Run only tasks with no tags |
| `--tags <name>` | Run tasks tagged `<name>` |
| `--skip-tags <name>` | Skip tasks tagged `<name>` |

`--tags untagged` is occasionally useful for finding "what tasks did the
playbook author forget to tag" in a codebase that's adopting tagging.

---

## 5. Idempotency

Every task must be safe to re-run. Idempotency is Ansible's core value
proposition; tasks that aren't idempotent break the guarantee for everything
downstream of them.

### 5.1 Prefer native-idempotent modules

```yaml
- ansible.builtin.dnf:        { name: nginx, state: present }
- ansible.builtin.template:   { src: nginx.conf.j2, dest: /etc/nginx/nginx.conf }
- ansible.posix.firewalld:    { port: 80/tcp, state: enabled, permanent: true }
```

### 5.2 Make shell/command idempotent

```yaml
# Use creates: on install/download tasks
- ansible.builtin.unarchive:
    src: https://example.com/binary.tar.gz
    dest: /opt
    remote_src: true
    creates: /opt/binary           # skip if already present
```

`creates:` is available on `unarchive`, `get_url`, `command`, and `shell`. For
version-pinned installs, encode the version in the `creates:` path so an
upgrade actually triggers:

```yaml
- ansible.builtin.unarchive:
    src: "https://example.com/tool/{{ tool_version }}/tool.tar.gz"
    dest: /opt
    remote_src: true
    creates: "/opt/tool-{{ tool_version }}"
```

### 5.3 `changed_when:` for read-only commands

```yaml
- name: Check current firewall default zone
  ansible.builtin.command: firewall-cmd --get-default-zone
  register: _zone
  changed_when: false              # never report changed for a read
```

### 5.4 `failed_when:` for probes

```yaml
- name: Probe whether the postgres user exists
  ansible.builtin.command: id postgres
  register: _probe
  failed_when: false               # absence is information, not failure
  changed_when: false
```

### 5.5 `ansible-lint no-changed-when` enforces this

ansible-lint rule `no-changed-when`¹⁹ flags every `command` / `shell` task
without an explicit `changed_when:` or `creates:`. Treat the lint as
non-negotiable.

---

## 6. Hygiene (ansible-lint enforced)

ansible-lint²⁰ has become the de facto Ansible style guide since 6.x. Run it
in CI and treat failures as build-breaking.

### 6.1 FQCN everywhere

Use the fully-qualified collection name for every module:

```yaml
# Yes
- ansible.builtin.copy: { ... }
- ansible.posix.firewalld: { ... }
- community.general.archive: { ... }

# No
- copy: { ... }              # ansible-lint fqcn[action-core]
- firewalld: { ... }         # ansible-lint fqcn[action]
```

Mandatory since ansible-core 2.10+. Enforced by ansible-lint rule `fqcn`.

### 6.2 Explicit `state:` on every state-aware module

```yaml
- ansible.builtin.dnf:
    name: nginx
    state: present              # explicit, not implicit
```

### 6.3 `name:` on every play, task, and block

ansible-lint enforces `name[play]`, `name[missing]`, `name[casing]`. Names
should be sentence-case (uppercase first letter), describe what the task does
(not what it's checking), and avoid Jinja templating in the name if avoidable.

```yaml
- name: Install nginx                     # good
- name: install nginx                      # bad (name[casing])
- name: Install {{ pkg }}                  # avoid if possible (name[template])
```

### 6.4 Variable naming: prefix with role name

`ansible-lint var-naming[no-role-prefix]`¹² enforces that role-scoped variables
are prefixed with the role name (`nginx_port`, not `port`). See §2.4.

### 6.5 Common ansible-lint rules to know

| Rule | What it flags |
|---|---|
| `fqcn` | Non-fully-qualified module references |
| `name[*]` | Missing or wrongly-cased task names |
| `no-changed-when` | `command`/`shell` without `changed_when:`/`creates:` |
| `no-handler` | Notify of an undefined handler |
| `risky-file-permissions` | `file`/`copy`/`template` without explicit `mode:` |
| `risky-shell-pipe` | Shell pipes without `set -o pipefail` |
| `no-jinja-when` | Jinja syntax inside `when:` (already an expression) |
| `var-naming[no-role-prefix]` | Role variable missing role-name prefix |
| `command-instead-of-module` | Using `command:` when a dedicated module exists |
| `partial-become` | `become:` without `become_user:` |
| `complexity[tasks]` | More than ~100 tasks in one file |

---

## 7. Secrets

Never commit plaintext secrets. Three patterns, in preference order:

### 7.1 Live lookup from a secret store

```yaml
db_password: >-
  {{ lookup('community.hashi_vault.hashi_vault',
            'secret=kv/myapp/runtime:password') }}
```

Best for shared environments — secrets are pulled at execution time, never
committed, rotation doesn't require a code change.

### 7.2 ansible-vault encrypted files

```yaml
# group_vars/all/vault.yml — encrypted with `ansible-vault encrypt`
vault_db_password: !vault | $ANSIBLE_VAULT;1.1;...
```

Workable for self-contained repos; the encryption passphrase becomes the
shared secret instead.

### 7.3 `--extra-vars @file.yml` with a gitignored file

Last resort. Use only for one-off operations.

### 7.4 `no_log:` on secret-handling tasks

```yaml
- ansible.builtin.user:
    name: app
    password: "{{ app_password | password_hash('sha512') }}"
  no_log: true                       # don't print to stdout/log
```

---

## 8. Inventory design

### 8.1 Static inventory layout

```
inventories/
├── prod/
│   ├── hosts.yml                # the host list
│   ├── group_vars/
│   │   ├── all.yml              # prod-wide values
│   │   ├── web_servers.yml
│   │   └── db_servers.yml
│   └── host_vars/
│       └── web01.yml            # exceptions only
└── dev/
    └── ...
```

### 8.2 Host naming

Pick a convention; document it; stick to it. Common patterns that scale:

- **Function + index**: `web01`, `db01`, `db02`
- **Env + function + index**: `prod-web-01`, `dev-db-01`
- **DNS-style with role + env**: `vault01.prod.example.com`

Avoid ad-hoc names (`tom-test`, `the-new-one`) — they don't survive team
turnover.

### 8.3 Groups as logical buckets

Roles target groups, not individual hosts. Add groups liberally; they're cheap.

```yaml
# inventories/prod/hosts.yml
all:
  children:
    web_servers:
      hosts: { web01: {}, web02: {}, web03: {} }
    db_servers:
      hosts: { db01: {}, db02: {} }
    db_primary:
      hosts: { db01: {} }
    db_replica:
      hosts: { db02: {} }
```

### 8.4 Dynamic inventory (inventory plugins)

Since Ansible 2.4, inventory plugins²¹ are the preferred mechanism for cloud,
container, and federated sources:

- `amazon.aws.aws_ec2` — EC2 hosts auto-discovered
- `community.vmware.vmware_vm_inventory` — vSphere VMs
- `kubernetes.core.k8s` — K8s pods
- `ansible.builtin.constructed` — synthesize groups/vars from existing host
  attributes
- `theforeman.foreman.foreman` — Foreman/Katello

Inventory plugins shift some variable-scoping logic from static files into
plugin configuration. Plan for this when designing variable layers.

### 8.5 `host_vars` for exceptions only

If three of five hosts in a group share a value, put it in `group_vars/<group>.yml`
and override the two exceptions in `host_vars/<exception>.yml`. Don't define
the same value in every host_vars file.

---

## 9. Collections

Since 2024, **Collections** are the default packaging unit for new Ansible
content, replacing standalone Galaxy roles for non-trivial work.²² Even for
internal repos, the current guidance is "start with a collection."

A collection groups related roles, modules, plugins, and tests under a single
namespace + version:

```
my_namespace.my_collection/
├── galaxy.yml                    # collection metadata
├── plugins/
│   ├── modules/
│   └── filter/
├── roles/
│   ├── nginx/
│   └── postgresql/
├── playbooks/
└── meta/runtime.yml              # collection requirements + redirects
```

Benefits over standalone roles:
- Atomic versioning of related content
- Shared modules/plugins/filters across the roles in the collection
- Automation Hub / Galaxy distribution
- Required for Red Hat-certified content

For a repo that's already a flat `roles/` tree, migrating to a collection is
non-trivial but mostly mechanical. Defer the migration until you have a
concrete reason (cross-repo reuse, certification, publishing).

---

## 10. Anti-patterns checklist

What the community **actually** flags as wrong, with sources:

| Anti-pattern | Source |
|---|---|
| Mixing `roles:` and `tasks:` in the same play | Red Hat CoP¹ |
| Verb-prefixed role names (`install_nginx`, `setup_postgres`) | ansible-lint `role-name`⁷, oneuptime⁶ |
| Lifecycle-split roles (separate `install_nginx` and `configure_nginx` roles) | DebOps / linux-system-roles convention⁴ |
| `command`/`shell` without `creates:`/`changed_when:` | ansible-lint `no-changed-when`¹⁹ |
| Role variables without a role-name prefix | ansible-lint `var-naming[no-role-prefix]`¹² |
| Non-FQCN module references in new code | ansible-lint `fqcn`²⁰ |
| Empty defaults that should have behavioral defaults | Red Hat CoP¹, geerlingguy⁹ |
| Cross-role `set_fact` coupling (role B reading role A's set_facts) | Polar Squad²³, ansible#46824²⁴ |
| Hand-curated cluster lists in `group_vars` instead of `groups[...]` derivation | Red Hat CoP¹ |
| Tags as enablement (everything tagged; no-tags run does nothing) | Red Hat CoP¹ |
| Using `meta/main.yml dependencies:` instead of explicit `include_role` wiring | Current consensus¹³ |
| Per-task `name:` missing or lowercase | ansible-lint `name[*]`²⁰ |
| File modules without explicit `mode:` | ansible-lint `risky-file-permissions`²⁰ |
| Shell pipes without `pipefail` | ansible-lint `risky-shell-pipe`²⁰ |

---

## 11. Migration strategy: inheriting an unprincipled codebase

Don't try to fix everything in one pass.

### Phase 1: instrument

- Add yamllint + ansible-lint to CI. Fail PRs that *introduce new* violations;
  grandfather existing ones via inline `# noqa` or `.ansible-lint` config.
- Inventory existing anti-patterns (which roles ship empty defaults, which
  have hardcoded env-specific values, which couple via `set_fact`). Make this
  a tracked artifact.

### Phase 2: thin out monolithic playbooks

Pick the largest playbook. Identify clusters of tasks with a single
responsibility. Extract each into a role (one per service, not per phase).
Replace the task block with a role invocation. Verify with `--check --diff`
that nothing semantically changed. Commit. Move on.

### Phase 3: consolidate split-by-phase roles

If the repo has `install_nginx` + `configure_nginx` (or worse, `nginx_install`
/ `nginx_configure` / `nginx_users` as separate roles), consolidate into one
`nginx` role with task files per phase, gated by tags. Update playbooks to
drop the lifecycle composition.

### Phase 4: push env-specifics out of roles into inventory

For each role:
1. Identify env-specific values in `vars/main.yml` or `defaults/main.yml`
   (IPs, domains, env labels, hostnames)
2. Move them into `inventory/<env>/group_vars/`
3. In `defaults/main.yml`, leave the variable commented out (for discoverability)
   or remove if always inventory-set
4. Add `meta/argument_specs.yml` to validate inputs
5. Smoke-test with `--check` against each env; commit; move on

Budget: 10–30 min per role.

### Phase 5: normalise tag taxonomy

If the codebase uses tags as enablement, transition slowly:
1. Add coarse phase tags (`install`, `configure`) **alongside** existing
   fine-grained tags. Both work; callers can pick.
2. Update CI and runbooks to use the coarse tags.
3. Once no caller references the fine-grained tags, delete them.
4. Reserve `never`-gated tags for destructive operations.

### Phase 6: kill cross-role hostvars coupling

Find every `hostvars['X'].Y` where X isn't in the current play's scope. For
each:
- If Y is static, move to `group_vars` or `host_vars` (inventory data)
- If Y is runtime-discovered, put both hosts in the same play OR use
  `delegate_to:` + `delegate_facts:`

Hardest to migrate; do it last when the rest is clean.

### Phase 7: introduce Molecule per role

Once roles are decoupled and have proper defaults, add Molecule scenarios.
Start with the most-changed roles. CI runs `molecule test` on PRs that touch
a role.

---

## 12. Pre-commit checklist

- [ ] Playbook is a thin role-orchestrator; no large `tasks:` block
- [ ] Playbook does **not** mix `roles:` and `tasks:` in the same play
- [ ] Multi-scope plays are intentional and documented; not accidental drift
- [ ] Roles named after the resource (`nginx`), not the action (`install_nginx`)
- [ ] One role per service, with lifecycle phases as task files + tags
- [ ] `defaults/main.yml` has behavioral defaults; env-specific values are
      commented out for discoverability
- [ ] `vars/main.yml` has only non-overridable constants
- [ ] `meta/argument_specs.yml` declares input contract
- [ ] All role variables prefixed with role name
- [ ] No cross-role `set_fact` coupling
- [ ] Tags follow a single school; `never`-gated for destructive ops
- [ ] FQCN on every module
- [ ] Explicit `state:` on every state-aware module
- [ ] `name:` on every play, task, and block
- [ ] Explicit `mode:` on every file/copy/template
- [ ] Every `command`/`shell` has `creates:`/`changed_when:`/`failed_when:`
- [ ] No `hostvars[X].Y` for `Y` that's a gathered fact when X is out of scope
- [ ] Secrets via lookup or ansible-vault; never plaintext in group_vars
- [ ] Role has a README with a minimal-config usage example
- [ ] yamllint + ansible-lint pass

---

## Sources

1. **Red Hat Communities of Practice — Good Practices for Ansible**.
   https://redhat-cop.github.io/automation-good-practices/
2. **Ansible rolling update example** (canonical pre_tasks/post_tasks).
   https://github.com/ansible/ansible-examples/blob/master/lamp_haproxy/rolling_update.yml
3. **lamp_haproxy site.yml** (multi-scope playbook).
   https://github.com/ansible/ansible-examples/blob/master/lamp_haproxy/site.yml
4. **DebOps site.yml** (`import_playbook` composition pattern).
   https://github.com/debops/debops/blob/master/ansible/playbooks/site.yml
5. **Ansible facts and magic variables** (cross-host facts trap).
   https://docs.ansible.com/ansible/latest/playbook_guide/playbooks_vars_facts.html
6. **Ansible naming conventions guide** (verb-name anti-pattern).
   https://oneuptime.com/blog/post/2026-02-21-how-to-follow-ansible-naming-conventions/view
7. **ansible-lint `role-name` rule**.
   https://docs.ansible.com/projects/lint/rules/role-name/
8. **geerlingguy.mysql** (single-role multi-phase example).
   https://github.com/geerlingguy/ansible-role-mysql
9. **geerlingguy.nginx defaults** (behavioral defaults pattern).
   https://github.com/geerlingguy/ansible-role-nginx/blob/master/defaults/main.yml
10. **Ansible variable precedence**.
    https://docs.ansible.com/ansible/latest/playbook_guide/playbooks_variables.html#understanding-variable-precedence
11. **`validate_argument_spec` module**.
    https://docs.ansible.com/ansible/latest/collections/ansible/builtin/validate_argument_spec_module.html
12. **ansible-lint `var-naming[no-role-prefix]`**.
    https://docs.ansible.com/projects/lint/rules/var-naming/
13. **Role dependencies guide** (prefer explicit include_role over meta deps).
    https://oneuptime.com/blog/post/2026-01-24-ansible-roles-dependencies/view
14. **Molecule + Podman testing** (Red Hat).
    https://www.redhat.com/en/blog/developing-and-testing-ansible-roles-with-molecule-and-podman-part-1
15. **Ansible tags documentation**.
    https://docs.ansible.com/ansible/latest/playbook_guide/playbooks_tags.html
16. **`never` and `always` special tags** (same source as 15).
17. **DebOps DEP-2 code standards** (`role::*` / `skip::*` tag scheme).
    https://docs.debops.org/en/master/dep/dep-0002.html
18. **`include_role` module** (`apply:` keyword semantics).
    https://docs.ansible.com/ansible/latest/collections/ansible/builtin/include_role_module.html
19. **ansible-lint `no-changed-when` rule**.
    https://docs.ansible.com/projects/lint/rules/no-changed-when/
20. **ansible-lint rules index**.
    https://docs.ansible.com/projects/lint/rules/
21. **Ansible inventory plugins**.
    https://docs.ansible.com/ansible/latest/plugins/inventory.html
22. **Collections — overview and migration guidance**.
    https://docs.ansible.com/ansible/latest/dev_guide/developing_collections.html
23. **Polar Squad — Ansible Best Practices Part 2** (loose role coupling).
    https://polarsquad.com/blog/ansible-best-practices-part-2
24. **ansible#46824** (set_fact persistence across roles).
    https://github.com/ansible/ansible/issues/46824
