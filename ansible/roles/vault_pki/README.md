# vault_pki

## TL;DR

Manages a HashiCorp Vault PKI secrets engine on an existing cluster: the mount
and its tuning, the issuing roles, and the intermediate issuer lifecycle
(generate a CSR inside Vault, hand it to an external signer, `set-signed`
import the chain back in). The intermediate's private key never leaves Vault.

```bash
ansible-playbook -i inventories/mgt/hosts.yml playbooks/25_plat_pki.yml
```

## Requirements

Install collections before running (repo `requirements.yml`, or ad-hoc):

    ansible-galaxy collection install -r requirements.yml

| Collection | When | Used for |
|---|---|---|
| `community.hashi_vault` (>= 6.1) | always | `vault_read` / `vault_write` against the PKI API |

## Key variables

Full list: `defaults/main.yml`. Contract: `meta/argument_specs.yml`.

**Required** = value must be correct for a successful run (defaults often work).
**Optional** = safe to leave default / empty; phase stays off or uses built-ins.
**When X** = required only if that feature is on.

| Req | Variable | Default | Purpose |
|---|---|---|---|
| **Required** | `vault_pki_addr` | `""` | Vault API address, e.g. `https://vault.example.com:8200` |
| Optional | `vault_pki_token` / `vault_pki_token_file` | `""` / `~/.vault-token` | Declared token wins; else the `vault login` token file |
| Optional | `vault_pki_mount` | `pki` | Secrets-engine mount path |
| Optional | `vault_pki_max_lease_ttl_hours` | `87600` (10y) | Mount-wide TTL ceiling, tuned on drift only |
| When csr | `vault_pki_intermediate_common_name` / `_organization` / `_country` | `""` | Intermediate subject; country is optional |
| Optional | `vault_pki_intermediate_key_type` / `_key_bits` | `rsa` / `4096` | Key parameters for the one-time generation |
| When csr | `vault_pki_csr_path` | `""` | Control-node path the generated CSR is written to |
| When set_signed | `vault_pki_chain_path` | `""` | Control-node path of the signed chain to import |
| When sign_external | `vault_pki_sign_external_csr_path` | `""` | Control-node path of the external CSR to sign |
| When sign_external | `vault_pki_sign_external_cert_out` / `_chain_out` | `""` | Control-node paths the signed cert/chain are written to |
| Optional | `vault_pki_sign_external_ttl` | `87600h` | TTL requested for a signed child; Vault caps it at the issuer's remaining validity |
| Optional | `vault_pki_roles` | `[]` | Issuing roles `[{name, config}]`; `config` is a full-replace write to the Vault role API |

## Minimum configuration

```yaml
# group_vars/vault_pki_hosts.yml
---
# Required
vault_pki_addr: "https://service.example.internal"
```

## Usage

```yaml
- name: Converge the estate PKI engine
  hosts: localhost
  gather_facts: false
  roles:
    - role: vault_pki
```

Run:

```bash
ansible-playbook -i inventories/mgt/hosts.yml playbooks/25_plat_pki.yml
```

## Preconditions

- A Vault token already exists at `vault_pki_token_file` (default
  `~/.vault-token`, from `vault login`) unless `vault_pki_token` is declared
  directly.
- First-time `sys/mounts` writes (enable, tune) need more than a read-only
  token; on an already-mounted, already-tuned engine both writes are
  skipped, so a normal converge succeeds with a read-only token.

## Behaviour

- `<mount>/intermediate/generate/internal` mints a new private key on every
  call, so the CSR-generation and signed-chain-import phases are both gated
  on an issuer probe (`GET <mount>/issuers?list=true`): an existing issuer
  skips generation/import outright, a 404 lets the lifecycle proceed, and
  any other status (403, 5xx) fails the play rather than risk misreading an
  error as "no issuer" and re-keying the mount.
- Re-keying is a deliberate manual operation (delete the mount's
  issuers/keys first), never a converge side effect.
- Signing an external CSR runs only when a caller composes it via
  `tasks_from`, handing it an external CSR (the mount acts as signer, not
  signee) — it is not part of a normal converge.
- Every task touching the token value is `no_log`.
