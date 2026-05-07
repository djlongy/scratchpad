# swarm_stack

Generic engine for deploying any application stack onto an existing Docker
Swarm. Built on `community.docker.docker_swarm_service` with externalised
resources (encrypted overlay network with pinned subnet, NFS-backed
local-driver volumes pre-created on every candidate worker, content-versioned
docker secrets and configs) and a per-service deploy loop. Designed to be
resilient to Ansible's lazy variable evaluation and to support full
destroy → recreate via a single tag.

Use this role through a thin **wrapper role** that hands it a
`swarm_stack_*` spec for the app you're deploying. `roles/mattermost_swarm/`
is the worked example — copy-paste it as the starting point for a new app.

## What it does

1. Asserts every entry in `swarm_stack_services` has a non-empty `name`
   and a non-empty `image` URL.
2. mkdirs the NFS subpaths (delegated to the NFS host).
3. Reads each declared secret value from Ansible variable scope, creates a
   docker secret per entry, name suffixed `_v1`, `_v2`, … on content change.
4. Renders + creates docker configs (same versioning).
5. Pre-creates the encrypted overlay network with the pinned subnet.
6. Pre-creates each NFS-backed local-driver volume on every candidate
   worker (so swarm can schedule there without on-demand mounts).
7. Iterates `swarm_stack_services` and calls `docker_swarm_service` per
   entry. Each service's env block can come from either:
   - `env_template` — a Jinja YAML file rendered LATE so it can reference
     `swarm_stack_secret_values` and `_config_names`, or
   - `env_static` — a literal dict for services with no late-binding.
8. Prunes labeled objects no longer referenced (skips in-use).

## Resilience to lazy var eval

The whole point of splitting `swarm_stack_services` (top-level scalars)
from `env_template` (file path resolved lazily) is to avoid a sharp edge
in Ansible's `include_role` semantics. The vars block on `include_role`
is evaluated EAGERLY when the role is included — so any expression inside
`swarm_stack_services` that references something the role itself populates
(`swarm_stack_secret_values.x`, `swarm_stack_config_names.y`) blows up
before the role even gets to the secrets/configs phase. By moving those
references into a separate template file referenced by *path*, the role
can render them at deploy time when the secret/config registries are
populated.

The role only ever reads top-level fields from each service entry
(`item.name`, `item.image`, `item.networks`, etc), and uses
`item.get('update', omit)` for the `update` key — `update` is a dict
method name and `item.update` resolves to the bound method, not the dict
value, which Ansible chokes on with "builtin_function_or_method is not
JSON serializable". Don't fall into that trap when adding new fields
named after dict methods (`update`, `keys`, `values`, `items`, `get`,
`pop`, `clear`, `copy`, `setdefault`).

## Image URLs

Each service entry sets `image:` directly to a full image URL — no
intermediate map. Use a wrapper-defaults variable so swap-once-propagate
is still one edit:

```yaml
# roles/<wrapper>/defaults/main.yml
mattermost_postgres_image: "registry.example.com/library/postgres:17-alpine"
mattermost_app_image:      "mattermost/mattermost-team-edition:9.11"
```

```yaml
# roles/<wrapper>/tasks/main.yml — inside the include_role vars block
swarm_stack_services:
  - name: mm-postgres
    image: "{{ mattermost_postgres_image }}"
    ...
  - name: mm-app
    image: "{{ mattermost_app_image }}"
    ...
```

> ⚠️ **Lazy-eval trap (still applies)**: `image:` is safe to embed
> directly because it resolves to a wrapper-defaults variable. Do NOT
> embed references to anything the engine itself populates
> (`swarm_stack_secret_values`, `swarm_stack_secret_names`,
> `swarm_stack_config_names`) in `swarm_stack_services` — Ansible
> evaluates that block eagerly when the engine is included, before the
> registries are populated. Late-bound values go in `env_template`.

## Overlay subnet registry

Pin every overlay's subnet via a single map in inventory:

```yaml
# inventories/<env>/group_vars/<group>/main.yml
swarm_overlay_subnets:
  mattermost: "10.40.10.0/24"
  splunk:     "10.40.11.0/24"
  # next free: 10.40.12.0/24
```

Each wrapper looks up its subnet by stack name:

```yaml
swarm_stack_networks:
  - name: mm_overlay
    attachable: true
    encrypted: true
    subnet: "{{ swarm_overlay_subnets[mattermost_stack_name] }}"
```

The registry is the single source of truth — collisions between overlays
show up as a diff to one file at PR review, not as silent east-west
traffic black-holes between two stacks that happened to grab overlapping
ranges from swarm's default pool.

## Redeploy + teardown

Three tag-gated flows:

- **default (no tags)** — full create/update chain (idempotent).
- **`--tags redeploy`** — destroy everything this stack owns (services,
  then label-scoped secrets/configs/networks/volumes), then re-run the
  full create chain. NFS data on disk is preserved.
- **`--tags teardown`** — destroy only, leave nothing running.
- **`--tags wipe-data`** (combined with redeploy or teardown) — also
  rm -rf the NFS data directories. Destructive.

```bash
ansible-playbook -i inventories/swarm/hosts.yml playbooks/mattermost_swarm.yml                      # idempotent update
ansible-playbook -i inventories/swarm/hosts.yml playbooks/mattermost_swarm.yml --tags redeploy      # full destroy + recreate, data preserved
ansible-playbook -i inventories/swarm/hosts.yml playbooks/mattermost_swarm.yml --tags teardown      # destroy only
ansible-playbook -i inventories/swarm/hosts.yml playbooks/mattermost_swarm.yml --tags redeploy,wipe-data
```

