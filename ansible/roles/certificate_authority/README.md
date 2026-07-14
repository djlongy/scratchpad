# certificate_authority

Portable, file-based internal Certificate Authority built on **`community.crypto`**
only — no CA daemon, no HashiCorp Vault PKI engine. The system of record is a plain
X.509 PEM tree under `pki/`; Ansible is a replaceable executor and every operation
has a bare-`openssl` break-glass equivalent (see [Break-glass](#break-glass-openssl-only)).

## Hierarchy

```
<org> Root CA                 pki/root/          self-signed, ~20 yr
├── <env> FreeIPA CA          pki/ipa/           CA cert signed DIRECTLY by root (~10 yr)
└── <env> TLS Issuing CA      pki/intermediates/ pathlen:0, name-constrained, ~5 yr
      └── *.<env>.<domain>     pki/wildcards/     leaf, 397 d
```

FreeIPA CAs chain off the **root** (their external-ca CSR requests `CA:TRUE`, which a
`pathlen:0` intermediate is forbidden to sign). TLS wildcards chain off the
name-constrained intermediates.

## Tags / phases (`tasks/main.yml` imports each; no nested roles)

| Tag | Phase | Runs where |
|---|---|---|
| `root` | Self-signed root key (encrypted PKCS#8) + cert | control node |
| `intermediate` | Per-env `pathlen:0`, name-constrained CA, root-signed | control node |
| `wildcard` | Per-env wildcard leaf, issue/renew inside its window | control node |
| `crl` | Root + intermediate CRL files | control node |
| `sign_ipa` (`never`) | Sign a fetched FreeIPA CSR as a CA cert off the root | control node |
| `distribute` (`never`) | Push root anchor into a host's OS trust store | target host |

Generation/signing phases run `delegate_to: localhost`, `become: false`,
`run_once: true` so the role composes into localhost plays and remote plays alike.
`distribute` runs on the play's host and self-escalates `become: true` per task.
`sign_ipa` and `distribute` are `never`-tagged (opt-in only).

Every signing phase appends one line to `pki/issued.log`
(`date|issuer|subject|serial|not_after`).

## Key custody

Every `.key` under `pki/` is a **passphrase-encrypted PKCS#8 PEM**
(`ENCRYPTED PRIVATE KEY` header) — openssl-readable directly. Only the passphrases
are secrets, resolved declared-var-first: an Ansible-Vault-provided variable if set,
else a HashiCorp Vault fallback (`certificate_authority_vault_secret`), else a
fail-fast assert. Every task touching a passphrase or key content is `no_log: true`.
This is encrypted-at-rest, not air-gapped: anyone who can run the playbook can sign.

## Bring-your-own CA key (in-memory)

For the "key lives in a secret store, used at runtime in memory, nothing persisted to
disk" model, provide the CA key + cert as PEM **content** instead of letting the role
generate a `pki/` tree:

```yaml
certificate_authority_root_key_content:  "{{ vault_ca_root_key }}"   # from a vaulted var
certificate_authority_root_cert_content: "{{ vault_ca_root_cert }}"
# certificate_authority_root_key_passphrase: only if YOUR key is itself encrypted
```

When `_root_key_content` is set, root generation is **skipped** and `sign_ipa` signs
off that key **in memory** (`ownca_privatekey_content`) — the private key is never
written to disk; only the public signed cert + chain are. The key may be
**passwordless**, so the secret store (e.g. Ansible Vault) is the single at-rest layer
with no redundant double encryption. Generate + store it yourself, then wire the vars:

```bash
openssl req -x509 -newkey rsa:4096 -keyout root.key -out root.crt -days 7300 -nodes \
  -subj "/CN=Org Root CA"
# store root.key + root.crt in your secret store; wire the two _content vars from them
ansible-playbook ca.yml --tags sign_ipa \
  -e certificate_authority_ipa_csr_path=/path/to/ipa.csr \
  -e certificate_authority_ipa_name=zonea
```

This mode currently covers signing off the root (`sign_ipa`) + `distribute`; declaring
intermediates/wildcards uses file mode (a fail-fast guard enforces this).

## Variables

Full contract in `meta/argument_specs.yml`; defaults and item shapes in
`defaults/main.yml`. Environment values (real common names, domains, name
constraints) belong in inventory `group_vars`, never in the role. Illustrative
placeholder data:

```yaml
certificate_authority_root: { common_name: "Example Root CA", days: 7300 }
certificate_authority_intermediates:
  - { name: zonea, common_name: "Example Zone A TLS Issuing CA", name_constraints: [".zonea.example.com"], days: 1826 }
certificate_authority_wildcards:
  - { name: zonea, common_name: "*.zonea.example.com", issuer: zonea, days: 397 }
```

## Usage

A minimal playbook that generates/refreshes the whole hierarchy on the control node:

```yaml
# ca.yml
- hosts: localhost
  gather_facts: false
  roles:
    - certificate_authority
```

```bash
# Generate/refresh the hierarchy (idempotent; a clean re-run reports changed=0)
ansible-playbook ca.yml

# Sign a fetched FreeIPA CSR off the root (opt-in)
ansible-playbook ca.yml --tags sign_ipa \
  -e certificate_authority_ipa_csr_path=/path/to/ipa.csr \
  -e certificate_authority_ipa_name=zonea

# Trust the root on a host group (opt-in) — run against remote hosts
ansible-playbook -i inventory trust.yml --tags distribute
```

## Break-glass (openssl only)

The role writes nothing you cannot inspect or reproduce with `openssl` alone — the
point is that you can operate or abandon the CA with no Ansible at all.

```bash
# Inspect the hierarchy
openssl x509 -in pki/root/root.crt -noout -text
openssl x509 -in pki/intermediates/<name>.crt -noout -text | grep -A2 'Name Constraints'

# Verify a chain
openssl verify -CAfile pki/root/root.crt pki/intermediates/<name>.crt
openssl verify -CAfile pki/root/root.crt -untrusted pki/intermediates/<name>.crt \
  pki/wildcards/<name>.crt

# Decrypt a key (you are prompted for the passphrase)
openssl pkey -in pki/root/root.key

# Sign a FreeIPA CA CSR off the root by hand (what `sign_ipa` automates)
openssl x509 -req -in ipa.csr -CA pki/root/root.crt -CAkey pki/root/root.key \
  -CAcreateserial -days 3650 -extfile <(printf 'basicConstraints=critical,CA:TRUE\nkeyUsage=critical,keyCertSign,cRLSign\n') \
  -out pki/ipa/<name>-ipa-ca.crt
cat pki/ipa/<name>-ipa-ca.crt pki/root/root.crt > pki/ipa/<name>-chain.crt
```

Commit the public `.crt`/`.csr`/`.crl`/log in the clear; keep every `.key`
passphrase-encrypted. A CI/unit guard that fails on any unencrypted key under `pki/`
is recommended.

## Dependencies

`community.crypto` (always), and `community.hashi_vault` **only** if the Vault
passphrase fallback is used. No new collections/pip; the role never shells out to
`openssl` (it uses the `community.crypto` modules).
