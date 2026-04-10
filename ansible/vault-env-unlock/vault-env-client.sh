#!/bin/bash
# Returns the Ansible Vault password from the ANSIBLE_VAULT environment variable.
# If unset, prompts interactively for the password.
# Used by ansible.cfg vault_password_file directive.
if [ -z "$ANSIBLE_VAULT" ]; then
  read -rsp 'Vault Password: ' ANSIBLE_VAULT </dev/tty
  echo >&2
fi

# Only output when executed as a subprocess (e.g. by Ansible via vault_password_file).
# When sourced interactively, this just sets ANSIBLE_VAULT in the current shell.
if [[ "${BASH_SOURCE[0]}" == "${0}" ]]; then
  echo "$ANSIBLE_VAULT"
fi