Service discovery for teardown filters on both `swarm_stack=<name>` label
(services this role created) AND name prefix `<stack>_` (services left
over from a legacy `docker stack deploy` that didn't carry the role's
label) so cutover from a stack-deployed predecessor works.

## Secret pattern

Two kinds of secrets fit cleanly:

- **File-as-secret** (postgres, redis, anything that reads `*_FILE`).
  Listed in `swarm_stack_secrets`, then referenced by name in a service's
  `secrets:` list. Role mounts the file at `/run/secrets/<name>` and
  resolves the hashed name automatically. Use
  `POSTGRES_PASSWORD_FILE: /run/secrets/mm_pg_password` in the service env.

- **Bake-into-env** (apps that don't read `/run/secrets/`). Same
  `swarm_stack_secrets` entry, but the env_template references
  `swarm_stack_secret_values.<name>` to bake the value into the env dict.
  Plaintext lives only in raft (encrypted) inside the service spec.

## Rolling-version mechanics

`docker_secret` / `docker_config` with `rolling_versions: true` inspects
existing objects matching `<name>_v[0-9]+`. If the latest version's
content matches what you're submitting, it's a no-op. If different, it
creates `<name>_v(N+1)` and the next deploy uses the new hashed name →
swarm rolls. Prune deletes labeled objects no longer referenced by the
current spec; in-use objects are skipped.

## Worker node setup (one-time)

The `community.docker.docker_*` modules need the Docker Python SDK on
both the manager AND every worker (the `delegate_to` volume creation
runs the module against each worker). On AlmaLinux/Rocky/RHEL 9:

```bash
sudo dnf install -y python3-docker python3-requests python3-jsondiff
```

(`python3-jsondiff` lives in EPEL.)

## Encrypted overlay firewall (one-time)

Encrypted overlays wrap VXLAN in ESP (protocol 50) and NAT-T (UDP 4500).
Without firewall rules for both, east-west traffic between swarm hosts on
the encrypted overlay times out at the TCP layer with no obvious error.
On every swarm host:

```bash
sudo firewall-cmd --permanent --add-protocol=esp
sudo firewall-cmd --permanent --add-port=4500/udp
sudo firewall-cmd --reload
```

## Logging (optional)

By default every service uses the docker daemon's default log driver
(usually `json-file`). To ship container stdio to a remote syslog
collector — your own rsyslog/syslog-ng box, a Splunk universal forwarder
receiver, a SIEM, etc. — set `swarm_stack_logging`:

```yaml
# inventories/<env>/group_vars/<group>/main.yml
swarm_stack_logging:
  driver_name: syslog
  options:
    syslog-address: "tcp://syslog.example.com:514"
    syslog-facility: "local0"
    syslog-format: "rfc5424"
    tag: "{% raw %}{{.Name}}/{{.ID}}{% endraw %}"
    mode: "non-blocking"
    max-buffer-size: "25m"
```

Every service in every wrapper that uses `swarm_stack` inherits this.
Override per-service by adding a `logging:` field on a
`swarm_stack_services` entry — same shape — which beats the stack-wide
default. To restore the docker default for a single service while
keeping a stack-wide setting, pass
`logging: { driver_name: json-file, options: {} }` explicitly.

### Don't omit `mode: non-blocking`

In the **default blocking mode**, docker calls `write(2)` on the log
driver's pipe and waits for it to return. If your syslog endpoint goes
down or gets slow, the container's stdout fills, the write blocks, and
your app stalls — sometimes invisibly, since it's happening in the
runtime layer below your code. `non-blocking` mode buffers up to
`max-buffer-size` in memory and drops overflow on the floor. Losing a
few log lines under outage beats losing the service.

The `tag: "{{.Name}}/{{.ID}}"` template is rendered by docker (NOT
Ansible) at container-start time — that's what the `{% raw %}` escaping
in the YAML is for, so Ansible passes the literal Go template string
through.

### Other drivers

The role passes the dict straight to
`community.docker.docker_swarm_service`, so any driver docker knows about
works: `journald`, `gelf`, `fluentd`, `awslogs`, `splunk`, `gcplogs`,
etc. The options dict's keys are exactly what
`docker run --log-opt key=value` expects for that driver.

## Adding a new app

1. Copy `roles/mattermost_swarm/` to `roles/<svc>_swarm/`.
2. Edit `defaults/main.yml` for the new app's tunables (NFS paths, image
   URLs, replica counts, port, vault path placeholders).
3. Edit `tasks/main.yml` — change the `swarm_stack_*` block:
   - `swarm_stack_networks` referencing
     `swarm_overlay_subnets[<stack_name>]`.
   - `swarm_stack_volumes` for NFS subpaths.
   - `swarm_stack_secrets` referencing your vault variables.
   - `swarm_stack_services` with one entry per swarm service —
     `image`, `networks`, `mounts`, `publish`, `secrets`, `placement`,
     etc.
4. Add per-service env templates under `templates/<service>.env.yml.j2`
   for any service whose env needs late-bound secret/config values.
5. Add a playbook in `playbooks/` that invokes the new wrapper role.
6. Add `<stack_name>: "<subnet>"` to `swarm_overlay_subnets` in
   `inventories/swarm/group_vars/swarm_bootstrap/main.yml`.
7. Add the app's secret variables to your encrypted vault file.

## Variables

See `defaults/main.yml` for the full annotated list of `swarm_stack_*`
variables and the resilience contract callers must respect.
