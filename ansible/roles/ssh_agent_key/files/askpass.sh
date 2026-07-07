#!/bin/sh
# ssh-add has no terminal under Ansible, so it runs the program named in
# $SSH_ASKPASS to obtain the key's passphrase. This helper echoes the variable
# the unlock task places in ssh-add's process environment — the script itself
# contains no secret and never changes.
#
# ssh-add passes the prompt text as $1 and re-asks forever on a wrong
# passphrase ("Bad passphrase, try again ..."). We only ever have one answer,
# so refuse the retry prompt — ssh-add then aborts instead of looping.
case "$1" in *again*) exit 1 ;; esac
printf '%s\n' "$SSH_AGENT_KEY_PASSPHRASE"
