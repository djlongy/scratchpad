"""Derive the OpenSSH public key line from a private key — in memory only.

This is what lets unlock and lock work independently of each other: instead of
remembering what unlock added, lock recomputes the key's public half from the
same vaulted private key and deletes exactly that from the agent. Uses the
`cryptography` library that ships as an ansible-core dependency, so nothing is
spawned and nothing touches disk.
"""
from __future__ import annotations

from ansible.errors import AnsibleFilterError


def ssh_agent_key_pubkey(private_key_text, passphrase=""):
    """Return the 'ssh-ed25519 AAAA...' public line for a private key string."""
    try:
        from cryptography.hazmat.primitives import serialization
    except ImportError as exc:  # pragma: no cover - controller always has it via ansible-core
        raise AnsibleFilterError(
            "ssh_agent_key_pubkey: the 'cryptography' Python library is required on the controller"
        ) from exc

    if not private_key_text:
        raise AnsibleFilterError("ssh_agent_key_pubkey: the private key text is empty")

    data = private_key_text.encode()
    password = passphrase.encode() if passphrase else None

    key = None
    problems = []
    # OpenSSH-format keys first (the common case), then classic PEM.
    for loader in (serialization.load_ssh_private_key, serialization.load_pem_private_key):
        try:
            key = loader(data, password=password)
            break
        except Exception as exc:  # noqa: BLE001 - collect and report both attempts
            problems.append(str(exc))
    if key is None:
        raise AnsibleFilterError(
            "ssh_agent_key_pubkey: could not read the private key "
            "(wrong/missing passphrase, or unsupported format): " + "; ".join(problems)
        )

    return key.public_key().public_bytes(
        serialization.Encoding.OpenSSH, serialization.PublicFormat.OpenSSH
    ).decode()


class FilterModule(object):
    def filters(self):
        return {"ssh_agent_key_pubkey": ssh_agent_key_pubkey}
