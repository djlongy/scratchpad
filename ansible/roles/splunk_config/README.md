# splunk_config

## TL;DR

Captures the entire live configuration of a Splunk-on-Docker estate into a
declarative, version-controlled snapshot (a readable `manifest.yml` + native
app/config bundles), and can apply that snapshot back when the estate supports
it. Reaches Splunk through the containers (`docker exec` / `docker cp`),
auto-detects topology, and scrubs every secret before anything touches git.

**Export is the bare-minimum path** — it works without Vault, without admin
credentials, and without knowing whether storage is local, bind-mounted, or
NFS-backed Docker volumes. Apply is best-effort on top of a prior export.

```bash
ansible-playbook -i inventories/swarm/hosts.yml playbooks/app_splunk.yml --tags export
```

## Requirements

Install collections before running (repo `requirements.yml`, or ad-hoc):

    ansible-galaxy collection install -r requirements.yml

| Collection | When | Used for |
|---|---|---|
| `community.hashi_vault` | When Vault push/reseed is enabled *and* `vault_addr` is set | pushing / reseeding `splunk.secret` |

Export does **not** require HashiCorp Vault. When Vault is unset or unreachable,
export still writes the scrubbed snapshot; only the optional secret-reseed
path for apply is deferred.

## Key variables

Full list: `defaults/main.yml`. Contract: `meta/argument_specs.yml`.

**Required** = value must be correct for a successful run (defaults often work).
**Optional** = safe to leave default / empty; phase stays off or uses built-ins.
**When X** = required only if that feature is on.

| Req | Variable | Default | Purpose |
|---|---|---|---|
| Optional | `splunk_config_stack` | `splunk` | Primary `docker ps` name filter |
| Optional | `splunk_config_name_filters` | `[]` | Extra name filters (OR) for foreign estates |
| Optional | `splunk_config_image_filters` | `[]` | Image-substring filters (OR), e.g. `splunk/splunk` |
| Optional | `splunk_config_snapshot_dir` | `<role>/files/snapshots/<stack>` | Where the snapshot is written/read |
| Optional | `splunk_config_capture_paths` | see defaults | `etc/` subtrees harvested per container |
| Optional | `splunk_config_export_ignore_capture_errors` | `true` | One failed container does not abort export |
| Optional | `splunk_config_export_fail_on_empty` | `false` | Fail export when zero containers match |
| Optional | `splunk_config_docker_root` | `/var/lib/docker` | Docker data-root; volumes live under `<root>/volumes/<name>/_data` |
| Optional | `splunk_config_export_host_volume_fallback` | `true` | When the container is stopped, read config from the host volume path |
| Optional | `splunk_config_export_include_stopped` | `true` | Include stopped containers in discovery |
| Optional | `splunk_config_export_record_mounts` | `true` | Record Docker mount metadata (NFS vs bind) |
| Optional | `splunk_config_vault_path` | `apps/splunk/config` | Vault KV path for `splunk.secret` |
| Optional | `splunk_config_export_push_splunk_secret` | `true` | Push `splunk.secret` to Vault on export (soft-fails) |
| Optional | `splunk_config_apply_reseed_secrets` | `true` | Reseed secrets from Vault on apply (soft-skips) |
| Optional | `splunk_config_apply_push_bundles` | `true` | Trigger tier bundle pushes on apply |
| When bundle push | `splunk_config_admin_password` | `""` | Estate admin credential; empty skips pushes |

## Usage

```yaml
- hosts: swarm_managers:swarm_workers
  become: true
  roles:
    - splunk_config
```

Foreign / work estate — cast a wider discovery net and keep the snapshot out of
the role tree:

```yaml
- hosts: swarm_managers:swarm_workers
  become: true
  roles:
    - role: splunk_config
      vars:
        splunk_config_stack: ""                 # optional if image filters alone match
        splunk_config_name_filters:
          - splunk
          - idx
          - sh-
        splunk_config_image_filters:
          - splunk/splunk
          - splunk/universalforwarder
        splunk_config_snapshot_dir: "{{ playbook_dir }}/../captures/work-splunk"
        splunk_config_export_push_splunk_secret: false
```

Run it — pick a mode with a tag, both are `never`-gated so a bare run does
nothing:

```bash
# Capture (bare-minimum retrieve — no Vault required)
ansible-playbook -i inventories/swarm/hosts.yml playbooks/app_splunk.yml --tags export

# Restore (dry: stage files, skip pushes)
ansible-playbook -i inventories/swarm/hosts.yml playbooks/app_splunk.yml --tags apply \
    -e splunk_config_apply_push_bundles=false
```

## Preconditions

- Runs against **all** nodes that may host Splunk containers (e.g.
  `swarm_managers:swarm_workers`) — a container is exec-reachable only on the
  node hosting it.
- Docker CLI on each target host. Export prefers `docker exec` / `docker cp`;
  when a task is stopped it falls back to host paths under Docker's data-root
  (default `/var/lib/docker`, or whatever `docker info` reports). Named volumes
  — including local-driver + NFS opts — appear at
  `<docker_root>/volumes/<name>/_data`; the NFS server export path is not
  required for retrieve.
- For `apply`: a prior export snapshot must exist. Vault reseed and bundle
  pushes are optional soft-paths — placement still runs when they are skipped.

## Behaviour

- Capture scope is auto-detected per tier: cluster manager → `manager-apps/`,
  SHC deployer → `shcluster/apps/`, deployment server → `deployment-apps/` +
  `serverclass.conf`, search head(s) → app/user/system local overrides,
  indexer(s) → system/app local overrides. Every app contributes `local/**` +
  `metadata/local.meta`; `default/**` is captured only for genuinely custom
  apps (Splunk-shipped apps contribute local overrides only).
- In-container etc root is auto-detected (`/opt/splunk/etc` or
  `/opt/splunkforwarder/etc`).
- Secrets are scrubbed before the snapshot is written. When Vault is configured,
  `splunk.secret` is pushed on export and re-seeded on apply; when it is not,
  export still succeeds and apply placement still runs.
- Mount metadata (type / name / source / destination / driver) is recorded per
  instance. Volume `Source` paths are under `splunk_config_docker_root`
  (default `/var/lib/docker`) even when the backend is NFS.
- Apply **overwrites** placed config and re-runs each push, so it is
  convergent / re-appliable — not a fine-grained no-op diff.

## Out of scope

- kvstore *data* and index *bucket data* — only kvstore collection
  *definitions* are captured.
- License files.
- Secret *values* in git — captured as scrubbed placeholders; re-seeded from
  Vault on apply when Vault is available.
- Guaranteeing apply on a foreign estate — export is the contract; apply needs
  live containers, matching service names, and (for encrypted values) Vault.

## Expected result

- `--tags export` produces `manifest.yml` + scrubbed bundles under
  `splunk_config_snapshot_dir`, even when Vault is absent or a single container
  fails to capture.
- `SECRETS-SCRUBBED.md` lists every redaction.
- `--tags apply` places what it can and skips (with a warning) reseed/push when
  credentials or Vault are missing.
