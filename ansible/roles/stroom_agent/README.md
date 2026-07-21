# stroom_agent

## TL;DR

Generic, defaults-driven log forwarder that ships local log files to a
Stroom datafeed HTTP endpoint via `curl` + a systemd timer. The built-in
engine (a bash script + systemd oneshot + timer) needs no external packages
beyond `curl` and `gzip`. Your organisation sets `stroom_datafeed_url` and
`stroom_feed_name` in inventory — the role has no defaults for those two
and asserts if they are empty.

```bash
ansible-playbook -i inventories/<env>/hosts.yml playbooks/<playbook>.yml --tags configure
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
| **Required** | `stroom_datafeed_url` | `""` | Full datafeed URL, e.g. `https://stroom.example.com/stroom/datafeed` |
| **Required** | `stroom_feed_name` | `""` | Stroom Feed name, e.g. `LINUX-AUDIT-EVENTS` |
| Optional | `stroom_system_name` | `{{ inventory_hostname }}` | Value of the `System:` header |
| Optional | `stroom_environment` | `{{ env \| default('prod') }}` | Value of the `Environment:` header |
| Optional | `stroom_log_paths` | `[/var/log/messages, /var/log/secure, /var/log/audit/audit.log]` | Log files to POST on each timer firing |
| Optional | `stroom_send_interval` | `5min` | Systemd timer interval (`OnUnitActiveSec` or `OnCalendar`) |
| Optional | `stroom_tls_verify` | `true` | Verify TLS certificates; `false` passes `--insecure` to curl |
| When mTLS | `stroom_ca_cert` / `stroom_client_cert` / `stroom_client_key` | `""` | CA bundle / client cert / client key paths for mTLS (`--cacert`/`--cert`/`--key`) |
| When org binary | `stroom_log_sender_url` / `stroom_log_sender_version` | `""` | Fetch an org-supplied sender binary instead of using the built-in script only |

## Usage

```yaml
- name: Deploy Stroom log agent
  hosts: <group>
  become: true
  roles:
    - role: stroom_agent
      vars:
        stroom_datafeed_url: "https://stroom.example.com/stroom/datafeed"
        stroom_feed_name: "LINUX-AUDIT-EVENTS"
```

Run it:

```bash
ansible-playbook -i inventories/<env>/hosts.yml playbooks/<playbook>.yml --tags configure
```

## Preconditions

- `stroom_datafeed_url` must point to a reachable Stroom datafeed endpoint
  that already has `stroom_feed_name` configured to accept the feed.
- For mTLS, `stroom_ca_cert` / `stroom_client_cert` / `stroom_client_key`
  must already exist at those paths on the host — this role does not
  deploy them.

## Behaviour

- Fetches an org-supplied sender binary once (idempotent `creates:` guard)
  and symlinks it to `{{ stroom_install_dir }}/stroom-log-sender`; the
  built-in shell script is always deployed alongside it, and `ExecStart` in
  the service template decides which one systemd calls.
- Rerun with `--tags configure` after rotating mTLS certs to regenerate
  `log-sender.conf` without touching the install phase.
- `log-sender.conf` is mode `0640` (root:root); `send-to-stroom.sh` is mode
  `0750` and runs as `root` (needed to read `/var/log/audit/audit.log`).
- The systemd unit carries `PrivateTmp=true`, `NoNewPrivileges=true`, and
  `ProtectSystem=strict` to limit blast radius.
- Set `no_log: true` at the play level if `stroom_client_key` holds key
  material directly rather than a path.
