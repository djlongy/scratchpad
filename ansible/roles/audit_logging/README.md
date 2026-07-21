# audit_logging

## TL;DR

Portable, backend-agnostic audit logging for `ansible-playbook` runs. Captures who ran
a playbook, from where, against which hosts and roles, with which CLI arguments and git
state — then ships one structured JSON record to any combination of backends (file,
syslog, rsyslog, fluentd, elasticsearch, splunk, cloudwatch).

```bash
ansible-playbook -i inventories/<env>/hosts.yml playbooks/audit_single_logger.yml
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
| Optional | `audit_logging_mode` | `ship` | `ship` (buffer + ship) or `accumulate` (buffer only, multi-play) |
| Optional | `audit_logging_backends` | `[]` | Backends to ship to (mode `ship` only) — `file`/`syslog`/`rsyslog`/`fluentd`/`elasticsearch`/`splunk`/`cloudwatch` |
| Optional | `audit_logging_status` | `success` | Run status stamped into the record (set `failed` from a rescue block) |
| Optional | `audit_logging_file_path` | `/var/log/ansible/audit.jsonl` | `file` backend path |
| When `splunk` | `audit_logging_splunk_hec_url` | `""` | Splunk HEC base URL |
| When `splunk` | `audit_logging_splunk_hec_token` | `""` | Splunk HEC token |
| When `elasticsearch` | `audit_logging_elasticsearch_url` | `""` | ES base URL |
| When `rsyslog` | `audit_logging_rsyslog_host` | `""` | Remote rsyslog host |
| Optional | `audit_logging_syslog_format` | `json` | syslog body: `json` blob or `kv` logfmt (Splunk auto-extracts `kv` fields) |
| Optional | `audit_logging_fluentd_url` | `http://localhost:9880` | Fluentd HTTP input |
| Optional | `audit_logging_cloudwatch_log_group` | `/ansible/audit` | CloudWatch log group |

## Usage

Single-play, inline under `roles:` (preferred):

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

Run it:

```bash
ansible-playbook -i inventories/<env>/hosts.yml playbooks/audit_single_logger.yml
```

Multi-play — `accumulate` in intermediate plays, `ship` with backends in the final play:

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

Recording a failed run from a rescue block:

```yaml
post_tasks:
  - block:
      - ansible.builtin.include_role: { name: audit_logging }
        vars: { audit_logging_backends: [splunk] }
    rescue:
      - ansible.builtin.include_role: { name: audit_logging }
        vars: { audit_logging_backends: [splunk], audit_logging_status: failed }
```

## Preconditions

Control node needs `git` already installed (for git metadata) and, per backend used,
`nc` (rsyslog) or the AWS CLI (cloudwatch).

## Behaviour

All shipping runs exactly once on the control node — the record describes the *run*,
not each target host. The body is delegated to `localhost` and gated to the first host
of the whole play (`ansible_play_hosts_all | first`), so it ships once under the
linear, `free`, and `serial:` strategies alike (`run_once` is deliberately not used —
it fires once *per serial batch*). Every backend is best-effort (`ignore_errors`) so
audit logging never fails the play.

The shipped record is a flat JSON object — every top-level key is a searchable field
once indexed:

```json
{
  "timestamp": "2026-06-30T00:21:00Z",
  "user": "deploy",
  "control_node": "runner-01",
  "playbook": "Deploy nginx",
  "plays": [{"play_name": "Deploy nginx", "hosts": ["web01"], "roles": ["nginx"]}],
  "roles": "nginx",
  "hosts": ["web01"],
  "status": "success",
  "ansible_command": "ansible-playbook -i inventories/<env> site.yml",
  "run_tags": "all",
  "git_branch": "main",
  "git_commit": "a1b2c3d…",
  "git_is_clean": true
}
```

The record captures the full `ansible-playbook` command line in `ansible_command`.
Supply secrets (HEC token, ES password) from Vault / `group_vars` — not as plain
`-e key=value` on the CLI, or they land verbatim in the audit record. JSON
`--extra-vars` blobs are already stripped by the collector.
