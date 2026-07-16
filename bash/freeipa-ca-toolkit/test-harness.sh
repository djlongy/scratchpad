#!/usr/bin/env bash
# test-harness.sh — validate validate-freeipa-p12.sh flawlessly.
# Builds synthetic fixtures (incl. a legacy-cipher p12 that reproduces the Dogtag
# "openssl can't read keys" quirk) + optionally a REAL Dogtag export, runs the
# validator non-interactively, and asserts the verdicts. Run on a RHEL box with
# openssl + nss-tools. Cleans up everything. Usage: sudo ./test-harness.sh [VALIDATOR]
set -uo pipefail
VALIDATOR="${1:-./validate-freeipa-p12.sh}"
PW="test123"
T="$(mktemp -d)"; trap 'rm -rf "$T"' EXIT
cd "$T"
pass=0; fail=0
ok(){ echo "  PASS: $1"; pass=$((pass+1)); }
no(){ echo "  FAIL: $1"; fail=$((fail+1)); }
DB="$T/db"; mkdir -p "$DB"; certutil -N -d "$DB" --empty-password >/dev/null 2>&1

echo "### building synthetic fixtures"
# root CA
openssl req -x509 -newkey rsa:2048 -nodes -keyout root.key -out root.crt -days 3 \
  -subj "/O=TESTORG/CN=Test Root CA" >/dev/null 2>&1
# sub-CA signed by root (CA:TRUE) — mimics a FreeIPA Dogtag sub-CA
openssl req -newkey rsa:2048 -nodes -keyout subca.key -out subca.csr \
  -subj "/O=TEST.REALM/CN=Certificate Authority" >/dev/null 2>&1
openssl x509 -req -in subca.csr -CA root.crt -CAkey root.key -CAcreateserial -out subca.crt -days 3 \
  -extfile <(printf 'basicConstraints=critical,CA:TRUE\nkeyUsage=critical,keyCertSign,cRLSign\n') >/dev/null 2>&1
# agent client cert (CA:FALSE, clientAuth) — mimics ca-agent
openssl req -newkey rsa:2048 -nodes -keyout agent.key -out agent.csr \
  -subj "/O=TEST.REALM/CN=ipa-ca-agent" >/dev/null 2>&1
openssl x509 -req -in agent.csr -CA subca.crt -CAkey subca.key -CAcreateserial -out agent.crt -days 3 \
  -extfile <(printf 'basicConstraints=critical,CA:FALSE\nextendedKeyUsage=clientAuth\n') >/dev/null 2>&1

# cacert-like p12: friendly name "caSigningCert cert-pki-ca", LEGACY cipher (reproduces the
# "openssl -nocerts bad decrypt" quirk so we test the -legacy fallback + pk12util key path)
openssl pkcs12 -export -legacy -inkey subca.key -in subca.crt -certfile root.crt \
  -name "caSigningCert cert-pki-ca" -passout pass:$PW -out cacert.p12 >/dev/null 2>&1
# agent-like p12
openssl pkcs12 -export -inkey agent.key -in agent.crt -certfile subca.crt \
  -name "ipa-ca-agent" -passout pass:$PW -out ca-agent.p12 >/dev/null 2>&1
# chains
cat subca.crt root.crt > chain-subca.crt      # 2 certs -> sub-CA case
cp root.crt chain-selfsigned.crt               # 1 self-signed -> root case

echo "### run 1: cacert.p12 + ca-agent.p12, sub-CA chain"
P12PASS=$PW bash "$VALIDATOR" --ipa-ca chain-subca.crt ca-agent.p12 cacert.p12 > out1.txt 2>&1 || true
cat out1.txt
echo "### assertions (run 1)"
grep -q "CONTAINS the CA SIGNING key" out1.txt && ok "cacert.p12 -> signing-key verdict" || no "cacert.p12 signing verdict"
grep -q "caSigningCert cert-pki-ca" out1.txt && ok "detected caSigningCert friendly name (pk12util)" || no "caSigningCert name"
grep -q "CLIENT / AGENT identity" out1.txt && ok "ca-agent.p12 -> client verdict" || no "agent verdict"
grep -qi "CA:FALSE" out1.txt && ok "agent cert classified CA:FALSE" || no "agent CA:FALSE"
grep -qi "Test Root CA" out1.txt && ok "chain names the root signer" || no "root signer shown"
# authoritative CA-mode read from the signing cert itself (the definitive verdict)
grep -q "EXTERNAL-CA SUB-CA" out1.txt && ok "authoritative CA MODE = external-ca (from signing cert)" || no "authoritative external-ca"
grep -Eq "AUTHORITATIVE .*= external-ca|= external-ca\." out1.txt && ok "bundle section reconciles to external-ca" || no "reconcile external-ca"

