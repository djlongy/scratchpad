# Stand up a test FreeIPA realm signed into an existing CA hierarchy

Goal: a new realm `freeipa.<testsub>.<root-domain>` whose certificates are **already trusted**
by existing endpoints, because its CA chains up to the same trusted root as production.

You've confirmed prod is **external-ca** (the `caSigningCert` has `subject != issuer`), so this
uses FreeIPA's two-phase `--external-ca` install: FreeIPA makes its own CA **key**, you only
**sign its CSR** with an existing CA. The new realm never shares prod's private key.

---

## First decide: WHO signs the test CA's CSR

| | **Path A — sibling (isolated, preferred)** | **Path B — under prod's IPA CA (pragmatic)** |
|---|---|---|
| Signer | the **ROOT/intermediate** that signed prod's IPA CA (its Issuer) | **prod's IPA CA** itself (the key in `cacert.p12`) |
| Result chain | test → **root** (test is a *sibling* of prod) | test → **prod IPA CA** → root (test is a *child* of prod) |
| Isolation | full — no shared keys, test can't affect prod | test's CA is a valid sub-CA under prod's CA (coupled) |
| Needs | the **root's private key**, or corporate PKI signs the CSR for you | only `cacert.p12` (which you have) |

Which do you have? Check your validator's key list:
```
./validate-freeipa-p12.sh cacert.p12        # look at "PRIVATE KEYS present:"
```
- If **only** `caSigningCert cert-pki-ca` has a key → you have prod's IPA CA key but **not** the
  root key → **Path B** (or get corporate to sign the CSR for Path A).
- If the **root** cert also has a key available → **Path A**.

Path B works and gives you trusted test certs today; contain the coupling with `pathlen:0` +
`nameConstraints` (step 3). Steps below cover both — only the signer in step 1 & 3 differs.

---

## Step 0 — Prep the test host
- A fresh RHEL/Alma VM, correct FQDN (`ipa.<testsub>.<root-domain>`), time synced (chrony).
- DNS: an A record for the test IPA host resolvable on the network you'll test from.
- `dnf install ipa-server ipa-server-dns` (add `-dns` only if you want integrated DNS).

## Step 1 — Get the signer as PEM (on a SECURE admin host, not the test box)
Export just the signing identity from the live CA's NSS DB (source of truth), then to PEM:
```
# Path B signer = prod's IPA CA. (Path A signer = your ROOT — export that identity instead.)
sudo pk12util -o /root/signer.p12 -n 'caSigningCert cert-pki-ca' \
  -d /var/lib/pki/pki-tomcat/alias -k /var/lib/pki/pki-tomcat/conf/pwdfile.txt -W 'TMP_PASS'

openssl pkcs12 -in /root/signer.p12 -clcerts -nokeys  -legacy -passin pass:TMP_PASS -out signer.crt
openssl pkcs12 -in /root/signer.p12 -nocerts -nodes   -legacy -passin pass:TMP_PASS -out signer.key
# also grab the chain up to the root (public certs):
#   signer.crt (prod IPA CA)  +  root.crt (the external root, from cacert.p12 or /etc/ipa/ca.crt)
```
⚠ `signer.key` is root-equivalent. Keep it on an encrypted host, delete when done (`shred -u`),
and store the master copy in Vault — never leave it loose.

## Step 2 — Phase 1 on the test host: emit the CA CSR
Either by hand:
```
ipa-server-install --external-ca \
  --realm 'FREEIPA.<TESTSUB>.<ROOT-DOMAIN>' --domain 'freeipa.<testsub>.<root-domain>' \
  --ds-password '…' --admin-password '…' [--setup-dns --forwarder … | --no-host-dns]
# stops after writing /root/ipa.csr
```
…or via the role: set `freeipa_server_ca_mode: external-ca` and run — it emits `/root/ipa.csr`.

## Step 3 — Sign the CSR with the signer + CA extensions
```
cat > subca-ext.cnf <<'EOF'
basicConstraints      = critical,CA:TRUE,pathlen:0
keyUsage              = critical,digitalSignature,keyCertSign,cRLSign
subjectKeyIdentifier  = hash
authorityKeyIdentifier= keyid:always
# OPTIONAL isolation (Path B esp.): restrict the test CA to its own DNS domain.
# Only enable if the realm issues names ONLY under <testsub>.<root-domain>, else it breaks issuance:
# nameConstraints     = critical,permitted;DNS:.<testsub>.<root-domain>
EOF

openssl x509 -req -in /root/ipa.csr \
  -CA signer.crt -CAkey signer.key -CAcreateserial \
  -extfile subca-ext.cnf -days 1826 -sha256 -out test-ipa-ca.crt
```

## Step 4 — Assemble the chain the installer needs
```
# the signed test CA + every issuer above it, up to the trusted root:
cat signer.crt root.crt > ca-chain.crt      # Path B: prod IPA CA + root
#                                             Path A: intermediates + root (no prod IPA CA)
```

## Step 5 — Phase 2 on the test host: finish the install
```
ipa-server-install --external-cert-file=/root/test-ipa-ca.crt --external-cert-file=/root/ca-chain.crt \
  [same other args as phase 1]
```
…or via the role: `freeipa_server_external_cert_files: ['/root/test-ipa-ca.crt','/root/ca-chain.crt']`
then re-run.

## Step 6 — Verify + clean up
```
openssl verify -CAfile /etc/ipa/ca.crt /var/lib/ipa/certs/httpd.crt     # should say: OK
openssl x509 -in /etc/ipa/ca.crt -noout -issuer -subject                # test IPA CA issuer = your signer
shred -u signer.key signer.p12 /root/*.p12 2>/dev/null                   # destroy the signing key copy
```
Endpoints that already trust the root now trust the test realm — no per-box cert import needed.
Store the signing material + the test realm's own CA backup in Vault.

---

### Notes
- `pathlen:0` lets the test CA issue normal host/service certs but **not** further sub-CAs — right
  for a test realm.
- `nameConstraints` is strong isolation but strict: enable it only if every cert the realm issues
  is under `<testsub>.<root-domain>` (no stray IP-SAN-only or cross-domain certs), or issuance fails.
- Prefer **Path A** if you can get the root to sign — it keeps test and prod fully independent.
- If corporate PKI holds the root: skip step 1, submit `/root/ipa.csr` to them, and use the cert
  they return as `test-ipa-ca.crt` in step 4/5.
