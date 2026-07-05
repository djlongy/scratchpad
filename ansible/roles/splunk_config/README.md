# splunk_config

Capture the **entire live configuration** of a Splunk estate running on Docker
Swarm into a declarative, version-controlled **snapshot** (a readable
`manifest.yml` + native app/config bundles), and **apply** that snapshot back to
reproduce the estate. Reaches Splunk *through the containers* (`docker exec` /
`docker cp`), **auto-detects topology** (cluster manager / SHC deployer /
deployment server / search head / indexer), and **scrubs every secret** before
anything touches git — re-seeding from HashiCorp Vault on apply.

See **Why capture-first** below for the design rationale.

## Why capture-first (not hand-authored YAML)

In Splunk, **the files _are_ the config** — ~100+ `.conf` types plus dashboards
(`data/ui/views/*.xml`), nav, lookups, and per-user knowledge objects. Modelling
each as declarative YAML would be enormous and lossy. So this role captures the
config **files / app bundles** themselves (faithful, includes dashboards) and
uses a readable `manifest.yml` only as the *topology + placement contract*. It's
the standard "adopt unmanaged infrastructure into IaC" pattern: snapshot the live
system, version it, prove a round-trip, refactor later as diffs.

## Two modes (both `never`-gated — a bare run does nothing)

| Mode | Tag | What it does |
|------|-----|--------------|
| **export** | `--tags export` | Harvest each container's config subtree → scrub secrets → write `manifest.yml` + bundles under `files/snapshots/<stack>/`; push each `splunk.secret` to Vault. |
| **apply** | `--tags apply` | Place the snapshot back into the correct tier staging dirs, reseed secrets from Vault, trigger each tier's bundle push / reload. |

```bash
# Capture
ansible-playbook -i inventories/swarm/hosts.yml \
    playbooks/splunk_config.yml --tags export

# Restore (dry: stage files, skip pushes)
ansible-playbook -i inventories/swarm/hosts.yml \
    playbooks/splunk_config.yml --tags apply \
    -e splunk_config_apply_push_bundles=false
```

## What gets captured

Per auto-detected tier:

| Tier | Captured `etc/` |
|------|-----------------|
| Cluster manager | `manager-apps/` |
| SHC deployer | `shcluster/apps/` |
| Deployment server | `deployment-apps/` + `system/local/serverclass.conf` |
| Search head(s) | `apps/*/local/**` (dashboards, nav), `users/*/*/local/**`, `system/local/*.conf` |
| Indexer(s) | `system/local/*.conf`, `apps/*/local` |

**Scope rule:** every app contributes `local/**` + `metadata/local.meta`;
`default/**` is captured only for genuinely custom apps (Splunk-shipped apps —
`search`, `splunk_*`, `TA-*`, … — contribute only your local overrides).

## Secrets

Scrubbed **before** the snapshot is written and re-seeded from Vault on apply:
`splunk.secret`, `etc/passwd`, `$1$`/`$7$`-encrypted values, `sslPassword`,
`pass4SymmKey`, credential stanzas, `passwords.conf`. Every removal is listed in
`SECRETS-SCRUBBED.md` and the manifest's `scrubbed_secrets`. Restoring
`splunk.secret` from Vault is what keeps `$7$`-encrypted conf values valid across
a rebuild. Export fails hard if any secret pattern survives the scrub.

## Snapshot layout

```
files/snapshots/<stack>/
  manifest.yml            # topology + placement + counts + scrubbed-secret index
  SECRETS-SCRUBBED.md      # human index of redactions
  manager-apps/<app>/…     # (if a cluster manager was detected)
  shcluster-apps/<app>/…   # (if a deployer was detected)
  deployment-apps/<app>/…  # (if a deployment server was detected)
  instance-<role>-<service>/{apps,system-local,users}/…
```

## Key variables

| Variable | Default | Purpose |
|----------|---------|---------|
| `splunk_config_stack` | `splunk` | `docker ps` name filter selecting the Splunk containers |
| `splunk_config_snapshot_dir` | `<role>/files/snapshots/<stack>` | where the snapshot is written/read |
| `splunk_config_capture_paths` | see defaults | `etc/` subtrees harvested per container |
| `splunk_config_vault_mount` / `_vault_path` | `<vault_kv_mount>` / `apps/splunk/config` | Vault location for `splunk.secret` |
| `splunk_config_apply_push_bundles` | `true` | trigger tier bundle pushes on apply |
| `splunk_config_apply_restart_instances` | `false` | restart simple SH/indexers on apply to load config |
| `splunk_config_admin_password` | `""` | estate admin credential for CLI pushes (Vault lookup); empty → pushes skipped with a warning |

## Requirements & scope

- Runs against **all** swarm nodes (`swarm_managers:swarm_workers`) — a container
  is exec-reachable only on the node hosting it.
- **In scope:** all `.conf` knowledge, dashboards + nav, lookups, app bundles,
  serverclasses, kvstore collection *definitions*, tier topology.
- **Out of scope (v1, flagged in `manifest.out_of_scope_v1`):** kvstore *data*,
  index *bucket data*, license files — and secret *values* (re-seeded from Vault).
- Apply **overwrites** placed config and re-runs each push, so it is convergent /
  re-appliable — not a fine-grained no-op diff.
