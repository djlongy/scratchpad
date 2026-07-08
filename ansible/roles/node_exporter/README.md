# node_exporter

Renamed from `sw_node_exporter` (bare resource noun; drop the `sw_` verb prefix).
Note: hosts running the `alloy` role get node metrics via Alloy's built-in unix
exporter instead.

This role installs and configures Prometheus Node Exporter. It includes:
- Installing Node Exporter on systems
- Configuring system metrics collection
- Setting up service management for Node Exporter
- Managing Node Exporter configuration files
- Ensuring proper monitoring integration with Prometheus

The role provides comprehensive system metrics export for infrastructure monitoring.

## TL;DR

**Most common: install node_exporter.** Run-once per host and idempotent on re-run; skip hosts running the `alloy` role (they already get node metrics via Alloy's built-in exporter).

```bash
ansible-playbook -i inventories/<env>/hosts.yml playbooks/site.yml [--limit <host>]
```