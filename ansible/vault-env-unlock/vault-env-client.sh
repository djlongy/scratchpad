#!/bin/sh
# Returns the Ansible Vault password from the VAULT_PASSWORD environment variable.
# If unset, prompts interactively for the password.
# Used by ansible.cfg vault_password_file directive.
if [ -z "$VAULT_PASSWORD" ]; then
  read -rsp 'Vault Password: ' VAULT_PASSWORD </dev/tty
  echo >&2
fi
echo "$VAULT_PASSWORD"
