#!/usr/bin/env bash
# validate-freeipa-p12.sh
# ---------------------------------------------------------------------------
# Inspect FreeIPA / Dogtag PKCS#12 files and tell you, in plain English:
#   - which private keys + certificates each file holds,
#   - whether each is a CA SIGNING identity (crown jewels) or a client/agent login,
#   - and prod's CA chain level (self-signed root vs sub-CA, and who signed it).
#
# READ-ONLY. It never prints private-key material. Passwords are read silently,
# kept only in 0600 temp files inside a 0700 temp dir, and wiped on exit.
#
# Usage:
#   ./validate-freeipa-p12.sh [options] FILE.p12 [FILE2.p12 ...]
# Options:
#   --ipa-ca PATH   prod CA trust file to inspect  (default: /etc/ipa/ca.crt)
#   --nssdb  PATH   also confirm live CA private keys in this NSS DB
#                   (default off; typical: /var/lib/pki/pki-tomcat/alias)
#   -h | --help
#
# Examples:
#   ./validate-freeipa-p12.sh ca-agent.p12 cacert.p12
#   sudo ./validate-freeipa-p12.sh --nssdb /var/lib/pki/pki-tomcat/alias cacert.p12
#
# Requires: nss-tools (pk12util, certutil) + openssl.  RHEL/AlmaLinux: dnf install nss-tools
# ---------------------------------------------------------------------------
set -uo pipefail

IPA_CA="/etc/ipa/ca.crt"
NSSDB=""
FILES=()

while [[ $# -gt 0 ]]; do
  case "$1" in
    --ipa-ca) IPA_CA="${2:-}"; shift 2 ;;
    --nssdb)  NSSDB="${2:-}";  shift 2 ;;
    -h|--help) sed -n '2,30p' "$0" | sed 's/^# \?//'; exit 0 ;;
    -*) echo "unknown option: $1" >&2; exit 2 ;;
    *)  FILES+=("$1"); shift ;;
  esac
done

