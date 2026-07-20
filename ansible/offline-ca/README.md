# Offline CA

Port of the estate offline root + issuing CA toolkit. **No estate-specific
secrets** — uses system `openssl` only.

## Script

```bash
# From ansible/ (or any dir; state defaults to ./offline-ca)
export CA_PASSPHRASE='…'   # or prompt
../scripts/offline-ca.sh setup
../scripts/offline-ca.sh help
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
