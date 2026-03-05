#!/bin/bash
# Returns the Ansible Vault password from the VAULT_PASSWORD environment variable.
# If unset, prompts interactively for the password.
# Used by ansible.cfg vault_password_file directive.
if [ -z "$VAULT_PASSWORD" ]; then
  read -rsp 'Vault Password: ' VAULT_PASSWORD </dev/tty
  echo >&2
fi

# Only output when executed as a subprocess (e.g. by Ansible via vault_password_file).
# When sourced interactively, this just sets VAULT_PASSWORD in the current shell.
if [[ "${BASH_SOURCE[0]}" == "${0}" ]]; then
  echo "$VAULT_PASSWORD"
fi