[[ ${#FILES[@]} -gt 0 ]] || { echo "ERROR: pass at least one .p12 file (see --help)"; exit 2; }
for bin in pk12util certutil openssl; do
  command -v "$bin" >/dev/null 2>&1 || { echo "ERROR: '$bin' not found (dnf install nss-tools openssl)"; exit 2; }
done

# Locked-down temp workspace, auto-wiped.
umask 077
WORK="$(mktemp -d)"; chmod 700 "$WORK"
cleanup(){ rm -rf "$WORK"; }
trap cleanup EXIT INT TERM
TMPDB="$WORK/nssdb"; mkdir -p "$TMPDB"
certutil -N -d "$TMPDB" --empty-password >/dev/null 2>&1

line(){ printf '%s\n' "======================================================================"; }
sub(){  printf '%s\n' "----------------------------------------------------------------------"; }

# --- read a password silently into a 0600 file, echo the file path ---
# If $2 names a set env var (e.g. P12PASS / NSSPASS) that value is used instead of
# prompting — enables non-interactive/automated runs without a password on the CLI.
read_pw_file(){
  local prompt="$1" envvar="${2:-}" pwf="$WORK/pw.$RANDOM" pw=""
  if [[ -n "$envvar" && -n "${!envvar:-}" ]]; then
    pw="${!envvar}"
  else
    read -rs -p "$prompt" pw </dev/tty; echo >&2
  fi
  printf '%s' "$pw" > "$pwf"; unset pw
  printf '%s' "$pwf"
}

inspect_p12(){
  local f="$1"
  line; echo "FILE: $f"
  [[ -r "$f" ]] || { echo "  !! cannot read file"; return; }

  local pwf; pwf="$(read_pw_file "  PKCS12 password for $(basename "$f"): " P12PASS)"

  # 1) keys + certs actually inside (NSS is authoritative for Dogtag p12s)
  if ! pk12util -l "$f" -d "$TMPDB" -w "$pwf" >"$WORK/list" 2>"$WORK/err"; then
    echo "  !! pk12util could not open it (wrong password?): $(tr '\n' ' ' <"$WORK/err")"
    rm -f "$pwf"; return
  fi
  sub; echo "  Contents (private keys vs certificates):"
  awk '
    /^Key/         { sec="KEY";  next }
    /^Certificate/ { sec="CERT"; next }
    /Friendly Name:/ { sub(/.*Friendly Name:[ \t]*/,""); if(sec=="KEY") k[$0]=1; else c[$0]=1 }
    END{
      print "    PRIVATE KEYS present:"; kn=0
      for(x in k){ print "      * " x; kn++ }
      if(kn==0) print "      (none)"
      print "    CERTIFICATES present:"
      for(x in c) print "      - " x
      print "___KEYCOUNT___ " kn
      for(x in k) if(x ~ /caSigningCert/) print "___HAS_CA_SIGNING___"
    }' "$WORK/list" | tee "$WORK/parsed" | grep -v '^___'

  local has_ca kn
  has_ca=$(grep -c '^___HAS_CA_SIGNING___' "$WORK/parsed")
  kn=$(awk '/^___KEYCOUNT___/{print $2}' "$WORK/parsed")
  sub
  if [[ "$has_ca" -gt 0 ]]; then
    echo "  VERDICT: *** This file CONTAINS the CA SIGNING key (caSigningCert) — crown jewels. ***"
    echo "           This is the CA identity itself. Guard it like a root key; store in Vault, never loose."
  elif [[ "${kn:-0}" -le 1 ]]; then
    echo "  VERDICT: This is a CLIENT / AGENT identity (one key, no CA signing cert)."
    echo "           It logs in to the CA's admin interface; it CANNOT sign certificates."
  else
    echo "  VERDICT: Multiple keys but no caSigningCert — subsystem/service keys only (not the signer)."
  fi

  # 2) classify each certificate (CA vs client) via extensions
  sub; echo "  Per-certificate detail:"
  if ! openssl pkcs12 -in "$f" -nokeys -passin "file:$pwf" >"$WORK/certs.pem" 2>/dev/null || ! grep -q 'BEGIN CERT' "$WORK/certs.pem"; then
    openssl pkcs12 -in "$f" -nokeys -legacy -passin "file:$pwf" >"$WORK/certs.pem" 2>/dev/null || true
  fi
  rm -f "$pwf"
  if grep -q 'BEGIN CERT' "$WORK/certs.pem"; then
    rm -f "$WORK"/c-*.pem
    awk -v d="$WORK" 'BEGIN{n=0} /-----BEGIN CERTIFICATE-----/{n++} {print > (d"/c-"n".pem")}' "$WORK/certs.pem"
    for c in "$WORK"/c-*.pem; do
      [[ -f "$c" ]] || continue
      openssl x509 -in "$c" -noout >/dev/null 2>&1 || continue   # skip non-cert chunks (pkcs12 header text)
      local subj iss bc eku
      subj=$(openssl x509 -in "$c" -noout -subject 2>/dev/null | sed 's/^subject= *//')
      iss=$( openssl x509 -in "$c" -noout -issuer  2>/dev/null | sed 's/^issuer= *//')
      bc=$(  openssl x509 -in "$c" -noout -ext basicConstraints 2>/dev/null | grep -io 'CA:[A-Z]*' | head -1)
      eku=$( openssl x509 -in "$c" -noout -ext extendedKeyUsage 2>/dev/null | grep -ioE 'Client Authentication|Server Authentication|OCSP Signing' | paste -sd', ' -)
      echo "      • subject: $subj"
      echo "          issuer: $iss"
      echo "          ${bc:-CA:?}   $( [[ "$bc" == "CA:TRUE" ]] && echo '(a CA)' || echo '(leaf/client)' )   EKU: ${eku:-none}"
    done
  else
    echo "      (could not extract certs with openssl — use pk12util list above)"
  fi
}

# ---- inspect each p12 ----
for f in "${FILES[@]}"; do inspect_p12 "$f"; done

# ---- prod CA chain level: the fact that decides the test-CA plan ----
line
if [[ -r "$IPA_CA" ]]; then
  echo "PROD CA CHAIN  ($IPA_CA)"
  openssl crl2pkcs7 -nocrl -certfile "$IPA_CA" 2>/dev/null | openssl pkcs7 -print_certs -noout 2>/dev/null > "$WORK/chain.txt" || true
  cat "$WORK/chain.txt"
  n=$(grep -c '^subject=' "$WORK/chain.txt")
  # self-signed if any cert's subject == issuer
  selfsigned=$(paste <(grep '^subject=' "$WORK/chain.txt" | sed 's/^subject= *//') \
                     <(grep '^issuer='  "$WORK/chain.txt" | sed 's/^issuer= *//') \
               | awk -F'\t' '$1==$2{print "yes"}' | head -1)
  sub
  if [[ "$n" -ge 2 ]]; then
    echo "  VERDICT: prod CA is a SUB-CA. Its Issuer (the cert above it) is the ROOT/INTERMEDIATE"
    echo "           that must sign your sibling TEST CA (external-ca). Grab THAT signer, not the Dogtag key."
  elif [[ "$selfsigned" == "yes" ]]; then
    echo "  VERDICT: prod CA is a SELF-SIGNED ROOT (no parent). A sibling test CA would be signed by"
    echo "           this same root key — different plan; tell your agent this result."
  else
    echo "  VERDICT: could not classify — paste the lines above."
  fi
else
  echo "PROD CA CHAIN: $IPA_CA not readable (run on the FreeIPA server, or pass --ipa-ca PATH)"
fi

# ---- optional: confirm live CA private keys in the running NSS DB ----
if [[ -n "$NSSDB" ]]; then
  line; echo "LIVE CA NSS DB  ($NSSDB)  — confirming private keys exist"
  if [[ ! -d "$NSSDB" ]]; then
    echo "  !! not a directory (need root; typical: /var/lib/pki/pki-tomcat/alias)"
  else
    pwf=""
    if [[ -r "$NSSDB/../conf/password.conf" ]]; then
      # Dogtag stores the internal token pw here; extract 'internal=' value into a temp pw file
      awk -F= '/^internal=/{sub(/^internal=/,""); print; exit}' "$NSSDB/../conf/password.conf" > "$WORK/nsspw" 2>/dev/null && pwf="$WORK/nsspw"
    fi
    if [[ -z "$pwf" ]]; then
      pwf="$(read_pw_file "  NSS DB password (blank if none): " NSSPASS)"
    fi
    echo "  Private keys in the live CA database (certutil -K):"
    certutil -K -d "$NSSDB" -f "$pwf" 2>/dev/null | sed 's/^/    /' || echo "    (could not read — wrong password / need sudo)"
    rm -f "$WORK/nsspw"
    echo "  Any line above ending 'caSigningCert cert-pki-ca' proves the live signing key is present."
  fi
fi
line
echo "Done. Nothing above contains private-key material — safe to share the output."
