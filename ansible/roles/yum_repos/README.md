# yum_repos

Declaratively own `/etc/yum.repos.d` on EL-family hosts (RHEL / AlmaLinux /
Rocky). Writes a managed set of repositories — typically internal
Artifactory/Nexus mirrors — and, by default, sweeps away every other `.repo`
file so the directory ends up holding **exactly** the repos you declare, plus a
configurable allowlist.

**Supported majors: EL8, EL9, EL10.** Non-EL hosts skip the role. It self-heals
`min` facts, so it also works under a `gather_facts: false` play or a
tag-isolated `--tags yum_repos` run.

## TL;DR

**Most common: reconcile `/etc/yum.repos.d`.** Declare repos per major (or a flat
list with `$releasever`), optionally set `yum_repos_keep`, then run — the role
picks the list for each host's major, writes those repos, and sweeps the rest.

```bash
ansible-playbook -i inventories/<env>/hosts.yml playbooks/site.yml --tags yum_repos
```

## What it does

1. **resolve** — pick `_yum_repos_effective` for this host's
   `ansible_distribution_major_version` (from `yum_repos_by_major` or the flat
   list); refuse unsupported majors.
2. **assert** — every effective repo has a `name` + `baseurl`; refuses to run if
   the sweep would delete *every* repo (empty effective set **and** empty allowlist).
3. **backup** — one-time snapshot of the pristine directory to
   `/etc/yum.repos.d.orig` (guarded, never clobbered on re-runs).
4. **configure** — writes each effective repo via `ansible.builtin.yum_repository`.
5. **cleanup** — removes `.repo` files that are neither effective nor in
   `yum_repos_keep`.
6. **makecache** (handler) — rebuilds the dnf cache on change; an unreachable
   managed repo fails the run loudly.

## Multi-EL (8 / 9 / 10)

Two ways to cover a mixed fleet. Prefer the map when GPG keys or layouts differ
per major (the usual case).

### 1. Map by major (recommended for mixed fleets)

```yaml
# inventories/<env>/group_vars/all.yml (or a repos group)
yum_repos_by_major:
  "8":
    - name: baseos
      description: "AlmaLinux 8 BaseOS (Artifactory)"
      baseurl: "https://artifactory.example.com/artifactory/almalinux-baseos/$releasever/BaseOS/$basearch/os/"
      gpgcheck: true
      gpgkey: "https://artifactory.example.com/artifactory/almalinux-baseos/RPM-GPG-KEY-AlmaLinux-8"
    - name: appstream
      description: "AlmaLinux 8 AppStream (Artifactory)"
      baseurl: "https://artifactory.example.com/artifactory/almalinux-appstream/$releasever/AppStream/$basearch/os/"
      gpgcheck: true
      gpgkey: "https://artifactory.example.com/artifactory/almalinux-baseos/RPM-GPG-KEY-AlmaLinux-8"
  "9":
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
  "10":
    - name: baseos
      description: "AlmaLinux 10 BaseOS (Artifactory)"
      baseurl: "https://artifactory.example.com/artifactory/almalinux-baseos/$releasever/BaseOS/$basearch/os/"
      gpgcheck: true
      gpgkey: "https://artifactory.example.com/artifactory/almalinux-baseos/RPM-GPG-KEY-AlmaLinux-10"
    - name: appstream
      description: "AlmaLinux 10 AppStream (Artifactory)"
      baseurl: "https://artifactory.example.com/artifactory/almalinux-appstream/$releasever/AppStream/$basearch/os/"
      gpgcheck: true
      gpgkey: "https://artifactory.example.com/artifactory/almalinux-baseos/RPM-GPG-KEY-AlmaLinux-10"
```

Keys **must be strings** (`"8"`, not `8`). When the map is non-empty it is
authoritative — every major you actually run against needs a key.

### 2. Flat list (single layout, or with per-entry `majors`)

```yaml
yum_repos_keep: [epel]

yum_repos_repos:
  - name: baseos
    description: "AlmaLinux BaseOS (Artifactory)"
    baseurl: "https://artifactory.example.com/artifactory/almalinux-baseos/$releasever/BaseOS/$basearch/os/"
    gpgcheck: true
    # expands to AlmaLinux-8 / -9 / -10 from host facts
    gpgkey: "https://artifactory.example.com/artifactory/almalinux-baseos/RPM-GPG-KEY-AlmaLinux-{{ ansible_distribution_major_version }}"
  - name: appstream
    description: "AlmaLinux AppStream (Artifactory)"
    baseurl: "https://artifactory.example.com/artifactory/almalinux-appstream/$releasever/AppStream/$basearch/os/"
    gpgcheck: true
    gpgkey: "https://artifactory.example.com/artifactory/almalinux-baseos/RPM-GPG-KEY-AlmaLinux-{{ ansible_distribution_major_version }}"
  - name: extras
    file: almalinux-extras
    majors: [8, 9]    # only on EL8/EL9
    baseurl: "https://artifactory.example.com/artifactory/almalinux-extras/$releasever/extras/$basearch/os/"
    gpgcheck: false
```

`$releasever` / `$basearch` are expanded by dnf on the host. The flat list is
used only when `yum_repos_by_major` is empty.

## Key variables

| Variable | Default | Purpose |
|---|---|---|
| `yum_repos_by_major` | `{}` | Map of major → repo list (`"8"` / `"9"` / `"10"`). Preferred for mixed fleets. |
| `yum_repos_repos` | `[]` | Flat repo list when the map is empty; optional per-entry `majors`. |
| `yum_repos_supported_majors` | `["8", "9", "10"]` | Majors the role will manage. |
| `yum_repos_remove_unmanaged` | `true` | `true` = replace all; `false` = additive. |
| `yum_repos_keep` | `[]` | Basenames (no `.repo`) to preserve, e.g. `[epel]`. |
| `yum_repos_backup` | `true` | Snapshot originals before the first sweep. |
| `yum_repos_makecache` | `true` | Refresh + reachability-check after changes. |
| `yum_repos_enabled` | `true` | Master off switch. |

Per-repo keys: `name`, `baseurl` (required); `file`, `description`, `enabled`,
`gpgcheck`, `gpgkey`, `sslverify`, `priority`, `module_hotfixes`,
`metadata_expire`, `username`, `password`, `majors`, `state` (optional).

## Playbook

```yaml
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

- **Anonymous vs authenticated repos.** For authenticated Artifactory repos add
  `username` / `password` on the entry — source the password from Vault in
  `group_vars`, never commit it.
- **GPG keys differ per major** (`RPM-GPG-KEY-AlmaLinux-8` vs `-9` vs `-10`). Use
  the map or `{{ ansible_distribution_major_version }}` in the flat `gpgkey`.
- The sweep errs toward *keeping*: a stray file whose basename matches an
  effective managed repo id is preserved rather than deleted.
- Restrict the fleet with `yum_repos_supported_majors` if you only want e.g.
  `["8", "9"]` and want EL10 hosts to fail loudly instead of half-applying.
