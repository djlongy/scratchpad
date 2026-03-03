# get_cli_args

An Ansible action plugin that exposes the `ansible-playbook` command-line arguments,
Semaphore-injected extra-vars, and the git state of the playbook's working tree to
tasks within a running playbook.

Useful when you need to know *how* and *from what state* a playbook was invoked —
for example, to log the exact command, reconstruct it in a notification, detect
modified files at runtime, or extract variables passed by Semaphore UI.

## Returns

### CLI keys

| Key | Type | Description |
|-----|------|-------------|
| `ansible_playbook_argv` | list | Cleaned CLI argument list (internal JSON extra-vars stripped) |
| `ansible_playbook_cmd` | string | Space-joined version of `ansible_playbook_argv` |
| `semaphore_vars` | dict \| null | Variables from Semaphore's injected `semaphore_vars` JSON block, or `null` if not run via Semaphore |

### `git_status` keys

| Key | Type | Description |
|-----|------|-------------|
| `branch` | string | Current branch name, or `HEAD` if detached |
| `commit` | string | Full commit SHA |
| `commit_short` | string | 7-char short SHA |
| `tag` | string \| null | Nearest tag from `git describe --tags --always --dirty` |
| `modified` | list | Tracked files with unstaged modifications |
| `staged` | list | Files staged in the index |
| `untracked` | list | Untracked files |
| `deleted` | list | Tracked files deleted in the worktree |
| `is_clean` | bool | `true` if working tree and index are clean |
| `error` | string | Set if git is unavailable or directory is not a repo |

The plugin strips internal Ansible JSON `--extra-vars` blobs (e.g. Semaphore's
`-e '{"semaphore_vars": {...}}'`) from `ansible_playbook_argv` while extracting
`semaphore_vars` into its own key. File-based extra-vars (`-e @file.yml`) and
`-ekey=value` flags are preserved.

## Example task

```yaml
- name: Get playbook invocation details
  get_cli_args:
  register: cli

- name: Show how we were called
  debug:
    msg: "Invoked as: {{ cli.ansible_playbook_cmd }}"

- name: Warn if working tree is dirty
  debug:
    msg: "WARNING: modified files at runtime: {{ cli.git_status.modified }}"
  when: not cli.git_status.is_clean

- name: Use Semaphore variables if present
  debug:
    msg: "Semaphore env: {{ cli.semaphore_vars }}"
  when: cli.semaphore_vars is not none
```

## Example output

```yaml
ansible_playbook_argv:
  - ansible-playbook
  - site.yml
  - -e
  - "@extra_vars.yml"
ansible_playbook_cmd: "ansible-playbook site.yml -e @extra_vars.yml"
semaphore_vars:
  semaphore_project_id: 42
  semaphore_task_id: 7
git_status:
  branch: main
  commit: a1b2c3d4e5f6...
  commit_short: a1b2c3d
  tag: v1.0.0-3-ga1b2c3d
  modified:
    - roles/myrole/tasks/main.yml
  staged: []
  untracked:
    - tmp_test.yml
  deleted: []
  is_clean: false
```

## Installation

Copy `get_cli_args.py` into your playbook repo's `action_plugins/` directory:

```bash
cp scratchpad/ansible/action_plugins/get_cli_args/get_cli_args.py \
   your-repo/action_plugins/get_cli_args.py
```

Or add the scratchpad path to `DEFAULT_ACTION_PLUGIN_PATH` in `ansible.cfg`:

```ini
[defaults]
action_plugins = /path/to/scratchpad/ansible/action_plugins/get_cli_args:/usr/share/ansible/plugins/action
```

## Requirements

- Python 3.6+
- Ansible 2.9+
- `git` available on the controller node

## Files

```
get_cli_args/
├── README.md
└── get_cli_args.py
```

## Changelog

### v1.0.1
- Added `git_status` to output: branch, commit, tag, modified/staged/untracked/deleted files, and `is_clean` flag
- Git info is captured from `playbook_dir` at runtime

### v1.0.0
- Initial release: CLI args capture and Semaphore extra-vars extraction
