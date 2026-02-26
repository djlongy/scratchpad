# action_plugins/get_cli_args.py
from ansible.plugins.action import ActionBase
import sys
import os
import json

class ActionModule(ActionBase):
    def run(self, tmp=None, task_vars=None):
        result = super(ActionModule, self).run(tmp, task_vars)
        argv = sys.argv

        # Strip full path from first argument
        if argv and '/' in argv[0]:
            argv[0] = os.path.basename(argv[0])

        # Extract semaphore_vars and other internal extra-vars
        semaphore_data = {}
        cleaned_argv = []
        skip_next = False

        for i, arg in enumerate(argv):
            if skip_next:
                skip_next = False
                continue

            # Handle --extra-vars or -e with JSON dict (internal Ansible format)
            if arg in ['--extra-vars', '-e'] and i + 1 < len(argv):
                next_arg = argv[i + 1]
                # Check if it's a JSON dict structure (internal Ansible format)
                if next_arg.startswith('{'):
                    try:
                        # Parse the JSON to extract semaphore_vars
                        extra_vars_dict = json.loads(next_arg)
                        if 'semaphore_vars' in extra_vars_dict:
                            semaphore_data = extra_vars_dict['semaphore_vars']
                    except json.JSONDecodeError:
                        pass
                    skip_next = True
                    continue
                else:
                    # Keep file-based extra-vars like -e@file.yml
                    cleaned_argv.append(arg)
                    cleaned_argv.append(next_arg)
                    skip_next = True
            # Handle -ekey=value format
            elif arg.startswith('-e') and '=' in arg and not arg.startswith('-e@'):
                cleaned_argv.append(arg)
            # Handle -e@file format
            elif arg.startswith('-e@'):
                cleaned_argv.append(arg)
            else:
                cleaned_argv.append(arg)

        result.update({
            "changed": False,
            "ansible_playbook_argv": cleaned_argv,
            "ansible_playbook_cmd": " ".join(cleaned_argv),
            "semaphore_vars": semaphore_data if semaphore_data else None,
        })
        return result