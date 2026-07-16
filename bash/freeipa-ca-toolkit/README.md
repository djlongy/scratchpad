# FreeIPA / Dogtag CA Toolkit

Understand, validate, preserve, and re-use a FreeIPA (Dogtag) Certificate Authority —
without being a PKI expert. Built to answer a concrete goal: **stand up a separate test
FreeIPA realm whose certificates are already trusted by existing endpoints** because they
chain to the same trusted CA hierarchy as production.

- `validate-freeipa-p12.sh` — read-only inspector for `.p12` files + the live CA database.
  Tells you in plain English what each file holds and what your CA chain looks like.
- `test-harness.sh` — self-test proving the validator works (synthetic fixtures + an
  optional real Dogtag export). Run it if you want to trust the tool before using it.

Nothing here ever prints private-key material; outputs are safe to share.

---

## 1. The two files everyone confuses: `ca-agent.p12` vs `cacert.p12`

Think of the CA as an official rubber stamp:

| File | What it is | Has a private key? | Can it sign certificates? |
|------|-----------|--------------------|---------------------------|
| **`cacert.p12`** | The **stamp + master keys**. Holds the CA signing cert **and key** (`caSigningCert cert-pki-ca`) plus the other Dogtag subsystem keys (ocsp, subsystem, audit, Server-Cert). | Yes — the **signing** key. | **Yes.** This *is* the CA. Crown jewels. |
| **`ca-agent.p12`** | The **ID badge** of the clerk allowed to operate the stamp — a client login to the CA's admin interface. | Yes — but only an **agent/client** key. | **No.** It authenticates you to the CA; it cannot sign. |

Both contain *a* private key, so both feel like "the CA" — but they are **different keys for
different jobs**. Only `cacert.p12` holds the signing key. `ca-agent.p12` usually also carries
the CA's *public* certificate (for chain/trust), which is why it looks like it "has the CA."
Public cert ≠ private signing key.

## 2. "Why can I only read it with `-nokeys`? Are my tools wrong?"

**Your tools aren't wrong, and the file isn't corrupt.** This is expected:

```
openssl pkcs12 -info -in cacert.p12 -nokeys    # ✅ works (shows certs)
openssl pkcs12 -info -in cacert.p12 -nocerts   # ❌ "bad decrypt / cipher final error"
```

Dogtag/NSS encrypts the PKCS#12 **key bags** with an older PBE cipher (e.g.
`pbeWithSHA1And3-KeyTripleDES-CBC`). **OpenSSL 3.x moved those legacy ciphers into a
non-default "legacy provider"**, so out of the box OpenSSL 3 can decrypt the *cert* bags but
not the *key* bags. The key is present and fine. Two ways to read it:

```
openssl pkcs12 -info -in cacert.p12 -nodes -legacy      # add -legacy
pk12util -l cacert.p12                                    # or use NSS tools (what Dogtag uses)
```

`pk12util` (NSS) is the native reader for Dogtag P12s and never hits this — which is why it
"just worked" for you. This toolkit uses `pk12util` for keys and falls back to `-legacy` for
certs, so you don't have to think about it.

## 3. Run the validator

Requires `nss-tools` (`pk12util`, `certutil`) + `openssl`. On RHEL/Alma: `dnf install nss-tools`.

```bash
chmod +x validate-freeipa-p12.sh

# Inspect the P12 files (prompts for each file's password; nothing echoed):
./validate-freeipa-p12.sh ca-agent.p12 cacert.p12

# Also confirm the live signing key in the running CA (auto-reads the Dogtag password):
sudo ./validate-freeipa-p12.sh --nssdb /var/lib/pki/pki-tomcat/alias ca-agent.p12 cacert.p12
```

Non-interactive / automation (password via env, never on the command line):

```bash
P12PASS='…' NSSPASS='…' ./validate-freeipa-p12.sh --nssdb /var/lib/pki/pki-tomcat/alias cacert.p12
```

Options: `--ipa-ca PATH` (default `/etc/ipa/ca.crt`), `--nssdb PATH`, `-h`.

### What to expect

- **`cacert.p12`** → `VERDICT: *** This file CONTAINS the CA SIGNING key … crown jewels ***`,
  listing keys incl. `caSigningCert cert-pki-ca`.
- **`ca-agent.p12`** → `VERDICT: This is a CLIENT / AGENT identity … cannot sign`,
  with the agent cert shown as `CA:FALSE … EKU: Client Authentication`.
- **CA MODE (AUTHORITATIVE)** → the deciding fact, read from the `caSigningCert` itself (via the
  `cacert.p12` or `--nssdb`):
  - `EXTERNAL-CA SUB-CA` + the "signed by" line = the Issuer is your **root/intermediate**, the
    signer for a sibling test CA. → **Case X** below.
  - `SELF-SIGNED ROOT` → the CA *is* the root. → **Case Y** below.
