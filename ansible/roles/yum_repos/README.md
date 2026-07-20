# yum_repos

Declaratively own `/etc/yum.repos.d` on EL-family hosts (RHEL / AlmaLinux /
Rocky). Writes a managed set of repositories — typically internal
Artifactory/Nexus mirrors — and, by default, sweeps away every other `.repo`
file so the directory ends up holding **exactly** the repos you declare, plus a
configurable allowlist.

Non-EL hosts skip the role. It self-heals `min` facts, so it also works under a
`gather_facts: false` play or a tag-isolated `--tags yum_repos` run.

## TL;DR

**Most common: reconcile `/etc/yum.repos.d`.** Declare `yum_repos_repos` in group_vars (add `yum_repos_keep` for repos to preserve), then run — the role writes the managed repos and sweeps every other `.repo` file.

```bash
ansible-playbook -i inventories/<env>/hosts.yml playbooks/site.yml --tags yum_repos
```

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
# inventories/mgt/group_vars/alma.yml  (live shape — force all dnf through Nexus)
yum_repos_repos:
  - name: baseos
    file: almalinux-nexus
    description: "AlmaLinux $releasever - BaseOS (via Nexus)"
    baseurl: "https://nexus.{{ env }}.{{ domain }}/repository/yum-almalinux-proxy/$releasever/BaseOS/$basearch/os/"
    gpgcheck: true
    gpgkey: "https://nexus.{{ env }}.{{ domain }}/repository/yum-almalinux-proxy/RPM-GPG-KEY-AlmaLinux-9"
  - name: appstream
    file: almalinux-nexus
    description: "AlmaLinux $releasever - AppStream (via Nexus)"
    baseurl: "https://nexus.{{ env }}.{{ domain }}/repository/yum-almalinux-proxy/$releasever/AppStream/$basearch/os/"
    gpgcheck: true
    gpgkey: "https://nexus.{{ env }}.{{ domain }}/repository/yum-almalinux-proxy/RPM-GPG-KEY-AlmaLinux-9"
  - name: epel
    file: epel-nexus
    description: "EPEL $releasever (via Nexus)"
    baseurl: "https://nexus.{{ env }}.{{ domain }}/repository/yum-epel-proxy/$releasever/Everything/$basearch/"
    gpgcheck: true
    gpgkey: "https://nexus.{{ env }}.{{ domain }}/repository/yum-epel-proxy/RPM-GPG-KEY-EPEL-$releasever"
```

Nexus must already have the yum proxies (`yum-almalinux-proxy`, `yum-epel-proxy`) —
provisioned by the `nexus` role (`app_supply_chain.yml --tags repos`).

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
