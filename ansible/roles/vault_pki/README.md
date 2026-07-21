# vault_pki

Signs an external subordinate-CA CSR (e.g. FreeIPA's `external-ca` CSR) with the
issuing CA held inside a HashiCorp Vault PKI mount, via
`<mount>/root/sign-intermediate`. The signing key never leaves Vault.

## TL;DR

No `tasks/main.yml` — invoke the phase explicitly. The mount must already hold
the issuing CA.

```bash
ansible-playbook -i inventories/<env>/hosts.yml playbooks/freeipa_signed_install.yml \
  -e freeipa_signed_install_signer=vault
```

## Requirements

Install collections before running (repo `requirements.yml`, or ad-hoc):

    ansible-galaxy collection install -r requirements.yml

| Collection | When | Used for |
|---|---|---|
| `community.hashi_vault` | always | The `sign-intermediate` call against the PKI mount |

## Key variables

Full list: `defaults/main.yml`. No `meta/argument_specs.yml` — every value is
generic; environment-specific data belongs in inventory `group_vars`.

**Required** = value must be correct for a successful run (defaults often work).
**Optional** = safe to leave default / empty; phase stays off or uses built-ins.
**When X** = required only if that feature is on.

| Req | Variable | Default | Purpose |
|---|---|---|---|
| **Required** | `vault_pki_addr` | `""` | Vault API address, e.g. `https://vault.example.com:8200` |
| When `sign_external` | `vault_pki_sign_external_csr_path` | `""` | The fetched CSR to sign |
| When `sign_external` | `vault_pki_sign_external_cert_out` | `""` | Where the signed cert is written |
| When `sign_external` | `vault_pki_sign_external_chain_out` | `""` | Where the assembled chain (leaf + issuing CA + root) is written |
| Optional | `vault_pki_mount` | `pki` | The PKI mount holding the issuing CA |
| Optional | `vault_pki_token` | `""` | Explicit Vault token; wins over `vault_pki_token_file` |
| Optional | `vault_pki_token_file` | `~/.vault-token` | Token file read when `vault_pki_token` is empty |
| Optional | `vault_pki_sign_external_ttl` | `87600h` (10y) | Requested TTL for the signed child cert; capped by the signer's remaining validity |

## Minimum configuration

```yaml
# group_vars/vault_pki_hosts.yml
---
# Required
vault_pki_addr: "https://service.example.internal"
```

## Usage

```yaml
- ansible.builtin.include_role:
    name: vault_pki
    tasks_from: sign_external
    apply: { delegate_to: localhost, run_once: true }
```

Run it:

```bash
ansible-playbook -i inventories/<env>/hosts.yml playbooks/freeipa_signed_install.yml \
  -e freeipa_signed_install_signer=vault
```

## Behaviour

- `resolve_token` resolves the Vault token once per run (from
  `vault_pki_token` or `vault_pki_token_file`), caches it, and never logs the
  value.
- `sign_external` is idempotent via a replay guard: if a certificate already
  on disk carries the CSR's public key and chains to the mount's CA, the
  phase reports `changed=false` and skips re-signing; otherwise it calls
  `sign-intermediate` once with `use_csr_values: true` — required for
  FreeIPA, since without it Vault issues a CN-only subject and drops
  `O=<REALM>`, which the external-ca install expects.
- After signing, the phase self-verifies the assembled chain with
  `openssl verify` before declaring success.

## Known failure mode

Do not join multi-cert PEM bodies with Jinja under YAML `>-` (`~ '\n' ~` can
emit a *literal* backslash-n). Do not use bare `cat a b` when `a` may lack a
trailing newline (glues `END CERTIFICATE` to `BEGIN CERTIFICATE`). This role
uses a small shell block with forced newlines — keep that pattern if you fork
it.
