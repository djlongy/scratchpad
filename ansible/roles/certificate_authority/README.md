# certificate_authority

## TL;DR

Portable, file-based internal Certificate Authority built on `community.crypto` only —
no CA daemon, no HashiCorp Vault PKI engine. The system of record is a plain X.509 PEM
tree under `pki/`; Ansible is a replaceable executor and every operation has a bare
`openssl` break-glass equivalent.

```bash
ansible-playbook -i inventories/<env>/hosts.yml playbooks/<playbook>.yml
```

## Requirements

Install collections before running (repo `requirements.yml`, or ad-hoc):

    ansible-galaxy collection install -r requirements.yml

| Collection | When | Used for |
|---|---|---|
| `community.crypto` | always | Key/CSR/cert/CRL generation and signing |
| `community.hashi_vault` | When no declared passphrase is set | Vault fallback lookup for key passphrases |

## Key variables

Full list: `defaults/main.yml`. Contract: `meta/argument_specs.yml`.

**Required** = value must be correct for a successful run (defaults often work).
**Optional** = safe to leave default / empty; phase stays off or uses built-ins.
**When X** = required only if that feature is on.

| Req | Variable | Default | Purpose |
|---|---|---|---|
| **Required** | `certificate_authority_root` | `{common_name: "Example Root CA", days: 7300}` | Root CA definition (`--tags root`) |
| Optional | `certificate_authority_pki_dir` | repo-root `pki/` | On-disk PKI tree location |
| When issuing intermediates | `certificate_authority_intermediates` | `[]` | Per-env `pathlen:0` name-constrained CAs (`--tags intermediate`) |
| When issuing wildcards | `certificate_authority_wildcards` | `[]` | Per-env wildcard leaf certs (`--tags wildcard`) |
| Optional | `certificate_authority_renew_within_days` | `30` | Re-issue a wildcard leaf inside this remaining-validity window |
| When signing a FreeIPA CSR | `certificate_authority_ipa_csr_path` / `_ipa_name` | `""` | CSR path + output stem for `--tags sign_ipa` |
| When no declared passphrase | `certificate_authority_vault_secret` | `""` | HashiCorp Vault KV path holding the key passphrases (fallback) |
| Optional | `certificate_authority_root_key_passphrase` / `_intermediate_` / `_wildcard_` | `""` | Declared-var-first passphrases (an Ansible-Vault group_var, typically) |
| When using a bring-your-own key | `certificate_authority_root_key_content` / `_root_cert_content` | `""` | Root key/cert as PEM content — skips on-disk root generation, signs in memory |
| Optional | `certificate_authority_crl_days` | `30` | CRL `nextUpdate` horizon |
| When distributing trust | `certificate_authority_trust_anchor_dir` | EL default | Target-host directory the root anchor is dropped into (`--tags distribute`) |

## Usage

```yaml
# a playbook that generates/refreshes the whole hierarchy on the control node
- hosts: localhost
  gather_facts: false
  roles:
    - certificate_authority
```

Run it:

```bash
ansible-playbook -i inventories/<env>/hosts.yml playbooks/<playbook>.yml

# Sign a fetched FreeIPA CSR off the root (opt-in)
ansible-playbook -i inventories/<env>/hosts.yml playbooks/<playbook>.yml --tags sign_ipa \
  -e certificate_authority_ipa_csr_path=/path/to/ipa.csr \
  -e certificate_authority_ipa_name=zonea

# Trust the root on a host group (opt-in) — run against remote hosts
ansible-playbook -i inventories/<env>/hosts.yml playbooks/<playbook>.yml --tags distribute
```

## Preconditions

- `--tags sign_ipa` requires a CSR already fetched to
  `certificate_authority_ipa_csr_path` — the role signs it, it does not fetch it.
- HashiCorp Vault passphrase fallback: the secret must already exist at
  `certificate_authority_vault_secret`'s path.

## Behaviour

```
<org> Root CA                 pki/root/          self-signed, ~20 yr
├── <env> FreeIPA CA          pki/ipa/           CA cert signed DIRECTLY by root (~10 yr)
└── <env> TLS Issuing CA      pki/intermediates/ pathlen:0, name-constrained, ~5 yr
      └── *.<env>.<domain>     pki/wildcards/     leaf, 397 d
```

FreeIPA CAs chain off the root, not off an intermediate (their external-ca CSR requests
`CA:TRUE`, which a `pathlen:0` intermediate is forbidden to sign). TLS wildcards chain
off the name-constrained intermediates.

Generation/signing phases run `delegate_to: localhost`, `become: false`,
`run_once: true`, so the role composes into localhost plays and remote plays alike.
`distribute` runs on the play's host and self-escalates `become: true` per task. Every
signing phase appends one line to `pki/issued.log`
(`date|issuer|subject|serial|not_after`).

Every `.key` under `pki/` is a passphrase-encrypted PKCS#8 PEM (`ENCRYPTED PRIVATE
KEY` header) — openssl-readable directly. Only the passphrases are secrets, resolved
declared-var-first: an Ansible-Vault-provided variable if set, else the HashiCorp
Vault fallback, else a fail-fast assert. Every task touching a passphrase or key
content is `no_log: true`. This is encrypted-at-rest, not air-gapped — anyone who can
run the playbook can sign.

For "key lives in a secret store, used at runtime, nothing persisted to disk", supply
the CA key + cert as PEM content instead of letting the role generate a `pki/` tree:

```yaml
certificate_authority_root_key_content: "{{ vault_ca_root_key }}"
certificate_authority_root_cert_content: "{{ vault_ca_root_cert }}"
```

Root generation is skipped and `sign_ipa` signs off that key in memory
(`ownca_privatekey_content`) — the private key is never written to disk, only the
public signed cert + chain. The key may be passwordless, so the secret store is the
single at-rest layer. This mode covers signing off the root (`sign_ipa`) and
`distribute`; declaring intermediates/wildcards uses file mode (a fail-fast guard
enforces this).

## Expected result

Every operation has a bare `openssl` equivalent — no Ansible required to inspect or
verify the hierarchy:

```bash
# Inspect the hierarchy
openssl x509 -in pki/root/root.crt -noout -text

# Verify a chain
openssl verify -CAfile pki/root/root.crt pki/intermediates/<name>.crt
openssl verify -CAfile pki/root/root.crt -untrusted pki/intermediates/<name>.crt pki/wildcards/<name>.crt

# Decrypt a key (you are prompted for the passphrase)
openssl pkey -in pki/root/root.key

# Sign a FreeIPA CA CSR off the root by hand (what sign_ipa automates)
openssl x509 -req -in ipa.csr -CA pki/root/root.crt -CAkey pki/root/root.key \
  -CAcreateserial -days 3650 -extfile <(printf 'basicConstraints=critical,CA:TRUE\nkeyUsage=critical,keyCertSign,cRLSign\n') \
  -out pki/ipa/<name>-ipa-ca.crt
```

Commit the public `.crt`/`.csr`/`.crl`/log in the clear; keep every `.key`
passphrase-encrypted.

## Tag safety

`sign_ipa` and `distribute` are `never`-tagged (opt-in only) — each must be requested
explicitly with `--tags sign_ipa` / `--tags distribute`.
