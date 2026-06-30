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

All shipping runs **once on the control node** (`delegate_to: localhost`,
`run_once: true`) — the record describes the *run*, not each target host. Every
backend is best-effort (`ignore_errors`) so audit logging never fails the play.

## Usage

### Single-play playbook

```yaml
- hosts: webservers
  roles:
    - role: nginx
  post_tasks:
    - name: Write audit log
      ansible.builtin.include_role:
        name: audit_logging
      vars:
        audit_logging_backends: [splunk, file]
        audit_logging_splunk_hec_url: "https://splunk.example.com:8088"
        audit_logging_splunk_hec_token: "{{ vault_splunk_hec_token }}"
```

### Multi-play (monolithic) playbook

Buffer each play, write once at the end:

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

### Recording failed runs

Wrap the play body so a failure still emits an audited record:

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
| `audit_logging_backends` | `[]` | Which backends to ship to |
| `audit_logging_status` | `success` | Run status stamped into the record |
| `audit_logging_file_path` | `/var/log/ansible/audit.jsonl` | file backend path |
| `audit_logging_splunk_hec_url` | `""` | Splunk HEC base URL **(required for `splunk`)** |
| `audit_logging_splunk_hec_token` | `""` | Splunk HEC token **(required for `splunk`)** |
| `audit_logging_splunk_sourcetype` | `ansible:audit` | Splunk sourcetype |
| `audit_logging_splunk_index` | `ansible` | Splunk index |
| `audit_logging_elasticsearch_url` | `""` | ES base URL **(required for `elasticsearch`)** |
| `audit_logging_rsyslog_host` | `""` | Remote rsyslog host **(required for `rsyslog`)** |
| `audit_logging_fluentd_url` | `http://localhost:9880` | Fluentd HTTP input |
| `audit_logging_cloudwatch_log_group` | `/ansible/audit` | CloudWatch log group |

## The audit record

A flat JSON object — every top-level key is a searchable field once indexed:

```json
{
  "timestamp": "2026-06-30T00:21:00Z",
  "user": "long",
  "control_node": "mba.local",
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

When sent to Splunk via HEC `event`, Splunk parses it natively — `index=ansible
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
