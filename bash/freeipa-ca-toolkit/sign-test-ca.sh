#!/usr/bin/env bash
# sign-test-ca.sh
# ---------------------------------------------------------------------------
# One-shot signer for a FreeIPA `--external-ca` test realm.
# Feed it the CSR from phase 1; it signs that CSR with your existing CA (exported
# from the live NSS DB, or a PEM you provide), applying correct sub-CA extensions,
# and writes the two files phase 2 needs: test-ipa-ca.crt + ca-chain.crt.
#
# Handles the self-signed-root case (prod IPA CA is its own root → chain = that cert
# alone) and the external-ca case (appends the issuer chain from the NSS DB).
#
# The signing key is exported to a locked-down temp dir and shredded on exit; it is
# never printed. Output files are public certs (safe to move to the test host).
#
# USAGE:
#   sudo ./sign-test-ca.sh --csr /root/ipa.csr [options]
# OPTIONS:
#   --csr PATH            the phase-1 CSR (required)
#   --nssdb PATH          NSS DB to export the signer from (default /var/lib/pki/pki-tomcat/alias)
#   --signer-name NAME    NSS nickname of the signing CA (default 'caSigningCert cert-pki-ca')
#   --signer-cert PATH --signer-key PATH
#                         use these PEMs instead of exporting from the NSS DB (Path A-local:
#                         point at your ROOT). Skips --nssdb.
#   --name-constraint DNS restrict the test CA to a DNS domain, e.g. .qa.core.example
#                         (recommended when signing under a live prod CA)
#   --days N              validity of the signed CA cert (default 1826 = 5y)
#   --out DIR             output directory (default: current dir)
#   -h | --help
#
# Requires: openssl + (for NSS export) nss-tools. NSS password auto-read from
# conf/pwdfile.txt or conf/password.conf, or pass env NSSPASS.
# ---------------------------------------------------------------------------
set -uo pipefail

CSR="" ; NSSDB="/var/lib/pki/pki-tomcat/alias" ; SIGNER_NAME="caSigningCert cert-pki-ca"
SIGNER_CERT="" ; SIGNER_KEY="" ; NC="" ; DAYS=1826 ; OUT="."
die(){ echo "ERROR: $*" >&2; exit 2; }

while [[ $# -gt 0 ]]; do
  case "$1" in
    --csr) CSR="${2:-}"; shift 2;;
    --nssdb) NSSDB="${2:-}"; shift 2;;
    --signer-name) SIGNER_NAME="${2:-}"; shift 2;;
    --signer-cert) SIGNER_CERT="${2:-}"; shift 2;;
    --signer-key) SIGNER_KEY="${2:-}"; shift 2;;
    --name-constraint) NC="${2:-}"; shift 2;;
    --days) DAYS="${2:-}"; shift 2;;
    --out) OUT="${2:-}"; shift 2;;
    -h|--help) sed -n '2,40p' "$0" | sed 's/^# \?//'; exit 0;;
    *) die "unknown option: $1 (see --help)";;
  esac
done

[[ -n "$CSR" ]] || die "pass --csr /root/ipa.csr (the phase-1 CSR)"
[[ -r "$CSR" ]] || die "cannot read CSR: $CSR"
openssl req -in "$CSR" -noout >/dev/null 2>&1 || die "$CSR is not a valid CSR"
command -v openssl >/dev/null || die "openssl not found"
mkdir -p "$OUT" || die "cannot create --out dir: $OUT"

umask 077
WORK="$(mktemp -d)"; chmod 700 "$WORK"
cleanup(){ command -v shred >/dev/null && find "$WORK" -type f -exec shred -u {} + 2>/dev/null; rm -rf "$WORK"; }
trap cleanup EXIT INT TERM

# ---- obtain signer cert + key as PEM ----
if [[ -n "$SIGNER_CERT" || -n "$SIGNER_KEY" ]]; then
  [[ -r "$SIGNER_CERT" && -r "$SIGNER_KEY" ]] || die "both --signer-cert and --signer-key must be readable PEMs"
  cp "$SIGNER_CERT" "$WORK/signer.crt"; cp "$SIGNER_KEY" "$WORK/signer.key"
  echo "Signer: PEM files you provided."
else
  command -v pk12util >/dev/null || die "nss-tools (pk12util) not found; or pass --signer-cert/--signer-key"
  [[ -d "$NSSDB" ]] || die "NSS DB not found: $NSSDB (need root? or pass --signer-cert/--signer-key)"
  # NSS token password
  npw=""
  if   [[ -r "$NSSDB/../conf/pwdfile.txt" ]]; then npw="$NSSDB/../conf/pwdfile.txt"
  elif [[ -r "$NSSDB/pwdfile.txt"        ]]; then npw="$NSSDB/pwdfile.txt"
  elif [[ -r "$NSSDB/../conf/password.conf" ]]; then
    awk -F= '/^internal=/{sub(/^internal=/,"");print;exit}' "$NSSDB/../conf/password.conf" > "$WORK/npw" && npw="$WORK/npw"
  elif [[ -n "${NSSPASS:-}" ]]; then printf '%s' "$NSSPASS" > "$WORK/npw"; npw="$WORK/npw"
  fi
  [[ -n "$npw" ]] || die "could not find the NSS DB password (need sudo, or set NSSPASS=…)"
  tmp="$(openssl rand -hex 16)"
  pk12util -o "$WORK/signer.p12" -n "$SIGNER_NAME" -d "$NSSDB" -k "$npw" -W "$tmp" >/dev/null 2>&1 \
    || die "pk12util could not export '$SIGNER_NAME' from $NSSDB (wrong nickname? try: certutil -L -d $NSSDB)"
  openssl pkcs12 -in "$WORK/signer.p12" -clcerts -nokeys -legacy -passin "pass:$tmp" -out "$WORK/signer.crt" 2>/dev/null \
    || openssl pkcs12 -in "$WORK/signer.p12" -clcerts -nokeys -passin "pass:$tmp" -out "$WORK/signer.crt" 2>/dev/null
  openssl pkcs12 -in "$WORK/signer.p12" -nocerts -nodes -legacy -passin "pass:$tmp" -out "$WORK/signer.key" 2>/dev/null \
    || openssl pkcs12 -in "$WORK/signer.p12" -nocerts -nodes -passin "pass:$tmp" -out "$WORK/signer.key" 2>/dev/null
  echo "Signer: exported '$SIGNER_NAME' from $NSSDB."
