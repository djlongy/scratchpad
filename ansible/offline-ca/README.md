# Offline CA

Offline root + issuing CA toolkit — bash + system `openssl` only, no other
dependencies. End-to-end walkthrough: `docs/offline-root-vault-freeipa.md`.

## Script

```bash
# From ansible/ (or any dir; state defaults to ./offline-ca)
export CA_PASSPHRASE='…'   # or prompt
./scripts/offline-ca.sh setup \
  --root-subject    "CN=Example Root CA R1,O=Example,C=AU" \
  --issuing-subject "CN=Example Vault Issuing CA R1,O=Example,C=AU"
./scripts/offline-ca.sh help
```

State directory: `CA_DIR` (default `./offline-ca`).

## Bundle template

`bundle/offline_ca.yml` — optional Ansible Vault escrow template for the
passphrases / PEMs after a ceremony. Fill locally; never commit secrets.

## Typical flow

1. `setup` — generate root + issuing intermediate offline
2. Escrow PEMs + passphrases into Ansible Vault / HashiCorp Vault
3. Import issuing CA into Vault PKI (or FreeIPA external-ca)
4. Day-2 leaf certs from Vault / FreeIPA only
