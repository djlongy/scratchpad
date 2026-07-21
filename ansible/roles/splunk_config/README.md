# splunk_config

## TL;DR

Captures the entire live configuration of a Splunk estate running on Docker
Swarm into a version-controlled snapshot (a readable `manifest.yml` + native
app/config bundles), and applies that snapshot back to reproduce the estate.
Reaches Splunk through the containers (`docker exec` / `docker cp`),
auto-detects topology (cluster manager / SHC deployer / deployment server /
search head / indexer), and scrubs every secret before anything touches git
— re-seeding from Vault on apply. Both modes are `never`-gated, so a bare
run does nothing.

```bash
ansible-playbook -i inventories/<env>/hosts.yml playbooks/splunk_config.yml --tags export   # capture
ansible-playbook -i inventories/<env>/hosts.yml playbooks/splunk_config.yml --tags apply    # restore
```

## Requirements

Install collections before running (repo `requirements.yml`, or ad-hoc):

    ansible-galaxy collection install -r requirements.yml

| Collection | When | Used for |
|---|---|---|
| `community.hashi_vault` | always | reading/writing `splunk.secret` at `splunk_config_vault_mount`/`_vault_path` |

## Key variables

Full list: `defaults/main.yml`. Contract: `meta/argument_specs.yml`.

**Required** = value must be correct for a successful run (defaults often work).
**Optional** = safe to leave default / empty; phase stays off or uses built-ins.
**When X** = required only if that feature is on.

| Req | Variable | Default | Purpose |
|---|---|---|---|
| Optional | `splunk_config_stack` | `splunk` | `docker ps` name filter selecting the Splunk containers |
| Optional | `splunk_config_snapshot_dir` | `<role>/files/snapshots/<stack>` | Where the snapshot is written/read |
| Optional | `splunk_config_capture_paths` | see defaults | `etc/` subtrees harvested per container |
| Optional | `splunk_config_vault_mount` / `_vault_path` | `<vault_kv_mount>` / `apps/splunk/config` | Vault location for `splunk.secret` |
| Optional | `splunk_config_apply_push_bundles` | `true` | Trigger tier bundle pushes on apply |
| Optional | `splunk_config_apply_restart_instances` | `false` | Restart simple SH/indexers on apply to load config |
| When apply pushes | `splunk_config_admin_password` | `""` | Estate admin credential for CLI pushes (Vault lookup); empty → pushes skipped with a warning |

## Usage

```yaml
- name: Capture / re-apply the Splunk estate configuration
  hosts: swarm_managers:swarm_workers
  become: true
  roles:
    - role: splunk_config
```

Run it:

```bash
# Capture
ansible-playbook -i inventories/<env>/hosts.yml playbooks/splunk_config.yml --tags export

# Restore (dry: stage files, skip pushes)
ansible-playbook -i inventories/<env>/hosts.yml playbooks/splunk_config.yml --tags apply \
    -e splunk_config_apply_push_bundles=false
```

## Preconditions

- Target play must include every swarm node (`swarm_managers:swarm_workers`)
  — a container is exec-reachable only on the node hosting it.
- `apply` re-seeding `splunk.secret` from Vault requires that secret
  already exists at `splunk_config_vault_mount`/`_vault_path` from a prior
  `export` run.

## Behaviour

- Both `export` and `apply` are `never`-gated — a bare run with no `--tags`
  does nothing.
- `export` scrubs `splunk.secret`, `etc/passwd`, `$1$`/`$7$`-encrypted
  values, `sslPassword`, `pass4SymmKey`, credential stanzas, and
  `passwords.conf` before anything is written to the snapshot, and fails
  hard if any secret pattern survives the scrub. Every removal is listed in
  `SECRETS-SCRUBBED.md` and the manifest's `scrubbed_secrets`.
- Captures `local/**` + `metadata/local.meta` for every app; `default/**`
  only for genuinely custom apps — Splunk-shipped apps (`search`,
  `splunk_*`, `TA-*`, …) contribute only their local overrides.
- `apply` overwrites placed config and re-runs each tier's push, so it is a
  convergent reconcile, not a fine-grained no-op diff.

## Out of scope

- kvstore *data* (kvstore collection *definitions* are captured)
- Index *bucket data*
- License files
- Secret *values* — re-seeded from Vault, not stored in the snapshot

## Expected result

- `export` leaves `manifest.yml` + per-tier bundles under
  `files/snapshots/<stack>/`, with `scrubbed_secrets` populated and zero
  secret patterns remaining.
- `apply` (dry: `-e splunk_config_apply_push_bundles=false`) stages files
  without pushing; a full apply triggers each detected tier's bundle
  push/reload.
