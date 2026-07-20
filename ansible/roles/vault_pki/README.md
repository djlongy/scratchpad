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
| `sign_external` | Replay-guarded sign: an existing cert that carries the CSR's key and chains to the mount CA is re-used (`changed=false`); otherwise one `sign-intermediate` call. Writes cert + chain (leaf, issuing CA, root) and self-verifies with `openssl verify` before declaring success. |

Inventory needs `vault_pki_addr` and (usually) `vault_pki_mount`. The mount must
already hold the issuing CA — see the `hashicorp_vault_container` role's
`pki_issuer` phase, which imports it from the offline-CA escrow bundle.
