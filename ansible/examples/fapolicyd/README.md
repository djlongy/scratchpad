# fapolicyd: Troubleshooting and Whitelisting Applications

When fapolicyd is active (common on STIG/CIS-hardened RHEL/Oracle Linux systems), applications not in the trust database are denied execution with `Operation not permitted`. This guide walks through diagnosing and resolving the issue for **any** application.

Tested on Oracle Linux 9.7 with fapolicyd 1.3.x. Applies to RHEL 9, AlmaLinux 9, Rocky Linux 9, and other EL9 derivatives.

---

## Table of Contents

- [Symptoms](#symptoms)
- [Step 1: Confirm fapolicyd is Blocking the Application](#step-1-confirm-fapolicyd-is-blocking-the-application)
- [Step 2: Identify the Blocked Binary](#step-2-identify-the-blocked-binary)
- [Step 3: Check the Trust Database](#step-3-check-the-trust-database)
- [Step 4: Determine Why the Binary is Untrusted](#step-4-determine-why-the-binary-is-untrusted)
- [Step 5: Try Refreshing the Trust Database](#step-5-try-refreshing-the-trust-database)
- [Step 6: Choose a Whitelisting Method](#step-6-choose-a-whitelisting-method)
  - [Method A: Trust Database (trust.d/)](#method-a-trust-database-trustd) -- hash-verified, per-file
  - [Method B: Allow Rules (rules.d/)](#method-b-allow-rules-rulesd) -- directory-based, no hashes
- [Step 7: Validate](#step-7-validate)
- [Advanced Debugging](#advanced-debugging)
- [Ansible Automation](#ansible-automation)
- [Kickstart Integration](#kickstart-integration)
- [Common Applications Reference](#common-applications-reference)
- [Diagnostic Quick Reference](#diagnostic-quick-reference)

---

## Symptoms

```
/usr/bin/myapp: line 62: /usr/share/myapp/bin/../myapp: Operation not permitted
```

The application binary or shared libraries are blocked. The mouse/GUI may work, but the app never opens. No crash, no segfault -- just `Operation not permitted`.

---

## Step 1: Confirm fapolicyd is Blocking and See What's Denied

Run the application from a terminal. If you see `Operation not permitted`, fapolicyd is the likely cause:

```bash
/usr/bin/myapp --verbose
```

Verify fapolicyd is running:

```bash
systemctl status fapolicyd
```

### Run fapolicyd in debug-deny mode

This is the single most useful diagnostic. Stop the service, run in foreground with `--debug-deny`, and trigger the app in another terminal:

```bash
# Terminal 1: stop the service and run in debug-deny mode
sudo systemctl stop fapolicyd
sudo fapolicyd --debug-deny
```

```bash
# Terminal 2: trigger the blocked application
/usr/bin/myapp
```

Terminal 1 shows each denied access with the rule number, binary, path, and trust status:

```
rule=13 dec=deny_audit perm=execute auid=1000 pid=4521 exe=/usr/bin/bash : path=/usr/share/myapp/myapp ftype=application/x-executable trust=0
```

This tells you everything you need:
- **rule=13** -- which rule triggered the deny (cross-reference with `fapolicyd-cli --list`)
- **exe=** -- which binary tried to access the file
- **path=** -- which file was blocked
- **trust=0** -- the file is not in the trust database

Press `Ctrl+C` to stop, then restart the service:

```bash
sudo systemctl start fapolicyd
```

With this output, you can skip straight to [Step 6](#step-6-choose-a-whitelisting-method) -- you already know what binary is blocked and which directory needs whitelisting. Steps 2-5 below are for when the debug output isn't clear or you need to understand *why* the binary is untrusted.

---

## Step 2: Identify the Blocked Binary

Wrapper scripts often call binaries elsewhere. Find the actual ELF binary being blocked:

```bash
# Check if the command is a script wrapping another binary
file $(which myapp)

# If it's a shell script, find the real binary it calls
cat $(which myapp) | grep -E 'exec|\./'

# Find all ELF binaries and shared libraries in the install directory
find /usr/share/myapp/ -type f \( -executable -o -name '*.so*' \) | head -20
```

---

## Step 3: Check the Trust Database

```bash
# Search the trust DB for the application's install directory
sudo fapolicyd-cli --dump-db | grep '/usr/share/myapp/'

# Check specifically for the main binary
sudo fapolicyd-cli --dump-db | grep '/usr/share/myapp/myapp$'
```

If the binary is **not listed**, fapolicyd does not trust it.

Also check whether an allow rule already exists:

```bash
# List all active rules and look for the app's directory
sudo fapolicyd-cli --list | grep -i myapp
sudo grep -r myapp /etc/fapolicyd/rules.d/
```

---

## Step 4: Determine Why the Binary is Untrusted

### 4a. Is the application RPM-installed?

```bash
rpm -qf /usr/share/myapp/myapp
```

- **If owned by an RPM**: fapolicyd should trust it via the `rpmdb` backend. The trust DB may be stale (see Step 5).
- **If not owned by an RPM** (manual install, tarball, pip, npm): you must whitelist it manually (see Step 6).

### 4b. Was the application installed after fapolicyd was started?

This is the most common cause. If the package was installed:
- In a kickstart `%post` section after fapolicyd's initial DB was built
- Via `dnf install` while fapolicyd was running but `rpm-plugin-fapolicyd` didn't fire
- From a third-party repo that doesn't trigger the RPM plugin correctly

The trust DB snapshot will be stale and missing the new files.

### 4c. Has the binary been modified since installation?

```bash
rpm -V $(rpm -qf /usr/share/myapp/myapp)
```

If the output shows size/hash changes (e.g., `S.5`), the on-disk binary doesn't match what RPM expects. Reinstall the package or re-add to trust.

---

## Step 5: Try Refreshing the Trust Database

If the application is RPM-installed, a DB refresh may pick it up without any manual whitelisting:

```bash
# Notify fapolicyd to re-read trust sources
sudo fapolicyd-cli --update

# If that doesn't work, restart the service for a full rebuild
sudo systemctl restart fapolicyd
```

Verify:

```bash
sudo fapolicyd-cli --dump-db | grep '/usr/share/myapp/myapp'
```

If the binary now appears, test the application. If it still doesn't appear, proceed to Step 6.

---

## Step 6: Choose a Whitelisting Method

There are two approaches. Choose based on your needs:

| | Method A: Trust Database (`trust.d/`) | Method B: Allow Rules (`rules.d/`) |
|---|---|---|
| **Security** | Each file hashed with SHA256 | Directory-level allow, no hashes |
| **Granularity** | Per-file | Per-directory, optionally scoped to calling binary |
| **Maintenance** | Must regenerate hashes on app update | No changes needed on app update |
| **Best for** | High-security environments, FIPS | Simpler management, Ansible automation |
| **Non-RPM apps** | Works for any file | Works for any file |

### Method A: Trust Database (trust.d/)

Adds files by path + size + SHA256 hash. Strongest integrity guarantee.

#### Add an entire directory (recommended)

```bash
# Add all files under the application's install directory
# --trust-file creates a named drop-in in /etc/fapolicyd/trust.d/
sudo fapolicyd-cli --file add /usr/share/myapp/ --trust-file myapp

# Notify fapolicyd to reload
sudo fapolicyd-cli --update
```

This recursively adds every file with its size and SHA256 hash to `/etc/fapolicyd/trust.d/myapp`.

#### Add individual files (when the app spans multiple locations)

```bash
sudo fapolicyd-cli --file add /usr/bin/myapp --trust-file myapp
sudo fapolicyd-cli --file add /usr/lib64/myapp/plugin.so --trust-file myapp
sudo fapolicyd-cli --update
```

#### Trust file format

Files in `/etc/fapolicyd/trust.d/` follow this format:

```
/full/path/to/binary SIZE SHA256HASH
```

Hashes are computed automatically by `fapolicyd-cli --file add`. To compute manually:

```bash
sha256sum /usr/share/myapp/myapp | awk '{print $1}'
stat -c %s /usr/share/myapp/myapp
```

#### Verify

```bash
ls -la /etc/fapolicyd/trust.d/
cat /etc/fapolicyd/trust.d/myapp | head -10
sudo fapolicyd-cli --dump-db | grep '/usr/share/myapp/myapp'
/usr/bin/myapp --version
```

---

### Method B: Allow Rules (rules.d/)

Adds a policy rule that allows execution of files within a directory. No per-file hashing -- simpler to maintain but less granular. This is the approach commonly used in production hardened environments.

#### How fapolicyd rules work

Rules are evaluated **in order** from the `rules.d/` directory. Files are numbered (e.g., `31-myapp.rules`) and processed lowest-to-highest. The default deny rule is typically `90-deny-execute.rules`. Your allow rules must be numbered **lower** (e.g., `31-*.rules`) so they are evaluated first.

```
10-languages.rules        # Language definitions
20-dracut.rules           # System tools
21-updaters.rules         # Package managers
30-patterns.rules         # Known patterns
31-myapp.rules            # <-- Your custom app rules go here
40-bad-elf.rules          # Block bad ELFs
41-shared-obj.rules       # Shared object rules
42-trusted-elf.rules      # Allow trusted (rpmdb/file) ELFs
70-trusted-lang.rules     # Allow trusted scripts
72-shell.rules            # Shell rules
90-deny-execute.rules     # Default deny -- everything above must allow first
95-allow-open.rules       # Allow open (read) for all
```

#### Create a rule file

The simplest form -- allow any binary to execute files in the app's directory:

```bash
cat <<'EOF' | sudo tee /etc/fapolicyd/rules.d/31-myapp.rules
allow perm=any all : dir=/usr/share/myapp/
EOF
```

A more restrictive form -- only allow a specific binary to access the directory:

```bash
cat <<'EOF' | sudo tee /etc/fapolicyd/rules.d/31-myapp.rules
allow perm=any exe=/usr/share/myapp/myapp : dir=/usr/share/myapp/
EOF
```

For Java/Python applications, scope to the interpreter:

```bash
# Java application (resolve the symlink first)
JAVA_BIN=$(readlink -f /etc/alternatives/java)
cat <<EOF | sudo tee /etc/fapolicyd/rules.d/31-myapp.rules
allow perm=any exe=${JAVA_BIN} : dir=/opt/myapp/
allow perm=any exe=${JAVA_BIN} : dir=/tmp
EOF

# Python application
cat <<EOF | sudo tee /etc/fapolicyd/rules.d/31-myapp.rules
allow perm=any exe=/usr/bin/python3 : dir=/opt/myapp/
allow perm=any exe=/usr/bin/python3 : dir=/opt/myapp/venv/
EOF
```

#### Set permissions and reload

```bash
sudo chown root:fapolicyd /etc/fapolicyd/rules.d/31-myapp.rules
sudo chmod 0644 /etc/fapolicyd/rules.d/31-myapp.rules

# Compile and load the rules
sudo fagenrules --load
```

#### Verify

```bash
# Confirm the rule is active
sudo fapolicyd-cli --list | grep myapp

# Test the application
/usr/bin/myapp --version
```

#### Rule syntax quick reference

```
allow perm=any all : dir=/path/to/app/           # Any binary can access this directory
allow perm=any exe=/usr/bin/app : dir=/opt/app/   # Only /usr/bin/app can access this directory
allow perm=execute all : dir=/opt/app/            # Allow execute only (not open/read)
allow perm=any exe=/usr/bin/java : dir=/opt/app/  # Java app scoped to interpreter
allow perm=any exe=/usr/bin/python3 : dir=/opt/app/  # Python app scoped to interpreter
```

---

## Step 7: Validate

After whitelisting with either method:

```bash
# Test the application runs
/usr/bin/myapp --version

# Run fapolicyd health checks
sudo fapolicyd-cli --check-trustdb
sudo fapolicyd-cli --check-path
```

---

## Advanced Debugging

Step 1 covers `--debug-deny` which solves most cases. These options are for deeper investigation.

### Full debug (all decisions, not just denials)

Traces every file access -- allow and deny. Generates a lot of output. Useful when you need to see the full chain of file accesses an application makes on startup:

```bash
sudo systemctl stop fapolicyd
sudo fapolicyd --debug 2>&1 | tee /tmp/fapolicyd-debug.log
```

Then in another terminal, run the app. Search the log for denials:

```bash
grep 'dec=deny' /tmp/fapolicyd-debug.log
```

### Permissive mode (log but don't block)

Allows all access but logs what *would* be denied. Useful for building rules for a new application without breaking it during testing:

```bash
# Enable permissive mode
sudo sed -i 's/^permissive = 0/permissive = 1/' /etc/fapolicyd/fapolicyd.conf
sudo systemctl restart fapolicyd

# Run the application normally, then check what would have been denied
sudo journalctl -u fapolicyd --no-pager | grep deny
```

> **Revert when done** -- permissive mode disables all enforcement:
> ```bash
> sudo sed -i 's/^permissive = 1/permissive = 0/' /etc/fapolicyd/fapolicyd.conf
> sudo systemctl restart fapolicyd
> ```

### Check internal stats (non-disruptive)

View denied access counts and trust DB usage without stopping the service:

```bash
sudo fapolicyd-cli --check-status
```

A non-zero `Denied accesses` count confirms fapolicyd is actively blocking something.

### Matching rule numbers to rules

Debug output shows `rule=N`. To find the corresponding rule:

```bash
fapolicyd-cli --list | grep "^13\."
```

### Journal queries

```bash
sudo journalctl -u fapolicyd --no-pager --since "10 minutes ago" | grep deny
```

---

## Ansible Automation

A reusable common task and Jinja2 templates are provided in this repo:

- [`roles/common/tasks/fapolicyd.yml`](../../roles/common/tasks/fapolicyd.yml) -- reusable include task (3 tasks, no tags, no silent skips)
- [`templates/31-app.rules.j2`](templates/31-app.rules.j2) -- generic data-driven rule template
- [`templates/31-java-app.rules.j2`](templates/31-java-app.rules.j2) -- Java app template with symlink resolution

### Design decisions

The common task is deliberately minimal:

| Decision | Why |
|---|---|
| **Fails if fapolicyd is missing** | If a role deploys a rule, fapolicyd should be installed. Silent skips hide broken playbooks. |
| **Template only, no copy** | `ansible.builtin.template` renders static content identically to `copy`. One code path, not two. |
| **No hardcoded tags** | `include_tasks` inherits the caller's tags automatically. Baking in tags couples the shared task to one workflow. |
| **Underscore-prefixed registers** | `_fapolicyd_deployed` instead of `fapolicyd_rules` avoids namespace collisions when multiple roles include this. |
| **Service check only on change** | Only queries systemd when the rule file actually changed. No wasted round trips on idempotent runs. |

### Using rules.d/ (recommended for simplicity)

#### Calling the common task from your role

```yaml
# roles/myapp/tasks/main.yml
- name: Deploy fapolicyd rules for myapp
  ansible.builtin.include_tasks: roles/common/tasks/fapolicyd.yml
  vars:
    fapolicyd_rule_template: templates/31-myapp.rules.j2
    fapolicyd_rule_name: 31-myapp.rules
```

#### For Java applications, resolve the symlink first

```yaml
- name: Resolve java symlink
  ansible.builtin.stat:
    path: /etc/alternatives/java
  register: java_path

- name: Deploy fapolicyd rules for nifi
  ansible.builtin.include_tasks: roles/common/tasks/fapolicyd.yml
  vars:
    fapolicyd_rule_template: templates/31-java-app.rules.j2
    fapolicyd_rule_name: 31-nifi.rules
    app_install_dir: "/opt/nifi/nifi-{{ nifi_vers }}"
```

#### Data-driven approach for multiple applications

```yaml
# group_vars/all.yml or role defaults
fapolicyd_app_rules:
  - name: vscode
    rule_file: 31-vscode.rules
    entries:
      - { exe: "all", dir: "/usr/share/code/" }
  - name: chrome
    rule_file: 31-chrome.rules
    entries:
      - { exe: "all", dir: "/opt/google/chrome/" }
  - name: nifi
    rule_file: 31-nifi.rules
    entries:
      - { exe: "{{ java_bin }}", dir: "/opt/nifi/nifi-{{ nifi_vers }}/" }
      - { exe: "{{ java_bin }}", dir: "/tmp" }
```

```yaml
# tasks/fapolicyd_rules.yml
- name: Deploy fapolicyd rules for {{ item.name }}
  ansible.builtin.include_tasks: roles/common/tasks/fapolicyd.yml
  vars:
    fapolicyd_rule_template: fapolicyd-app-rules.j2
    fapolicyd_rule_name: "{{ item.rule_file }}"
  loop: "{{ fapolicyd_app_rules }}"
```

### Using trust.d/ (when hash verification is required)

```yaml
- name: Trust application directory in fapolicyd
  ansible.builtin.command:
    cmd: "fapolicyd-cli --file add {{ item.path }} --trust-file {{ item.name }}"
  loop:
    - { name: "myapp", path: "/usr/share/myapp/" }
  notify: update fapolicyd
  tags: [add_fapolicyd]

- name: update fapolicyd
  ansible.builtin.command:
    cmd: fapolicyd-cli --update
  listen: update fapolicyd
```

> **Note:** The trust.d approach requires regenerating hashes after every application update.
> The rules.d approach does not -- the directory-level allow persists across updates.

---

## Kickstart Integration

### Using rules.d/ (simpler, survives updates)

```bash
%post --erroronfail --log=/root/ks-post.log

# Install the application
dnf install -y myapp

# Create fapolicyd allow rule for the app directory
cat > /etc/fapolicyd/rules.d/31-myapp.rules <<FAPEOF
allow perm=any all : dir=/usr/share/myapp/
FAPEOF
chown root:fapolicyd /etc/fapolicyd/rules.d/31-myapp.rules
chmod 0644 /etc/fapolicyd/rules.d/31-myapp.rules

%end
```

### Using trust.d/ (hash-verified)

```bash
%post --erroronfail --log=/root/ks-post.log

# Install the application
dnf install -y myapp

# Trust the entire install directory
fapolicyd-cli --file add /usr/share/myapp/ --trust-file myapp
fapolicyd-cli --update

%end
```

A full example kickstart with GNOME, fapolicyd, STIG hardening, and FIPS is available at [`examples/kickstart/ks-el9-hardened.cfg`](../kickstart/ks-el9-hardened.cfg).

---

## Common Applications Reference

| Application | RPM Package | Install Directory | Recommended Rule |
|---|---|---|---|
| VS Code | `code` | `/usr/share/code/` | `allow perm=any all : dir=/usr/share/code/` |
| Google Chrome | `google-chrome-stable` | `/opt/google/chrome/` | `allow perm=any all : dir=/opt/google/chrome/` |
| Slack | `slack` | `/usr/lib/slack/` | `allow perm=any all : dir=/usr/lib/slack/` |
| Zoom | `zoom` | `/opt/zoom/` | `allow perm=any all : dir=/opt/zoom/` |
| Teams | `teams-for-linux` | `/opt/teams-for-linux/` | `allow perm=any all : dir=/opt/teams-for-linux/` |
| JetBrains IDEs | manual | `/opt/jetbrains/` | `allow perm=any all : dir=/opt/jetbrains/` |
| Firefox | `firefox` | `/usr/lib64/firefox/` | Usually RPM-trusted; rule if needed |
| NiFi | manual | `/opt/nifi/` | `allow perm=any exe=<java> : dir=/opt/nifi/` |
| Zookeeper | manual | `/opt/zookeeper/` | `allow perm=any exe=<java> : dir=/opt/zookeeper/` |
| Python venvs | pip | `/opt/myapp/venv/` | `allow perm=any exe=/usr/bin/python3 : dir=/opt/myapp/venv/` |

---

## Diagnostic Quick Reference

```bash
# Is fapolicyd running?
systemctl status fapolicyd

# Show active fapolicyd rules (numbered, in evaluation order)
fapolicyd-cli --list

# Search trust DB for a path
sudo fapolicyd-cli --dump-db | grep '/path/to/app'

# Check what RPM owns a file
rpm -qf /path/to/binary

# Verify RPM file integrity
rpm -V package-name

# --- Method A: trust.d/ ---

# Add directory to trust (creates named drop-in file)
sudo fapolicyd-cli --file add /install/path/ --trust-file appname
sudo fapolicyd-cli --update

# Remove an application's trust entries
sudo fapolicyd-cli --file delete /install/path/ --trust-file appname
sudo fapolicyd-cli --update

# Delete a trust.d file entirely
sudo rm /etc/fapolicyd/trust.d/appname
sudo fapolicyd-cli --update

# --- Method B: rules.d/ ---

# Create a rule file (numbered 31-* to run before deny at 90-*)
sudo tee /etc/fapolicyd/rules.d/31-appname.rules <<< 'allow perm=any all : dir=/path/'
sudo chown root:fapolicyd /etc/fapolicyd/rules.d/31-appname.rules
sudo chmod 0644 /etc/fapolicyd/rules.d/31-appname.rules
sudo fagenrules --load

# Remove a rule
sudo rm /etc/fapolicyd/rules.d/31-appname.rules
sudo fagenrules --load

# --- General ---

# Health checks
sudo fapolicyd-cli --check-trustdb
sudo fapolicyd-cli --check-path

# Full trust DB rebuild (nuclear option)
sudo fapolicyd-cli --delete-db
sudo systemctl restart fapolicyd
```
