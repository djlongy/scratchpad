# audit_logging

Portable, backend-agnostic **audit logging for `ansible-playbook` runs**. Captures
who ran a playbook, from where, against which hosts and roles, with which CLI
arguments and git state — then ships **one structured JSON record** to any
combination of backends.

Self-contained: the run-metadata collector (`get_cli_args`) is bundled as a
role-local action plugin, so the role works dropped into any repo with no extra
plugin path wiring. Every option is exposed in `defaults/main.yml`; nothing is
hardcoded.

## Backends

| `audit_logging_backends` entry | Ships to | Transport |
|---|---|---|
| `file` | Control-node JSON Lines file | `lineinfile` |
| `syslog` | Local syslog | `logger(1)` |
| `rsyslog` | Remote rsyslog | UDP via `nc` |
| `fluentd` | Fluentd HTTP input | `uri` POST |
| `elasticsearch` | Elasticsearch index | `uri` POST `_doc` |
| `splunk` | Splunk HTTP Event Collector | `uri` POST `/services/collector/event` |
| `cloudwatch` | AWS CloudWatch Logs | AWS CLI |

All shipping runs **exactly once on the control node** — the record describes the
*run*, not each target host. The body is delegated to `localhost` and gated to the
first host of the whole play (`ansible_play_hosts_all | first`), so it ships once
under the **linear, `free`, and `serial:` (any batch size)** strategies alike.
(`run_once` is deliberately **not** used — it fires once *per serial batch* and is
unreliable under `free`, which over-ships.) Every backend is best-effort
(`ignore_errors`) so audit logging never fails the play.

## Mode

| `audit_logging_mode` | Behaviour |
|---|---|
| `ship` (default) | Buffer this play's metadata, then ship one consolidated record |
| `accumulate` | Buffer only — do not ship |

Every inclusion always buffers first (idempotent per play name). Intermediate
multi-play plays use `accumulate`; the final inclusion uses `ship` with backends.

`allow_duplicates: true` is set so the same play can list the role more than once
(e.g. mid-list accumulate + final ship).

## Homelab environment wiring

This repo enables Splunk HEC by default for every playbook that lists the role:

| Var (in `playbooks/group_vars/all/audit_logging.yml`) | Value |
|---|---|
| `audit_logging_backends` | `[splunk]` |
| `audit_logging_splunk_hec_url` | `http://splunk.mgt.example.com:8088` |
| `audit_logging_splunk_hec_token` | Vault `kv-ops/apps/splunk/runtime:hec_token` |
| `audit_logging_splunk_validate_certs` | `false` (HTTP HEC) |
| `audit_logging_splunk_index` | `main` |
| `audit_logging_splunk_sourcetype` | `ansible:audit` |

Playbooks accumulate per intermediate play and ship once on the final play.
HEC path is `{{ hec_url }}/services/collector/event`.

## Usage

### Single-play — inline under `roles:` (preferred)

```yaml
- hosts: webservers
  roles:
    - role: nginx
    - role: audit_logging
      vars:
        audit_logging_backends: [splunk, file]
        audit_logging_splunk_hec_url: "https://splunk.example.com:8088"
        audit_logging_splunk_hec_token: "{{ vault_splunk_hec_token }}"
```

### Multi-play — inline under `roles:` (preferred)

```yaml
- hosts: db
  roles:
    - postgresql
    - role: audit_logging
      vars:
        audit_logging_mode: accumulate

- hosts: web
  roles:
    - nginx
    - role: audit_logging
      vars:
        audit_logging_backends: [splunk]
```

### Same play — mid-list accumulate + final ship

```yaml
- hosts: idm
  roles:
    - role: vsphere_vm
      vars:
        vsphere_vm_wait_for_ssh: true
    - role: storage
      vars:
        storage_provision: true
    - role: freeipa_server
      tags: [freeipa]
    - role: audit_logging
      vars:
        audit_logging_mode: accumulate
    - role: another_role
    - role: audit_logging
      vars:
        audit_logging_backends: [splunk]
```

Mid-list accumulate is optional in a single play (role names for the whole play
are already known when ship runs). It is harmless — the buffer is idempotent —
and useful if you want an explicit checkpoint style.

### Multi-play — legacy `post_tasks` + `tasks_from` (still supported)

```yaml
- hosts: db
  roles: [postgresql]
  post_tasks:
    - ansible.builtin.include_role: { name: audit_logging, tasks_from: accumulate }

- hosts: web
  roles: [nginx]
  post_tasks:
    - ansible.builtin.include_role: { name: audit_logging, tasks_from: accumulate }

- hosts: localhost
  post_tasks:
    - name: Write consolidated audit log
      ansible.builtin.include_role: { name: audit_logging }
      vars:
        audit_logging_backends: [splunk]
```

Prefer the inline `roles:` form above; this path remains for existing playbooks.

### Recording failed runs

Wrap so a failure still emits an audited record:

