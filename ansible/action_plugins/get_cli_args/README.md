# get_cli_args

An Ansible action plugin that exposes the `ansible-playbook` command-line arguments
and Semaphore-injected extra-vars to tasks within a running playbook.

Useful when you need to know *how* a playbook was invoked — for example, to log the
exact command, reconstruct it in a notification, or extract variables passed by
Semaphore UI without hardcoding them.

## Returns

| Key | Type | Description |
|-----|------|-------------|
| `ansible_playbook_argv` | list | Cleaned CLI argument list (internal JSON extra-vars stripped) |
| `ansible_playbook_cmd` | string | Space-joined version of `ansible_playbook_argv` |
| `semaphore_vars` | dict \| null | Variables from Semaphore's injected `semaphore_vars` JSON block, or `null` if not run via Semaphore |

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

## Files

```
get_cli_args/
├── README.md
└── get_cli_args.py
```
