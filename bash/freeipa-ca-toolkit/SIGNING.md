# Stand up a test FreeIPA realm trusted by your existing endpoints

Goal: a new realm `ipa.<testsub>.<root-domain>` whose certificates are **already trusted**,
because its CA chains up to the same root as production. FreeIPA's `--external-ca` mode does
this in two phases: FreeIPA makes its own CA **key**, you only **sign its CSR** with an existing
CA — the new realm never shares a private key with prod.

You don't need to understand the PKI. **Run the validator, read one line, follow that path.**

---

## Step 0 — Let the validator tell you which path

On the production FreeIPA server:
```
git pull                       # get the toolkit
cd bash/freeipa-ca-toolkit
sudo ./validate-freeipa-p12.sh --nssdb /var/lib/pki/pki-tomcat/alias
```
Scroll to the bottom, **`===> WHAT YOU CAN DO`**. It prints one of:

| Validator says | Your path | Why |
|---|---|---|
| `PATH A … READY HERE — the ROOT key is in this DB` | **Path A (local)** | you hold the root key → sign a fully isolated sibling yourself |
| `PATH A … you have the root CERT but NOT its key here` | **Path A (submission)** | root key is elsewhere → send the CSR to whoever owns it |
| `PATH A: the root's cert isn't in this DB` | **Path A (submission)** | root lives outside FreeIPA → send the CSR to that team |
| only `PATH B … READY` (self-signed realm) | **Path B** | there is no separate root; sign with the IPA CA you have |

**Recommendation:** Path A keeps test and prod fully isolated and is preferred. If the root key
isn't yours to hold (the common enterprise case), use **Path A (submission)** — same result,
someone else just runs the one signing command. Path B is the quick fallback that needs nothing
external, contained with `nameConstraints`.

The `CA HIERARCHY & KEY MAP` section just above tells you the exact names: which cert is the
**ROOT CA**, which is your **IPA CA**, and which keys are present.

---

## Common — Step 1: create the CSR on the test host (all paths)
Prep: fresh RHEL/Alma VM, FQDN `ipa.<testsub>.<root-domain>`, time synced, `dnf install ipa-server`.
```
ipa-server-install --external-ca \
  --realm 'IPA.<TESTSUB>.<ROOT-DOMAIN>' --domain 'ipa.<testsub>.<root-domain>' \
  --ds-password '…' --admin-password '…'
#  → stops after writing /root/ipa.csr
```
(Or via the role: `freeipa_server_ca_mode: external-ca`, run once → it emits `/root/ipa.csr`.)
Copy `/root/ipa.csr` to wherever the signing happens.

---

## PATH A (submission) — recommended when the root key isn't yours
You never touch the root key. Whoever owns the root (corporate PKI / AD CS / the team that built
prod FreeIPA) signs your CSR.

1. From Step 0's hierarchy, note the **ROOT CA name** and confirm who operates it.
2. Send them `/root/ipa.csr` and ask for a **subordinate CA certificate** with:
   `basicConstraints=CA:TRUE,pathlen:0`, `keyUsage=keyCertSign,cRLSign`, valid ~2 yrs.
   (If they support it, ask them to add `nameConstraints` limiting it to `.<testsub>.<root-domain>`.)
