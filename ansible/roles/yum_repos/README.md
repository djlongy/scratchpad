# yum_repos

Declaratively owns `/etc/yum.repos.d` on EL-family hosts (RHEL / AlmaLinux /
Rocky). Writes a managed set of repositories — typically internal
Artifactory/Nexus mirrors — and, by default, sweeps away every other `.repo`
file so the directory ends up holding **exactly** the repos you declare, plus a
configurable allowlist.

## TL;DR

Declare `yum_repos_repos` in group_vars (add `yum_repos_keep` for repos to
preserve), then run — the role writes the managed repos and sweeps every other
`.repo` file. Non-EL hosts skip the role.

```bash
ansible-playbook -i inventories/<env>/hosts.yml playbooks/vault_solo_e2e.yml --tags yum_repos
```

## Requirements

None beyond `ansible.builtin`.

## Key variables

Full list: `defaults/main.yml`. Contract: `meta/argument_specs.yml`.

**Required** = value must be correct for a successful run (defaults often work).
**Optional** = safe to leave default / empty; phase stays off or uses built-ins.
**When X** = required only if that feature is on.

| Req | Variable | Default | Purpose |
|---|---|---|---|
| **Required** | `yum_repos_repos` | `[]` | Managed repositories — each needs `name` + `baseurl`. Assert fails if this AND `yum_repos_keep` are both empty while the sweep is on |
| Optional | `yum_repos_remove_unmanaged` | `true` | `true` = replace all (sweep); `false` = additive |
| Optional | `yum_repos_keep` | `[]` | Basenames (no `.repo` suffix) to preserve during the sweep, e.g. `[epel]` |
| Optional | `yum_repos_backup` | `true` | One-time snapshot of the pristine directory before the first sweep |
| Optional | `yum_repos_makecache` | `true` | `dnf makecache` after changes — an unreachable managed repo fails the run loudly |
| Optional | `yum_repos_enabled` | `true` | Master off switch |
| Optional | `yum_repos_dir` | `/etc/yum.repos.d` | Directory this role owns |
| Optional | `yum_repos_backup_dir` | `/etc/yum.repos.d.orig` | Where the one-time snapshot is written |

Per-repo keys: `name`, `baseurl` (required); `file`, `description`, `enabled`,
`gpgcheck`, `gpgkey`, `sslverify`, `priority`, `module_hotfixes`,
`metadata_expire`, `state` (optional).

## Minimum configuration

```yaml
# group_vars/yum_repos_hosts.yml
---
# Required
yum_repos_repos: "REPLACE_ME_yum_repos_repos"
```

## Usage

```yaml
- hosts: el_hosts
  become: true
  roles:
    - yum_repos
```

Run it:

```bash
ansible-playbook -i inventories/<env>/hosts.yml playbooks/<playbook>.yml --tags yum_repos
```

## Behaviour

1. **assert** — every repo has a `name` + `baseurl`; refuses to run if the
   sweep would delete *every* repo (empty managed set **and** empty
   allowlist).
2. **backup** — one-time snapshot of the pristine directory to
   `yum_repos_backup_dir` (guarded, never clobbered on re-runs).
3. **configure** — writes each managed repo via `ansible.builtin.yum_repository`.
4. **cleanup** — removes `.repo` files that are neither managed nor in
   `yum_repos_keep`. The sweep errs toward *keeping*: a stray file whose
   basename matches a managed repo id is preserved rather than deleted.
5. **makecache** (handler) — rebuilds the dnf cache on change; an unreachable
   managed repo fails the run loudly.

Self-heals `min` facts, so it also works under a `gather_facts: false` play or
a tag-isolated `--tags yum_repos` run.

For authenticated repos (e.g. Artifactory), add `username`/`password` to the
entry (both are `yum_repository` params) — source the password from Vault in
`group_vars`, never commit it.

Rollback: the pristine directory is preserved once at `yum_repos_backup_dir`:

```bash
cp -a /etc/yum.repos.d.orig/. /etc/yum.repos.d/
```
