# optional/acme_cloudflare — deletable ACME + Cloudflare package

**ACME issuance and Cloudflare DNS-01 are one optional unit.** They ship
together and strip together. Core VCSA cert deploy (REST `files` / `csr`,
optional VECS fallback) does **not** need this package.

## Contents

| Path | Role |
|------|------|
| `issue.yml` | ACME account/key/CSR + order (pass 1) + include DNS-01 |
| `dns01_challenge.yml` | Cloudflare TXT create/wait/complete/cleanup (pass 2) |
| `trust_roots.yml` | Install ISRG / LE R11 into VCSA trusted-root-chains |
| `shell_reference.yml` | Informational `acme.sh` equivalents (`--tags acme_shell`) |
| `files/*.pem` | ISRG Root X1 + Let's Encrypt R11 PEMs |

## Same markers elsewhere (search `OPTIONAL ACME + CLOUDFLARE`)

1. `defaults/main.yml` — all `vcenter_acme_*` / `vcenter_cloudflare_*` / issue gate
2. `tasks/vault_auth.yml` — Cloudflare token resolve
3. `tasks/certs_main.yml` — three `import_tasks` of this package
4. This directory

## Air-gap strip (delete everything ACME/Cloudflare)

1. Leave `vcenter_manage_certs` usable for FreeIPA/files/csr **or** set it false.
2. Delete **this entire directory** `tasks/optional/acme_cloudflare/`.
3. Delete the `>>> BEGIN/END OPTIONAL ACME + CLOUDFLARE` block in
   `defaults/main.yml`.
4. Delete the matching block in `tasks/vault_auth.yml`.
5. Delete the matching include block in `tasks/certs_main.yml`.
6. Drop ACME/Cloudflare rows from the role README Requirements table.

After strip, cert path is REST (or `vecs_ssh`) with `provider: files|csr` only.
Set `vcenter_cert_file` / `_key_file` / `_root_file` (or CSR signed paths).

## Enable (lab with public LE)

```yaml
vcenter_manage_certs: true
vcenter_cert_replace: true             # second gate — without this, no mutation
vcenter_cert_issue_acme: true          # package default is false
vcenter_acme_email: "ops@example.com"
vcenter_cloudflare_zone: "example.com"
# token via ansible-vault or HashiCorp Vault path below
```

Work estates with a manual/self-signed Machine SSL: leave `manage_certs`
and/or `cert_replace` **false**. Topology never requires this package.
