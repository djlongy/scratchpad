# certificate_authority

## TL;DR

File-based internal Certificate Authority built on `community.crypto` only — no CA
daemon, no HashiCorp Vault PKI engine. The system of record is a plain X.509 PEM tree
on the control node; every operation has a bare `openssl` break-glass equivalent.

```bash
ansible-playbook -i inventories/<env>/hosts.yml playbooks/<playbook>.yml
```

## Requirements

Install collections before running (repo `requirements.yml`, or ad-hoc):

    ansible-galaxy collection install -r requirements.yml

| Collection | When | Used for |
|---|---|---|
| `community.crypto` | always | key/CSR/cert generation, signing, inspection (`x509_certificate`, `openssl_privatekey`, …) |
| `community.hashi_vault` | When Vault fallback | `hashi_vault` lookup for a tier passphrase when the declared var is empty |

## Key variables

Full list: `defaults/main.yml`. Contract: `meta/argument_specs.yml`.

**Required** = value must be correct for a successful run (defaults often work).
**Optional** = safe to leave default / empty; phase stays off or uses built-ins.
**When X** = required only if that feature is on.

| Req | Variable | Default | Purpose |
|---|---|---|---|
| **Required** | `certificate_authority_root.common_name` | `"Example Root CA"` | Root CA subject — must be a real, non-placeholder CN |
| **Required** | `certificate_authority_root_key_passphrase` | `""` | Root key passphrase — this or the Vault fallback below must resolve one |
| Optional | `certificate_authority_pki_dir` | `{{ playbook_dir }}/../pki` | On-disk PEM tree root |
| Optional | `certificate_authority_intermediates` | `[]` | Per-env pathlen:0 intermediates — empty means a root-only estate |
| Optional | `certificate_authority_wildcards` | `[]` | Per-env wildcard leaf certs |
| Optional | `certificate_authority_renew_within_days` | `30` | Re-issue a wildcard leaf inside this window |
| Optional | `certificate_authority_allow_identity_change` | `false` | Bypass the identity/coherence guards — only to deliberately rename a CA or repair a cert/key mismatch |
| Optional | `certificate_authority_root_key_content` / `_root_cert_content` | `""` | Bring-your-own root key/cert as PEM content — root generation is skipped and signing happens in memory |
| When `sign_ipa` | `certificate_authority_ipa_csr_path` / `_ipa_name` | `""` | Control-node path of the fetched FreeIPA CSR + output stem |
| When `crl` | `certificate_authority_revocations` | `[]` | Declarative revocation list fed into the CRL |
| When `crl` | `certificate_authority_crl_days` | `30` | CRL `nextUpdate` horizon |
| When `distribute` | `certificate_authority_trust_anchor_dir` | `/etc/pki/ca-trust/source/anchors` | Target-host directory the root anchor is dropped into |
| Optional | `certificate_authority_vault_secret` | `""` | Vault KV path holding the passphrases (fallback when the declared vars above are empty) |

## Minimum configuration

```yaml
# group_vars/certificate_authority_hosts.yml
---
# Required
certificate_authority_root:
  common_name: "Example Root CA"     # must equal the existing anchor's CN (guard 2)
  days: 7300
certificate_authority_root_key_passphrase: "{{ vault_secret_root_key_passphrase }}"
```

Declare `certificate_authority_root` whole — an override replaces the default dict
rather than merging into it, and `days` is read without a fallback.

## Usage

```yaml
- hosts: localhost
  gather_facts: false
  roles:
    - role: certificate_authority
```

Run it:

```bash
# Generate/refresh root + any declared tiers (idempotent; root-only by default)
ansible-playbook -i inventories/<env>/hosts.yml playbooks/<playbook>.yml

# Sign a fetched FreeIPA CSR off the root (opt-in)
ansible-playbook playbooks/<playbook>.yml --tags sign_ipa \
  -e certificate_authority_ipa_csr_path=/path/to/ipa.csr -e certificate_authority_ipa_name=mgt

# Trust the root on a host group (opt-in)
ansible-playbook -i inventories/mgt/hosts.yml <play-targeting-hosts> --tags distribute
```

## Preconditions

- `sign_ipa`: `certificate_authority_ipa_csr_path` must already exist on the control
  node — this role does not fetch the FreeIPA CSR itself.
- When a tier passphrase var is left empty, a secret must already exist at
  `certificate_authority_vault_secret` in HashiCorp Vault.

## Behaviour

Every `.key` under the PKI tree is a passphrase-encrypted PKCS#8 PEM
(`ENCRYPTED PRIVATE KEY` header) — openssl-readable directly. Only the passphrases are
secrets: declared var first, HashiCorp Vault fallback, then a fail-fast assert. Each
tier resolves its passphrase only when it is declared, so a root-only estate needs only
the root passphrase.

Before any tier re-issues or overwrites existing material, three independent guards
must pass — each fails the run loudly rather than silently clobbering a distributed
anchor:

1. **Passphrase** — a private key is never silently re-keyed; a wrong/changed
   passphrase fails the run.
2. **Identity (CN)** — an existing cert's subject CN must equal the CN this run
   declares.
3. **Coherence (keypair)** — an existing cert and its private key must be the same
   keypair. `community.crypto`'s `x509_certificate` otherwise silently re-issues a cert
   that doesn't match its key, which for the root invalidates every trust store
   carrying the old anchor.

`certificate_authority_allow_identity_change` bypasses guards 2 and 3 together — set it
only to deliberately rename a CA in place or repair a cert/key mismatch. It never
bypasses guard 1 (keys are never re-keyed).

## Expected result

A default (root-only) run produces `pki/root/`. Declaring intermediates/wildcards, and
opting into `sign_ipa`, builds out the rest of the tree:

```
Root CA                pki/root/          self-signed, ~20 yr
├── FreeIPA CA          pki/ipa/           signed DIRECTLY by root (~10 yr)
└── TLS Issuing CA      pki/intermediates/ pathlen:0, name-constrained, ~5 yr
      └── wildcard leaf  pki/wildcards/     397 d
```

FreeIPA CAs chain off the root directly (their CSR requests `CA:TRUE`, which a
`pathlen:0` intermediate is forbidden to sign). TLS wildcards chain off the
name-constrained intermediates. `report` runs last on every untagged run and rebuilds
`pki/inventory.json` from the on-disk certs (public fields only) — check it to confirm
what actually exists.

## Tag safety

`crl`, `sign_ipa`, and `distribute` are tagged `never` — a plain (tagless) run skips
them even though they are otherwise valid phases. Request them explicitly
(`--tags crl` / `--tags sign_ipa` / `--tags distribute`) to regenerate CRLs, sign a
fetched FreeIPA CSR, or push the root anchor into a host's trust store.
