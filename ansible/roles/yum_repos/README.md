# yum_repos

Declaratively own `/etc/yum.repos.d` on EL-family hosts (RHEL / AlmaLinux /
Rocky). Writes a managed set of repositories — typically internal
Artifactory/Nexus mirrors — and, by default, sweeps away every other `.repo`
file so the directory ends up holding **exactly** the repos you declare, plus a
configurable allowlist.

Non-EL hosts skip the role. It self-heals `min` facts, so it also works under a
`gather_facts: false` play or a tag-isolated `--tags yum_repos` run.

## What it does

1. **assert** — every repo has a `name` + `baseurl`; refuses to run if the sweep
   would delete *every* repo (empty managed set **and** empty allowlist).
2. **backup** — one-time snapshot of the pristine directory to
   `/etc/yum.repos.d.orig` (guarded, never clobbered on re-runs).
3. **configure** — writes each managed repo via `ansible.builtin.yum_repository`.
4. **cleanup** — removes `.repo` files that are neither managed nor in
   `yum_repos_keep`.
5. **makecache** (handler) — rebuilds the dnf cache on change; an unreachable
   managed repo fails the run loudly.

## Key variables

| Variable | Default | Purpose |
|---|---|---|
| `yum_repos_repos` | `[]` | Managed repositories (see schema below). |
| `yum_repos_remove_unmanaged` | `true` | `true` = replace all; `false` = additive. |
| `yum_repos_keep` | `[]` | Basenames (no `.repo`) to preserve, e.g. `[epel]`. |
| `yum_repos_backup` | `true` | Snapshot originals before the first sweep. |
| `yum_repos_makecache` | `true` | Refresh + reachability-check after changes. |
| `yum_repos_enabled` | `true` | Master off switch. |

Per-repo keys: `name`, `baseurl` (required); `file`, `description`, `enabled`,
`gpgcheck`, `gpgkey`, `sslverify`, `priority`, `module_hotfixes`,
`metadata_expire`, `state` (optional).

## Example

Put env-specific URLs in inventory `group_vars`, not the role:

```yaml
# inventories/<env>/group_vars/some_group.yml
yum_repos_keep: [epel]          # keep EPEL, replace everything else

yum_repos_repos:
  - name: baseos
    description: "AlmaLinux 9 BaseOS (Artifactory)"
    baseurl: "https://artifactory.example.com/artifactory/almalinux-baseos/$releasever/BaseOS/$basearch/os/"
    gpgcheck: true
    gpgkey: "https://artifactory.example.com/artifactory/almalinux-baseos/RPM-GPG-KEY-AlmaLinux-9"
  - name: appstream
    description: "AlmaLinux 9 AppStream (Artifactory)"
    baseurl: "https://artifactory.example.com/artifactory/almalinux-appstream/$releasever/AppStream/$basearch/os/"
    gpgcheck: true
    gpgkey: "https://artifactory.example.com/artifactory/almalinux-baseos/RPM-GPG-KEY-AlmaLinux-9"
```

```yaml
# playbook
- hosts: el_hosts
  become: true
  roles:
    - yum_repos
```

## Rollback

The pristine directory is preserved once at `/etc/yum.repos.d.orig`:

```bash
cp -a /etc/yum.repos.d.orig/. /etc/yum.repos.d/
```

## Notes

- **Anonymous repos.** For authenticated Artifactory repos add `username` /
  `password` to the entry (both are `yum_repository` params) — source the
  password from Vault in `group_vars`, never commit it.
- The sweep errs toward *keeping*: a stray file whose basename matches a managed
  repo id is preserved rather than deleted.