fi
grep -q 'BEGIN CERTIFICATE' "$WORK/signer.crt" 2>/dev/null || die "failed to obtain the signer certificate"
grep -q 'BEGIN .*PRIVATE KEY' "$WORK/signer.key" 2>/dev/null || die "failed to obtain the signer private key"

sSubj=$(openssl x509 -in "$WORK/signer.crt" -noout -subject | sed 's/^subject= *//')
sIss=$( openssl x509 -in "$WORK/signer.crt" -noout -issuer  | sed 's/^issuer= *//')
selfsigned="no"; [[ "$(printf %s "$sSubj"|tr -d ' ')" == "$(printf %s "$sIss"|tr -d ' ')" ]] && selfsigned="yes"
echo "  signer subject: $sSubj"
echo "  signer is $( [[ $selfsigned == yes ]] && echo 'a SELF-SIGNED ROOT (test chains straight to it)' || echo 'a SUB-CA (chain will include its issuers)')"

# ---- extensions for the signed test CA ----
{
  echo "basicConstraints      = critical,CA:TRUE,pathlen:0"
  echo "keyUsage              = critical,digitalSignature,keyCertSign,cRLSign"
  echo "subjectKeyIdentifier  = hash"
  echo "authorityKeyIdentifier= keyid:always"
  [[ -n "$NC" ]] && echo "nameConstraints       = critical,permitted;DNS:${NC}"
} > "$WORK/ext.cnf"

# ---- sign ----
openssl x509 -req -in "$CSR" -CA "$WORK/signer.crt" -CAkey "$WORK/signer.key" -CAcreateserial \
  -extfile "$WORK/ext.cnf" -days "$DAYS" -sha256 -out "$OUT/test-ipa-ca.crt" 2>"$WORK/err" \
  || die "signing failed: $(tr '\n' ' ' <"$WORK/err")"

# ---- build ca-chain.crt (signer + its issuers up to the root) ----
cp "$WORK/signer.crt" "$OUT/ca-chain.crt"
if [[ "$selfsigned" != yes && -d "${NSSDB:-/nonexistent}" ]]; then
  # best-effort: append issuer certs from the NSS DB until we reach a self-signed root
  cur="$sIss"; guard=0
  while [[ -n "$cur" && $guard -lt 8 ]]; do
    guard=$((guard+1)); found=""
    while IFS= read -r nk; do
      certutil -L -d "$NSSDB" -n "$nk" -a >"$WORK/c.pem" 2>/dev/null || continue
      cs=$(openssl x509 -in "$WORK/c.pem" -noout -subject 2>/dev/null | sed 's/^subject= *//')
      [[ "$(printf %s "$cs"|tr -d ' ')" == "$(printf %s "$cur"|tr -d ' ')" ]] || continue
      cat "$WORK/c.pem" >> "$OUT/ca-chain.crt"; found=yes
      ci=$(openssl x509 -in "$WORK/c.pem" -noout -issuer 2>/dev/null | sed 's/^issuer= *//')
      [[ "$(printf %s "$cs"|tr -d ' ')" == "$(printf %s "$ci"|tr -d ' ')" ]] && cur="" || cur="$ci"
      break
    done < <(certutil -L -d "$NSSDB" 2>/dev/null | sed -E '1,/Trust Attributes/d' \
             | sed -E 's/[[:space:]]+[[:alnum:]]*,[[:alnum:]]*,[[:alnum:]]*[[:space:]]*$//' | sed '/^[[:space:]]*$/d')
    [[ -n "$found" ]] || { echo "  NOTE: couldn't find issuer '$cur' in the NSS DB — add it to ca-chain.crt by hand."; break; }
  done
fi

# output files are PUBLIC certs — make them world-readable so they can be moved without sudo
chmod 644 "$OUT/test-ipa-ca.crt" "$OUT/ca-chain.crt" 2>/dev/null || true

# ---- verify ----
verify_out=$(openssl verify -CAfile "$OUT/ca-chain.crt" "$OUT/test-ipa-ca.crt" 2>&1) || true

echo
echo "======================================================================"
echo "SIGNED. Wrote:"
echo "  $OUT/test-ipa-ca.crt   (the test realm's signed CA cert)"
echo "  $OUT/ca-chain.crt      (its issuer chain up to the trusted root)"
echo "chain verify: $verify_out"
[[ -n "$NC" ]] && echo "nameConstraints: locked to DNS:.${NC#.}"
echo
echo "NEXT — finish the install on the TEST host (phase 2):"
echo "  scp $OUT/test-ipa-ca.crt $OUT/ca-chain.crt  root@<test-host>:/root/"
echo "  ipa-server-install \\"
echo "    --external-cert-file=/root/test-ipa-ca.crt \\"
echo "    --external-cert-file=/root/ca-chain.crt   [same args as phase 1]"
echo "  # or the role: freeipa_server_external_cert_files: ['/root/test-ipa-ca.crt','/root/ca-chain.crt'] then re-run"
echo
echo "The signing key copy was shredded. Nothing above contains a private key."
echo "======================================================================"
