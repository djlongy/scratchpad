# model_archive

## TL;DR

Preserves pinned model snapshots, original documentation and verified file hashes on the remote archive host.

```sh
ansible-playbook -i inventories/<env> playbooks/model_archive.yml --limit <archive-host>
```

## Requirements

None beyond `ansible.builtin`.

## Key variables

Full list: `defaults/main.yml`. Contract: `meta/argument_specs.yml`.

| Req | Variable | Default | Purpose |
|---|---|---|---|
| **Required** | `model_archive_root` | `""` | Existing durable storage mount |
| **Required** | `model_archive_expected_gateway` | `""` | Remote host's permitted internet gateway |
| **Required** | `model_archive_models` | `[]` | Repository, immutable revision and separate local compatibility note |
| Optional | `model_archive_hub_version` | `2.4.0` | Isolated native snapshot client version |

## Minimum configuration

```yaml
# host_vars/<archive-host>.yml
model_archive_root: /srv/model-archive
model_archive_expected_gateway: 192.0.2.1
model_archive_models:
  - repo: owner/model
    revision: 0123456789abcdef0123456789abcdef01234567
    note: "Runtime compatibility has not been tested."
```

## Usage

```yaml
- name: Preserve model snapshots
  hosts: archive_hosts
  roles:
    - role: model_archive
```

## Preconditions

- The remote host has Python 3.10+, pip, venv and direct outbound HTTPS.
- Its archive mount has enough free space for every requested repository.
- Repositories are public. Original READMEs are preserved where available.

## Behaviour

Downloads and tool installation run on the remote host, never the controller. Local connections and an unexpected gateway fail before transfers. Downloads bypass environment HTTP proxies.

Each snapshot keeps upstream paths beneath `huggingface/<owner>/<model>/<revision>/snapshot/`. An upstream README sits beside its model files when available. `archive-metadata/` contains source identity, compatibility notes, upstream file hashes, local SHA256 hashes and a completion record written only after every file verifies. A separate local ABOUT.txt records missing model cards without inventing upstream documentation.

Native Hugging Face downloads resume incomplete transfers. Completed revisions are skipped. Older revisions remain when inventory changes. No snapshot is deleted. Use the saved SHA256SUMS from its sibling snapshot directory for a later integrity audit.

## Out of scope

Model serving, runtime validation, upstream repository history, authentication for private models, share creation and cross-NAS backups.

## Expected result

Every requested revision has an `archive-metadata/VERIFIED.json` record whose repository and revision match inventory.
