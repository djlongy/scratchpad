#!/bin/sh
# Returns the Ansible Vault password from the VAULT_PASSWORD environment variable.
# Used by ansible.cfg vault_password_file directive.
if [ -z "$VAULT_PASSWORD" ]; then
  echo "ERROR: VAULT_PASSWORD environment variable is not set" >&2
  exit 1
fi
echo "$VAULT_PASSWORD"
