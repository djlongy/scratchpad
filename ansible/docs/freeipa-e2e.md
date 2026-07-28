# FreeIPA e2e (portable)

Walk-away pipeline: offline CA → Vault PKI issuer → guest prep → signed FreeIPA install.

## Package surface

| Piece | Path |
|-------|------|
| Orchestrator | `scripts/freeipa-e2e.sh` |
| Phase 1+2 wrapper | `scripts/freeipa-signed-install.sh` |
| Offline CA ceremony | `scripts/offline-ca.sh` |
| Prep (VM+disk+baseline) | `playbooks/freeipa_prep.yml` |
| Import issuer into Vault | `playbooks/vault_pki_issuer.yml` |
| Signed install | `playbooks/freeipa_signed_install.yml` |
| Converge (full role) | `playbooks/freeipa.yml` |
| Example inventory | `inventories/example/` |

## Stages

| Stage | What runs | Skip |
|-------|-----------|------|
| `preflight` | checks | never |
| `vault` | `PLAYBOOK_VAULT` | `SKIP_VAULT=1` if Vault already healthy |
| `pki` | `PLAYBOOK_PKI` `--tags pki,pki_issuer` | `SKIP_PKI=1` if issuer already imported |
| `prep` | `PLAYBOOK_PREP` | `SKIP_PREP=1` if host exists |
| `sign` | phase1 → phase2 signed install | — |
| `converge` | `PLAYBOOK_CONVERGE` | `SKIP_CONVERGE=1` for install-only |
| `dns` | `PLAYBOOK_DNS` if set | default skip (`SKIP_DNS=1`) |

Resume: `--from sign` (or any stage name). State: `~/.cache/freeipa-e2e/`.

## User config (inventory only)

1. Enable `freeipa` (+ `vault`) hosts in inventory.
2. Set FreeIPA vars (`group_vars/freeipa`, `host_vars/<idm>`):
   - `freeipa_server_domain` / `realm` / `fqdn`
   - admin + DM passwords (declared or `freeipa_server_vault_secret`)
   - DNS forwarders
3. Set signer vars (`vault_pki_addr`, mount, token source).
4. Escrow offline CA → map `pki_*` onto `hashicorp_vault_pki_issuer_*` (see `offline_ca_import.yml.example`).

## Run

```bash
cd ansible
export ANSIBLE_VAULT_PASSWORD=...   # if needed

# Full path (Vault seed + prep + sign + converge)
INVENTORY=inventories/lab/hosts.yml IPA_HOST=idm-01 VAULT_HOST=vault-01 \
  VAULT_TOKEN_FILE=/path/to/root.token \
  ./scripts/freeipa-e2e.sh

# Host already exists; Vault issuer already imported
SKIP_VAULT=1 SKIP_PKI=1 SKIP_PREP=1 \
  INVENTORY=... IPA_HOST=idm-01 ./scripts/freeipa-e2e.sh --from sign

# Override playbooks (estate that uses different paths)
PLAYBOOK_SIGN=playbooks/20_iam_freeipa.yml \
PLAYBOOK_PREP=playbooks/freeipa.yml \
  INVENTORY=... IPA_HOST=... ./scripts/freeipa-e2e.sh
```

## Bare bones

- **Always:** `freeipa_server` + `vault_pki` + `freeipa_signed_install.yml` + inventory.
- **Optional stages:** Vault seed, pki issuer, guest/storage/baseline prep, DNS.
- **No estate names** in the script — only env vars and shipped defaults.
