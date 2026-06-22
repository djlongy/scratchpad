# stroom_agent

Generic, defaults-driven log forwarder that ships local log files to a
**Stroom datafeed** HTTP endpoint via `curl` + a systemd timer.

The built-in engine (a bash script + systemd oneshot + timer) requires no
external packages beyond `curl` and `gzip`, which are pre-installed on
every supported distro.  An org-supplied sender binary can optionally be
fetched alongside the script.

**Your organisation sets** `stroom_datafeed_url` and `stroom_feed_name` in
inventory — the role deliberately has no defaults for those two values and
will assert if they are empty.

---

## How it works

```
install -> configure -> service
```

| Phase | What it does |
|-------|-------------|
| `install` | Creates `stroom_install_dir` + `stroom_config_dir`; deploys `send-to-stroom.sh` from template; optionally fetches an org binary. |
| `configure` | Writes `{{ stroom_config_dir }}/log-sender.conf` (env-file sourced by the script). |
| `service` | Deploys `stroom-log-sender.service` (Type=oneshot) + `stroom-log-sender.timer`; enables and starts the timer. |

A no-tags run is a full idempotent converge.  Refinement tags narrow it:

```bash
# Only deploy the script (no service/timer changes):
--tags install

# Only regenerate the config (e.g. after rotating mTLS certs):
--tags configure

# Only manage the systemd units:
--tags service
```

---

## Variable reference

| Variable | Default | Description |
|----------|---------|-------------|
| `stroom_datafeed_url` | `""` **(required)** | Full datafeed URL, e.g. `https://stroom.example.com/stroom/datafeed` |
| `stroom_feed_name` | `""` **(required)** | Stroom Feed name, e.g. `LINUX-AUDIT-EVENTS` |
| `stroom_system_name` | `{{ inventory_hostname }}` | Value of the `System:` header |
| `stroom_environment` | `{{ env \| default('prod') }}` | Value of the `Environment:` header |
| `stroom_log_paths` | `[/var/log/messages, /var/log/secure, /var/log/audit/audit.log]` | Log files to POST on each timer firing |
| `stroom_send_interval` | `5min` | Systemd timer interval (`OnUnitActiveSec` or `OnCalendar`) |
| `stroom_install_dir` | `/opt/stroom` | Directory for the sender script and optional binary |
| `stroom_config_dir` | `/etc/stroom` | Directory for `log-sender.conf` |
| `stroom_tls_verify` | `true` | Verify TLS certificates; `false` passes `--insecure` to curl |
| `stroom_ca_cert` | `""` | Path to a custom CA bundle on the managed host (`--cacert`) |
| `stroom_client_cert` | `""` | Path to the mTLS client certificate (`--cert`) |
| `stroom_client_key` | `""` | Path to the mTLS client private key (`--key`) — keep secret |
| `stroom_log_sender_url` | `""` | Optional URL of an org-supplied sender binary |
| `stroom_log_sender_version` | `""` | Version string for the org binary (used for idempotent `creates:`) |

---

## Minimal example

```yaml
# inventories/prod/group_vars/linux_servers.yml
stroom_datafeed_url: "https://stroom.example.com/stroom/datafeed"
stroom_feed_name: "LINUX-AUDIT-EVENTS"
```

```yaml
# playbooks/L5_apps/stroom_agent.yml
---
- name: Deploy Stroom log agent
  hosts: linux_servers
  become: true
  roles:
    - role: stroom_agent
```

## mTLS example

```yaml
stroom_datafeed_url: "https://stroom.example.com/stroom/datafeed"
stroom_feed_name: "LINUX-SECURE-EVENTS"
stroom_ca_cert: /etc/pki/ca-trust/source/anchors/stroom-ca.crt
stroom_client_cert: /etc/stroom/client.crt
stroom_client_key: /etc/stroom/client.key   # kept in Vault; deploy separately
```

## Using the org-supplied sender binary

```yaml
stroom_datafeed_url: "https://stroom.example.com/stroom/datafeed"
stroom_feed_name: "LINUX-AUDIT-EVENTS"
stroom_log_sender_url: "https://artifacts.example.com/stroom-log-sender-1.2.0"
stroom_log_sender_version: "1.2.0"
```

The binary is fetched once (idempotent `creates:` guard) and symlinked to
`{{ stroom_install_dir }}/stroom-log-sender`.  The built-in shell script is
always deployed alongside it; which one the systemd service calls is
determined by `ExecStart` in the service template — override if needed via
`vars:` in your play.

---

## Security notes

- `log-sender.conf` is mode `0640` (root:root) to protect the datafeed URL
  and any mTLS key path from unprivileged users.
- `send-to-stroom.sh` is mode `0750` and runs as `root` (required for
  reading `/var/log/audit/audit.log`).
- Set `no_log: true` at the play level if `stroom_client_key` contains the
  key material itself rather than a path.
- The systemd service unit carries `PrivateTmp=true`, `NoNewPrivileges=true`,
  and `ProtectSystem=strict` to limit blast radius.
