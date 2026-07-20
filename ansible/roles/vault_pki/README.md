# vault_pki — Vault as the online CA signer

Signs an external subordinate-CA CSR (e.g. FreeIPA's `external-ca` CSR) with the
issuing CA held inside a HashiCorp Vault PKI mount, via
`<mount>/root/sign-intermediate`. The signing key never leaves Vault.

No `tasks/main.yml` — invoke phases explicitly:

```yaml
- ansible.builtin.include_role:
    name: vault_pki
    tasks_from: sign_external
    apply: { delegate_to: localhost, run_once: true }
  vars:
    vault_pki_sign_external_csr_path: "/path/to/ipa.csr"
    vault_pki_sign_external_cert_out: "/path/to/ipa-ca.crt"
    vault_pki_sign_external_chain_out: "/path/to/ipa-chain.crt"
```

| Phase | What it does |
|---|---|
| `resolve_token` | Vault token from `vault_pki_token` or `~/.vault-token`; cached, `no_log` |
| `sign_external` | Replay-guarded sign: an existing cert that carries the CSR's key and chains to the mount CA is re-used (`changed=false`); otherwise one `sign-intermediate` call with `use_csr_values: true`. Writes cert + chain (leaf, issuing CA, root) and self-verifies with `openssl verify` before declaring success. |

## Inventory

```yaml
vault_pki_addr: "https://vault.example.com:8200"
vault_pki_mount: "pki"          # default
# vault_pki_sign_external_ttl: "87600h"   # optional; default 10y
```

The mount must already hold the issuing CA — see the `hashicorp_vault_container`
role's `pki_issuer` phase, which imports it from the offline-CA escrow bundle
(`docs/offline-root-vault-freeipa.md`).

## FreeIPA wiring

`playbooks/freeipa_signed_install.yml` selects this signer with:

```bash
scripts/freeipa-signed-install.sh <host> -e freeipa_signed_install_signer=vault
```

Phase 2 still distributes the **public root** from
`<certificate_authority_pki_dir>/root/root.crt` into host trust stores — plant the
offline root cert there once after the ceremony.

## PEM safety (load-bearing)

Do **not** join multi-cert PEM bodies with Jinja under YAML `>-` (`~ '\n' ~` can
emit a *literal* backslash-n). Do **not** use bare `cat a b` when `a` may lack a
trailing newline (glues `END CERTIFICATE` to `BEGIN CERTIFICATE`). This role uses
a small shell block with forced newlines; keep that pattern if you fork it.

`use_csr_values: true` is required for FreeIPA: without it Vault issues a CN-only
subject and drops `O=<REALM>`, which the external-ca install expects.