- **TRUST BUNDLE (`/etc/ipa/ca.crt`)** is shown as a **secondary cross-check only**. It can read
  "self-signed" even on an external-ca server, because that file often publishes the **external
  root** (which is self-signed — all roots are), *not* the IPA CA. If the bundle disagrees with
  the CA MODE line, the tool flags it — **trust the CA MODE line** (it reads the actual signing
  cert). The fastest definitive check needs no P12:
  `sudo ./validate-freeipa-p12.sh --nssdb /var/lib/pki/pki-tomcat/alias`

## 4. How to proceed (decision tree)

### Case X — prod CA is a SUB-CA (chain shows ≥ 2 certs)  ← the good case
Your endpoints trust the **root** at the top. Stand up the test realm as a **sibling
sub-CA** under that same root — do **not** clone/reuse prod's signing key.

```
Trusted Root CA
├── Production FreeIPA CA   (your existing prod)
└── Test FreeIPA CA         (new realm, signed by the SAME root/intermediate)
```

Benefits: endpoints already trust the root, prod & test stay isolated, no shared keys, no
serial/CRL conflicts, a test compromise can't touch prod trust.

**→ Full step-by-step for standing up the test realm is in [`SIGNING.md`](./SIGNING.md)** (both
the sibling path and the pragmatic "sign with the CA you have" path).

**The one dependency:** you need to *sign the test CA's CSR with that root/intermediate* —
either you hold its private key, or corporate/offline PKI signs the CSR for you (no key
changes hands). The `caSigningCert` key inside `cacert.p12` is prod's **sub-CA** — the wrong
level for this; it would only let you make test a *child of prod* (couples them — avoid).

### Case Y — prod CA is a SELF-SIGNED ROOT (one self-signed cert)
There is no parent. A sibling test CA would have to be signed by this same root key
(`cacert.p12`'s signing key) — which couples test to prod's root. Prefer instead to create an
independent test CA and distribute its cert to the endpoints you care about, or introduce a
new shared root going forward. Flag this result — it changes the plan.

### Either case, if you can't get the signer to sign
Fall back to distributing the test CA cert to the relevant trust stores manually (Firefox
uses its own NSS store; browsers/OS differ).

## 5. Plugging into the `freeipa_server` role (external-ca, two-phase)

Once you can sign the test CA's CSR (Case X):

1. `freeipa_server_ca_mode: external-ca` → the first run emits `/root/ipa.csr` (the test
   realm's CA request).
2. Sign that CSR with the root/intermediate, applying sub-CA extensions:
   `basicConstraints=critical,CA:TRUE`, `keyUsage=critical,keyCertSign,cRLSign`, **pathlen:0**
   (test CA can't spawn more CAs), SKI/AKI, and ideally **nameConstraints** pinned to the test
   DNS domain (so a test-env compromise can only mint test-domain certs).
3. Second run: `freeipa_server_external_cert_files: [signed-test-ca.crt, chain.crt]` where
   `chain.crt` is the intermediate(s) + root up to the trusted anchor. The role finishes the
   install and the new realm's certs chain to the root your endpoints already trust.

## 6. "Do we keep P12 format?" — preservation strategy

- **Source of truth = the live NSS database**, not the P12s. Back up
  `/var/lib/pki/pki-tomcat/alias/` + `/var/lib/pki/pki-tomcat/conf/password.conf` + `/etc/ipa/`.
  Those are the authoritative, recoverable CA state — worth more than loose `ca-agent.p12` /
  `cacert.p12` copies.
- **P12 is a fine portable export** (`pk12util -o …`) — keep as a secondary backup.
- **For the role's external-ca step, produce PEM** (signed cert + chain). Convert at the point
  of use; don't treat loose P12s as the system of record.
- **Store any private-key material in a secrets manager (Vault), encrypted and access-controlled
  — never loose on a server.** The CA signing key is a root-equivalent secret.

## 7. Verify the tool itself

```bash
sudo ./test-harness.sh ./validate-freeipa-p12.sh
```

Builds synthetic fixtures (including a legacy-cipher P12 that reproduces the OpenSSL-3 quirk
above), runs the validator non-interactively, and asserts every verdict. On a host that runs a
Dogtag CA it also exports the real `caSigningCert` to a throwaway P12, validates it, and
securely deletes it. Expect `RESULT: 9 passed, 0 failed`.

## Security notes

- Read-only. No command here prints a private key; outputs are safe to paste/share.
- Passwords are read silently, held only in `0600` temp files under a `0700` temp dir, and
  wiped on exit — never placed on the command line.
- The CA signing key is root-equivalent. Treat exports and backups accordingly (Vault, encrypted,
  short-lived, shredded).