```yaml
  post_tasks:
    - block:
        - ansible.builtin.include_role: { name: audit_logging }
          vars: { audit_logging_backends: [splunk] }
      rescue:
        - ansible.builtin.include_role: { name: audit_logging }
          vars: { audit_logging_backends: [splunk], audit_logging_status: failed }
```

## Variables

Connection vars (HEC URL/token, rsyslog host, Elasticsearch URL) ship empty and
are asserted at runtime **only** for the backend you enable. Everything else has a
working default — see `defaults/main.yml` and `meta/argument_specs.yml` for the
full contract.

| Variable | Default | Purpose |
|---|---|---|
| `audit_logging_mode` | `ship` | `ship` (buffer + ship) or `accumulate` (buffer only) |
| `audit_logging_backends` | `[]` | Which backends to ship to (mode `ship` only) |
| `audit_logging_status` | `success` | Run status stamped into the record |
| `audit_logging_hostname_command` | `hostname` | Binary to read the hostname (falls back to `uname -n`) |
| `audit_logging_whoami_command` | `whoami` | Binary to read the username (falls back to `id -un`) |
| `audit_logging_file_path` | `/var/log/ansible/audit.jsonl` | file backend path |
| `audit_logging_splunk_hec_url` | `""` | Splunk HEC base URL **(required for `splunk`)** |
| `audit_logging_splunk_hec_token` | `""` | Splunk HEC token **(required for `splunk`)** |
| `audit_logging_splunk_sourcetype` | `ansible:audit` | Splunk sourcetype |
| `audit_logging_splunk_index` | `main` | Splunk index (empty `""` = omit, use the HEC token's default index) |
| `audit_logging_syslog_tag` | `ansible-audit` | syslog tag (route to a Splunk sourcetype) |
| `audit_logging_syslog_format` | `json` | syslog body: `json` blob or `kv` logfmt |
| `audit_logging_elasticsearch_url` | `""` | ES base URL **(required for `elasticsearch`)** |
| `audit_logging_rsyslog_host` | `""` | Remote rsyslog host **(required for `rsyslog`)** |
| `audit_logging_fluentd_url` | `http://localhost:9880` | Fluentd HTTP input |
| `audit_logging_cloudwatch_log_group` | `/ansible/audit` | CloudWatch log group |

## syslog → Splunk (field extraction)

Splunk's stock `linux_messages_syslog` sourcetype does **not** JSON-parse the
message body, so a `json`-format syslog line lands unparsed. Two ways to get
searchable key/value fields:

- **`audit_logging_syslog_format: kv`** — emits logfmt `key="value"` pairs (lists
  as compact single tokens, e.g. `hosts=["web01","web02"]`). Splunk's default
  `key=value` extraction picks these up under *any* sourcetype, no config needed.
- **Keep `json` + route the tag** — point `audit_logging_syslog_tag` at a
  dedicated sourcetype and set `KV_MODE = json` for it in `props.conf`.

## Efficiency on many-host plays

The role is included per target host but its body runs only for the first
(`ansible_play_hosts_all | first`), so it ships once — but the *include itself*
(and its arg-spec validation) is still evaluated per host. On large plays you can
skip that by gating the include at the call site to the same first host:

```yaml
roles:
  - role: audit_logging
    when: inventory_hostname == ansible_play_hosts_all | first
    vars: { audit_logging_backends: [splunk] }
```

Note: role-level `when:` on entries in `roles:` is supported in Ansible 2.x.
Use `when:` (not `run_once:` — that fires once *per serial batch*). The role is
correct either way; this just avoids the per-host include overhead. The `roles`
field in the record is de-duplicated regardless of how many hosts ran.

## The audit record

A flat JSON object — every top-level key is a searchable field once indexed:

```json
{
  "timestamp": "2026-06-30T00:21:00Z",
  "user": "deploy",
  "control_node": "runner-01",
  "playbook": "Deploy nginx",
  "plays": [{"play_name": "Deploy nginx", "hosts": ["web01"], "roles": ["nginx"]}],
  "play_count": 1,
  "roles": "nginx",
  "hosts": ["web01"],
  "status": "success",
  "ansible_command": "ansible-playbook -i inventories/prod site.yml",
  "run_tags": "all",
  "skip_tags": "",
  "git_branch": "main",
  "git_commit": "a1b2c3d…",
  "git_tag": "v1.4.0",
  "git_is_clean": true,
  "git_modified_files": [],
  "git_staged_files": [],
  "git_untracked_files": []
}
```

When sent to Splunk via HEC `event`, Splunk parses it natively — `index=main
sourcetype="ansible:audit"` yields each field (`user`, `git_commit`, `status`, …)
as a key/value pair with no field extraction config.

## Requirements

- Control node: Python, `git` (for git metadata), and `nc` (rsyslog backend) /
  AWS CLI (cloudwatch backend) as applicable.
- `ansible.builtin` only — no external collections.

## Security note

The record captures the full `ansible-playbook` command line in `ansible_command`.
Supply secrets (HEC token, ES password) from Vault / `group_vars` — **not** as
plain `-e key=value` on the CLI, or they will be captured verbatim in the audit
record. (JSON `--extra-vars` blobs are already stripped by the collector.)
