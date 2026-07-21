# splunk_config

## TL;DR

**Reverse-engineer an unknown Splunk estate over the management API**, scrub
secrets into flat files, and produce a recreation checklist so you can stand up
the same shape of install in a test environment.

No Docker. No host disk mounts. No HashiCorp Vault.

```bash
# 1) Password as a flat file (mode 0600)
mkdir -p roles/splunk_config/files/secrets/work
printf '%s' "$SPLUNK_ADMIN_PASSWORD" > roles/splunk_config/files/secrets/work/api_password
chmod 600 roles/splunk_config/files/secrets/work/api_password

# 2) Export (management port :8089 — not the web UI reverse-proxy)
# Bare run is enough — the ops playbook imports export.yml directly.
ansible-playbook playbooks/ops_splunk_config_export.yml \
  -e splunk_config_api_url=https://splunk.example.com:8089 \
  -e splunk_config_stack=work \
  -e splunk_config_snapshot_dir=$PWD/captures/work-splunk
```

Outputs:
- **Scrubbed conf snapshot** — apps, system conf, dashboards (safe to review; scrub placeholders for secrets)
- **`RECREATE.md`** — checklist to rebuild a test instance
- **`estate_inventory.json`** — version, roles, apps, indexes, inputs surface
- **`files/secrets/work/`** — flat secret material (gitignored; not for git)

There is also a **container** transport (`docker exec` / `/var/lib/docker` volumes)
for Swarm estates you operate yourself. The API path is the one for “we don’t
know how production was built.”

## Requirements

| Collection | When | Used for |
|---|---|---|
| *(none beyond `ansible.builtin`)* | always | API export, container export, flat-file secrets |
| `community.hashi_vault` | Only if you set `secrets_backend=vault` | optional; never required |

## Key variables

Full list: `defaults/main.yml`. Contract: `meta/argument_specs.yml`.

| Req | Variable | Default | Purpose |
|---|---|---|---|
| **Required (API)** | `splunk_config_api_url` | `""` | Management URL `https://host:8089` |
| When API | `splunk_config_api_password` / `_file` / `secrets_dir/api_password` | — | Admin password (flat file OK) |
| Optional | `splunk_config_export_transport` | `container` | Use `api` for work reverse-engineering |
| Optional | `splunk_config_secrets_backend` | `file` | `file` (default) \| `vault` \| `none` |
| Optional | `splunk_config_secrets_dir` | `files/secrets/<stack>/` | Flat secrets (gitignored) |
| Optional | `splunk_config_snapshot_dir` | `files/snapshots/<stack>/` | Scrubbed output |

## Usage

### Work / unknown production (API only)

Use the dedicated playbook — see TL;DR. Prefer the data-plane **management**
endpoint (`:8089`). A web UI hostname that only proxies `:443`/`:8000` will not
serve REST.

```yaml
# Minimal play (also what ops_splunk_config_export.yml does)
- hosts: localhost
  connection: local
  gather_facts: false
  roles:
    - role: splunk_config
      vars:
        splunk_config_export_transport: api
        splunk_config_api_url: "https://splunk-mgmt.example.com:8089"
        splunk_config_secrets_backend: file
        splunk_config_stack: work
```

### Homelab Swarm (container transport)

```bash
ansible-playbook -i inventories/swarm/hosts.yml playbooks/app_splunk.yml --tags export
```

### Apply into a lab instance (API)

```bash
# After export (or point snapshot_dir at an existing capture):
ansible-playbook -i inventories/ops_splunk/hosts.yml \
  playbooks/ops_splunk_config_apply.yml \
  -e splunk_config_snapshot_dir=$PWD/captures/work-splunk \
  -e splunk_config_api_url=https://lab-splunk.example.com:8089 \
  -e splunk_config_api_password_file=roles/splunk_config/files/secrets/work/api_password

# Dry-run (no writes)
ansible-playbook -i inventories/ops_splunk/hosts.yml \
  playbooks/ops_splunk_config_apply.yml \
  -e splunk_config_api_apply_dry_run=true \
  -e splunk_config_snapshot_dir=$PWD/captures/work-splunk

# Broader scope (use carefully on live targets)
# -e splunk_config_api_apply_scope=all
```

### Apply into a lab instance (container / Swarm)

```bash
# After standing up a blank Splunk of the same major/minor version:
ansible-playbook … --tags apply \
  -e splunk_config_secrets_backend=file
```

Or merge the scrubbed tree into `$SPLUNK_HOME/etc` by hand using `RECREATE.md`.

## Preconditions

- **API:** network path to splunkd **:8089** and an admin-equivalent password in a
  var, password file, or `files/secrets/<stack>/api_password`.
- **Container:** Docker CLI on hosts that run Splunk tasks.
- **Apply:** prior export snapshot; flat-file secrets dir if you need
  `splunk.secret` restored for encrypted conf values.

## Behaviour

- API export filters conf/views to **ACL-owned** entries (drops inherited product noise).
- System conf is **effective** config (enough to recreate behaviour; not a pure `local/` dig).
- Secret-looking values become `<SCRUBBED:secrets>` in the snapshot.
- Secrets default to **flat files** under `files/secrets/` (gitignored). Vault is optional.

## Extending the capture surface

**You do not edit Python to harvest another conf type.** The engines under
`files/*.py` only speak REST / scrub conf. The *what* is declared in
`defaults/main.yml` (and inventory overrides).

| Want to… | Edit |
|---|---|
| API-export another conf (e.g. `multikv`, `datalake`) | Append to `splunk_config_api_conf_files` or `splunk_config_api_conf_files_extra` |
| Harvest more confs from stock apps | `splunk_config_api_stock_conf_files` / `_extra` |
| Treat an app as stock (or un-stock) | `splunk_config_stock_app_names` / `_prefixes` / their `_extra` lists |
| Stop apply from POSTing a key name | `splunk_config_api_apply_forbidden_keys` / `_extra` |
| Capture views on stock apps | `splunk_config_api_capture_views_stock: true` |
| Container-export extra `etc/` paths | `splunk_config_capture_paths` |

```yaml
# inventories/…/group_vars/all/main.yml  — append without forking the role
splunk_config_api_conf_files_extra:
  - multikv
  - datalake
```

Ansible writes a merged JSON surface (`splunk_config_surface_json`) and passes
`--surface` into the Python engines. Open that JSON after a run to see exactly
what the engines received. The surface includes `_how_to_extend` as a breadcrumb.

## Out of scope

- Index bucket data and kvstore data (definitions only)
- License key files
- Secret *values* in git
- Guaranteed bit-identical filesystem layout vs production

## Expected result

- `manifest.yml` + scrubbed conf tree
- `RECREATE.md` + `estate_inventory.json` (API transport)
- Flat secrets under `splunk_config_secrets_dir` when store is enabled