echo "### run 2: self-signed chain + self-signed cacert-style p12"
# a self-signed CA p12 -> authoritative CA MODE must say self-signed
openssl req -x509 -newkey rsa:2048 -nodes -keyout ss.key -out ss.crt -days 3 \
  -subj "/O=SS.REALM/CN=Certificate Authority" \
  -addext "basicConstraints=critical,CA:TRUE" -addext "keyUsage=critical,keyCertSign,cRLSign" >/dev/null 2>&1
openssl pkcs12 -export -legacy -inkey ss.key -in ss.crt -name "caSigningCert cert-pki-ca" -passout pass:$PW -out ss-cacert.p12 >/dev/null 2>&1
P12PASS=$PW bash "$VALIDATOR" --ipa-ca chain-selfsigned.crt ss-cacert.p12 > out2.txt 2>&1 || true
grep -q "SELF-SIGNED ROOT" out2.txt && ok "authoritative CA MODE = self-signed" || no "self-signed verdict"

echo "### run 2b: DISAGREEMENT — external signing cert vs self-signed bundle (the user's case)"
P12PASS=$PW bash "$VALIDATOR" --ipa-ca chain-selfsigned.crt cacert.p12 > out2b.txt 2>&1 || true
grep -q "EXTERNAL-CA SUB-CA" out2b.txt && ok "signing cert wins: external-ca" || no "signing cert external"
grep -q "bundle DISAGREES" out2b.txt && ok "flags bundle/signing-cert disagreement" || no "disagreement flagged"

echo "### run 3: REAL Dogtag export (if this box runs pki-tomcat CA)"
ALIAS=/var/lib/pki/pki-tomcat/alias
if [[ -d "$ALIAS" ]]; then
  NSSPW=""
  for pf in /var/lib/pki/pki-tomcat/conf/pwdfile.txt; do [[ -r "$pf" ]] && NSSPW="$pf" && break; done
  if [[ -z "$NSSPW" && -r /var/lib/pki/pki-tomcat/conf/password.conf ]]; then
    awk -F= '/^internal=/{sub(/^internal=/,"");print;exit}' /var/lib/pki/pki-tomcat/conf/password.conf > "$T/nsspw"; NSSPW="$T/nsspw"
  fi
  if [[ -n "$NSSPW" ]] && pk12util -o "$T/real-cacert.p12" -n "caSigningCert cert-pki-ca" -d "$ALIAS" -k "$NSSPW" -W "$PW" >/dev/null 2>&1; then
    P12PASS=$PW bash "$VALIDATOR" "$T/real-cacert.p12" > out3.txt 2>&1 || true
    grep -q "CONTAINS the CA SIGNING key" out3.txt && ok "REAL Dogtag p12 -> signing-key verdict" || no "real Dogtag verdict"
    grep -q "caSigningCert cert-pki-ca" out3.txt && ok "REAL Dogtag friendly name detected" || no "real Dogtag name"
    command -v shred >/dev/null && shred -u "$T/real-cacert.p12" 2>/dev/null || rm -f "$T/real-cacert.p12"
    echo "  (real export securely deleted)"
  else
    echo "  SKIP real test — could not export (NSS pw / not a CA master)"
  fi
else
  echo "  SKIP real test — no /var/lib/pki/pki-tomcat/alias on this host"
fi

echo "### RESULT: $pass passed, $fail failed"
[[ $fail -eq 0 ]]
