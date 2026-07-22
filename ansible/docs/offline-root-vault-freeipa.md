# Offline root CA → Vault signs everything → FreeIPA in one command

Copy-paste path from nothing to a FreeIPA server whose certificate chain anchors
to your own offline root, with HashiCorp Vault doing all day-to-day signing.

Total hands-on time: ~15 minutes (plus the FreeIPA install itself, ~25 minutes
unattended).

## What you end up with

```
root CA        offline, ~25y — lives ONLY as an ansible-vault-encrypted bundle in git
  └─ issuing CA   ~10y — imported into Vault's PKI mount; signs everything online
       └─ FreeIPA CA   signed by Vault; issues host/service certs for the realm
```

Rebuild-from-nothing kit = a clone of this repo + your ansible-vault password +
the CA passphrase. Nothing else.

## Step 1 — create the CAs (once per ~10 years, ~2 minutes)

```bash
cd ansible
./scripts/offline-ca.sh setup \
  --root-subject    "CN=Example Root CA R1,O=Example,C=AU" \
  --issuing-subject "CN=Example Vault Issuing CA R1,O=Example,C=AU"
```

It prompts once to create the CA passphrase (one passphrase protects both keys)
and finishes by printing the exact escrow commands. Follow them — short version:

1. `ansible-vault encrypt offline-ca/bundle/offline_ca.yml`
2. Move it into your inventory's `group_vars/all/` (or paste its contents into
   an existing encrypted vars file — only the variable names matter)
3. Commit. Store the CA passphrase in your password manager + one offline copy.

The `offline-ca/` work directory ships its own `.gitignore` (`*`), so keys can
never be committed by accident. After the escrow is committed and verified you
can delete the directory — the encrypted bundle is the system of record.

## Step 2 — put the issuing CA into Vault (once per Vault build, ~1 minute)

Option A — automated, if Vault is deployed with the `hashicorp_vault_container`
role. In inventory:

```yaml
hashicorp_vault_pki_enabled: true
hashicorp_vault_pki_issuer_import: true
hashicorp_vault_pki_issuer_cert: "{{ pki_issuing_ca_cert }}"
hashicorp_vault_pki_issuer_key: "{{ pki_issuing_ca_key }}"
hashicorp_vault_pki_root_cert: "{{ pki_root_ca_cert }}"
```

then run the playbook with `--tags pki,pki_issuer`. Idempotent — the second run
reports `changed=0`. The phase self-verifies by certificate fingerprint.

Option B — any existing Vault, by hand:

```bash
vault secrets enable pki && vault secrets tune -max-lease-ttl=87600h pki/
cat issuing.crt issuing.key | vault write pki/issuers/import/bundle pem_bundle=-
cat root.crt              | vault write pki/issuers/import/bundle pem_bundle=-   # cert only, for full chains
```

(`issuing.crt`/`issuing.key`/`root.crt` = the corresponding fields from the
escrow bundle; each field's comment says exactly this.)

## Step 3 — deploy FreeIPA (one command per server)

Prerequisites, both one-liners:

1. Inventory points at Vault: `vault_pki_addr: "https://vault.example.com:8200"`
   (token comes from `~/.vault-token`; it needs write on
   `pki/root/sign-intermediate`).
2. The encrypted escrow bundle (`offline_ca.yml` from Step 1) sits in the
   inventory's `group_vars/all/` — phase 2 installs the trust anchor straight
   from its `pki_root_ca_cert` var. No root.crt file to write out.

Then:

```bash
scripts/freeipa-signed-install.sh <host>
```

Done. One command runs the whole thing:

1. Phase 1 — FreeIPA installs up to its external-CA CSR; the CSR lands on the
   control node
2. Vault signs it (`sign-intermediate`) and the chain is assembled + verified
3. Phase 2 — signed cert goes back, the install completes, the root cert is
   pushed into the estate trust stores

## Why the wrapper runs two ansible processes (read only if curious)

`ipa-server-install --external-ca` exits after emitting its CSR and must be
re-invoked to finish — and the `ansible_freeipa` collection detects "step two"
only across separate ansible-playbook processes (verified against
ansible_freeipa 1.16.0: re-entering the role in one process regenerates the CSR
instead of importing the signed cert). The wrapper hides that seam; both phases
stay hand-runnable with `-e freeipa_signed_install_phase=1` / `=2` if you ever
need them individually.

## When something fails

- `cannot unlock … wrong CA passphrase` — it wants the passphrase set during
  `setup`, not a new one. One passphrase protects both CA keys.
- Vault sign fails with 403 — your token lacks `create`/`update` on
  `pki/root/sign-intermediate`.
- Phase 2 says the chain does not verify — confirm Step 2 imported BOTH bundles
  (issuing cert+key AND the root cert); `curl $VAULT_ADDR/v1/pki/ca_chain`
  should return two certificates, issuing CA first.
- Phase 1 fails at `openssl verify` with `asn1 … wrong tag` / `no certificate
  found` — a multi-PEM chain file was corrupted. The shipped
  `vault_pki` `sign_external` phase joins PEMs via shell with forced newlines
  (never Jinja `~ '\n' ~` under YAML `>-`, never bare `cat a b`). Confirm the
  chain file has no literal backslash-n and no glued
  `-----END CERTIFICATE----------BEGIN CERTIFICATE-----` junctions.
- FreeIPA rejects the signed CA subject — Vault must sign with
  `use_csr_values: true` so `O=<REALM>` is preserved (the role already does this).
- A second run of the wrapper on a converged server re-signs nothing: the
  replay guard sees the existing cert and skips `sign-intermediate`.

## Inventory snippets (example env)

Sanitised copies live under `inventories/example/group_vars/all/`:

- `vault_pki.yml` — `vault_pki_addr` / mount
- `offline_ca_import.yml.example` — map escrow vars onto `pki_issuer` role vars

Copy the example import file, place your **encrypted** `offline_ca.yml` beside
it, enable `hashicorp_vault_pki_issuer_import`, and run `--tags pki,pki_issuer`.
Never commit a plaintext escrow.

Next step if you are starting fresh: run Step 1 now — it is two minutes and
everything else hangs off it.