3. They return `test-ipa-ca.crt`. Get the **chain** too: every cert from that up to the root
   (`ca-chain.crt` = intermediate(s) + root, public certs — the validator's hierarchy lists them).
4. Jump to **Common — Step 3** (finish the install).

## PATH A (local) — only if the validator said the ROOT key is in the DB
You hold the root key, so you sign the sibling yourself.

1. Export the **ROOT** identity to PEM (use the ROOT's name from Step 0's hierarchy, *not*
   `caSigningCert`), on a secure host:
   ```
   sudo pk12util -o /root/rootca.p12 -n '<ROOT CA name from hierarchy>' \
     -d /var/lib/pki/pki-tomcat/alias -k /var/lib/pki/pki-tomcat/conf/pwdfile.txt -W 'TMP_PASS'
   openssl pkcs12 -in /root/rootca.p12 -clcerts -nokeys -legacy -passin pass:TMP_PASS -out signer.crt
   openssl pkcs12 -in /root/rootca.p12 -nocerts -nodes  -legacy -passin pass:TMP_PASS -out signer.key
   ```
2. Sign the CSR → **Common — Step 2**, using `signer.crt`/`signer.key` (the ROOT).
3. Chain `ca-chain.crt` = `signer.crt` (root) alone (test chains straight to the root).
4. **Common — Step 3**.

## PATH B — quick fallback (sign with the IPA CA you already have)
Test becomes a child of prod's IPA CA (chains to root, trusted). Contained by `nameConstraints`.

1. Export the **IPA CA** identity to PEM (name `caSigningCert cert-pki-ca`), secure host:
   ```
   sudo pk12util -o /root/signer.p12 -n 'caSigningCert cert-pki-ca' \
     -d /var/lib/pki/pki-tomcat/alias -k /var/lib/pki/pki-tomcat/conf/pwdfile.txt -W 'TMP_PASS'
   openssl pkcs12 -in /root/signer.p12 -clcerts -nokeys -legacy -passin pass:TMP_PASS -out signer.crt
   openssl pkcs12 -in /root/signer.p12 -nocerts -nodes  -legacy -passin pass:TMP_PASS -out signer.key
   ```
2. Sign the CSR → **Common — Step 2**, using `signer.crt`/`signer.key` (the IPA CA).
3. Chain `ca-chain.crt` = `signer.crt` (IPA CA) + `root.crt` (the external root, from the
   validator's hierarchy or `/etc/ipa/ca.crt`).
4. **Common — Step 3**.

---

## Common — Step 2: sign the CSR (Path A-local / Path B only)
```
cat > subca-ext.cnf <<'EOF'
basicConstraints      = critical,CA:TRUE,pathlen:0
keyUsage              = critical,digitalSignature,keyCertSign,cRLSign
subjectKeyIdentifier  = hash
authorityKeyIdentifier= keyid:always
# OPTIONAL isolation — only if the realm issues names ONLY under the test domain, else issuance breaks:
# nameConstraints     = critical,permitted;DNS:.<testsub>.<root-domain>
EOF

openssl x509 -req -in /root/ipa.csr -CA signer.crt -CAkey signer.key -CAcreateserial \
  -extfile subca-ext.cnf -days 1826 -sha256 -out test-ipa-ca.crt
```
⚠ `signer.key` is root-equivalent (or IPA-CA-equivalent). Encrypted host only; `shred -u` it when
done; keep the master in Vault.

## Common — Step 3: finish the install on the test host
```
ipa-server-install \
  --external-cert-file=/root/test-ipa-ca.crt \
  --external-cert-file=/root/ca-chain.crt \
  [same args as Step 1]
```
(Or the role: `freeipa_server_external_cert_files: ['/root/test-ipa-ca.crt','/root/ca-chain.crt']`,
re-run.)

## Common — Step 4: verify + clean up
```
openssl verify -CAfile /etc/ipa/ca.crt /var/lib/ipa/certs/httpd.crt    # -> OK
openssl x509 -in /etc/ipa/ca.crt -noout -issuer -subject               # issuer = your signer
shred -u signer.key signer.p12 rootca.p12 2>/dev/null                   # destroy signing-key copies
```
Endpoints that already trust the root now trust the test realm — no per-box import needed. Back up
the test realm's own CA (`/var/lib/pki/pki-tomcat/alias` + `/etc/ipa/`) and store any keys in Vault.

### Notes
- `pathlen:0` = the test CA can issue host/service certs but not further CAs (right for a test realm).
- `nameConstraints` is strong isolation but strict — enable only if every issued name is under the
  test domain, or issuance fails. Path A already isolates test from prod, so it's optional there;
  it matters most for Path B.
