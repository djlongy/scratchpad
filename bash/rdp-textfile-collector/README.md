# rdp-textfile-collector

node_exporter textfile collector for RDP (TCP/3389) connection health.
Parses `ss -tin` (kernel `tcp_info`) for sockets where either end is :3389,
aggregates per peer + direction (server/client), and emits Prometheus metrics.

## Metrics

Per-peer gauges (`direction`, `peer`, `port` labels):

| metric | meaning |
|---|---|
| `rdp_tcp_connections` | current established RDP TCP conns to this peer |
| `rdp_tcp_rtt_seconds` | mean smoothed RTT |
| `rdp_tcp_rttvar_seconds` | mean RTT variance |
| `rdp_tcp_min_rtt_seconds` | min observed RTT |
| `rdp_tcp_rto_seconds` | max current retransmission timeout |
| `rdp_tcp_retrans_in_flight` | packets awaiting retransmission *now* |
| `rdp_tcp_retrans_total` | lifetime retransmits on current conns |
| `rdp_tcp_lost_packets` | packets the kernel currently considers lost |
| `rdp_tcp_reordering` | kernel reorder metric (default 3, grows on reorder) |
| `rdp_tcp_reord_seen_total` | actual reorder events observed (kernel 5.x+) |
| `rdp_tcp_bytes_sent` / `rdp_tcp_bytes_retrans` | bytes out / retransmitted |
| `rdp_tcp_data_segs_out` | data segments sent (for retrans ratio) |
| `rdp_tcp_unacked_segments` | unacked segments in flight |

Host-wide (for correlation against the per-peer values):
`rdp_host_tcp_retrans_segs`, `rdp_host_tcp_lost_retransmit`,
`rdp_host_tcp_timeouts`, `rdp_host_tcp_reordering`.

## Install

```bash
sudo install -o root -g root -m 0755 rdp_textfile_collector.sh \
  /usr/local/bin/rdp_textfile_collector.sh
sudo install -d -o node_exporter -g node_exporter \
  /var/lib/node_exporter/textfile_collector
```

Make sure node_exporter is started with:

```
--collector.textfile.directory=/var/lib/node_exporter/textfile_collector
```

### systemd timer (15s cadence)

`/etc/systemd/system/rdp-textfile-collector.service`:

```ini
[Unit]
Description=Collect RDP TCP metrics for node_exporter

[Service]
Type=oneshot
Environment=TEXTFILE_DIR=/var/lib/node_exporter/textfile_collector
Environment=RDP_PORT=3389
ExecStart=/usr/local/bin/rdp_textfile_collector.sh
User=root
```

`/etc/systemd/system/rdp-textfile-collector.timer`:

```ini
[Unit]
Description=Run RDP TCP collector every 15s

[Timer]
OnBootSec=30s
OnUnitActiveSec=15s
AccuracySec=1s
Unit=rdp-textfile-collector.service

[Install]
WantedBy=timers.target
```

```
sudo systemctl daemon-reload
sudo systemctl enable --now rdp-textfile-collector.timer
```

`ss` runs as root to read tcp_info from all sockets (an unprivileged user only
sees its own). If you want a non-root user, grant `CAP_NET_ADMIN` +
`CAP_NET_RAW` to the script binary or run via a tiny wrapper.

## Useful PromQL

- RTT trend: `rdp_tcp_rtt_seconds`
- Retransmit ratio (per peer):
  `rate(rdp_tcp_bytes_retrans[5m]) / rate(rdp_tcp_bytes_sent[5m])`
- Host-wide retransmit rate: `rate(rdp_host_tcp_retrans_segs[5m])`
- Timeouts firing: `increase(rdp_host_tcp_timeouts[5m]) > 0`
- RTT jitter spike: `deriv(rdp_tcp_rttvar_seconds[5m]) > 0.01`
- Reorder events: `increase(rdp_tcp_reord_seen_total[5m]) > 0`

## Kernel / distro notes

- **Oracle Linux 8 / RHEL 8 (kernel 4.18):** fully supported. `bytes_retrans`
  and all the other per-socket fields this script reads are present. The only
  field missing is `reord_seen` (added in 5.1), so `rdp_tcp_reord_seen_total`
  will be a flat 0 — for reorder detection on EL8, use `rdp_tcp_reordering`
  (per-conn gauge, alert when it rises above 3) plus the host-wide
  `rdp_host_tcp_reordering` counter (TCPSACKReorder + TCPTSReorder etc.),
  which works fine on 4.18.
- **EL9 / kernel 5.14+:** everything including `reord_seen` is populated.
- No `gawk` dependency — uses bash parameter expansion plus stock `awk`, so
  mawk / BSD awk environments also work.

## Notes / limitations

- `ss` only shows *current* sockets, so `rdp_tcp_*` gauges reset whenever a
  session drops. Use host-wide `rdp_host_tcp_*` counters for long-range trends.
- Only looks at `state established` — handshakes and half-open sockets are
  ignored. If you need to track SYN retries, extend the `ss` state filter to
  `syn-sent` and parse the same output.
- `reordering` defaults to 3 on a healthy connection — only treat *rises* as
  meaningful.
- Works for both server-side (xrdp, etc.) and client-side (jump host dialling
  out to Windows) — `direction` label distinguishes them.
