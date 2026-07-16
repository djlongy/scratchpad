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
CA_MODE=""   # set authoritatively from a signing cert if one is inspected

while [[ $# -gt 0 ]]; do
  case "$1" in
    --ipa-ca) IPA_CA="${2:-}"; shift 2 ;;
    --nssdb)  NSSDB="${2:-}";  shift 2 ;;
    -h|--help) sed -n '2,30p' "$0" | sed 's/^# \?//'; exit 0 ;;
    -*) echo "unknown option: $1" >&2; exit 2 ;;
    *)  FILES+=("$1"); shift ;;
  esac
done

[[ ${#FILES[@]} -gt 0 || -n "$NSSDB" ]] || { echo "ERROR: pass at least one .p12 file, or --nssdb PATH (see --help)"; exit 2; }
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
  elif { : </dev/tty; } 2>/dev/null; then   # /dev/tty openable (interactive)?
    read -rs -p "$prompt" pw </dev/tty; echo >&2
  else
    pw=""   # no usable tty (SSH/automation) and no env var — leave blank rather than erroring
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

    # AUTHORITATIVE CA mode — read the SIGNING cert's own subject vs issuer (not a bundle).
    # Definitive answer to "self-signed vs external-ca"; overrides the /etc/ipa/ca.crt reading.
    if [[ "$has_ca" -gt 0 ]]; then
      local scS="" scI="" cc s bcc
      for cc in "$WORK"/c-*.pem; do
        [[ -f "$cc" ]] || continue
        openssl x509 -in "$cc" -noout >/dev/null 2>&1 || continue
        bcc=$(openssl x509 -in "$cc" -noout -ext basicConstraints 2>/dev/null | grep -io 'CA:TRUE')
        [[ "$bcc" == "CA:TRUE" ]] || continue
        s=$(openssl x509 -in "$cc" -noout -subject 2>/dev/null | sed 's/^subject= *//')
        echo "$s" | grep -qi 'Certificate Authority' || continue
        scS="$s"; scI=$(openssl x509 -in "$cc" -noout -issuer 2>/dev/null | sed 's/^issuer= *//'); break
      done
      if [[ -n "$scS" ]]; then
        sub; echo "  CA MODE (AUTHORITATIVE — from the signing cert itself, not a bundle):"
        if [[ "$(printf %s "$scS" | tr -d ' ')" == "$(printf %s "$scI" | tr -d ' ')" ]]; then
          echo "    SELF-SIGNED ROOT  (subject == issuer) — this CA is its own root."
          CA_MODE="self-signed"
        else
          echo "    EXTERNAL-CA SUB-CA  (subject != issuer)."
          echo "      signed by (root/intermediate): $scI"
          echo "      -> sign your sibling TEST CA's CSR with THAT issuer."
          CA_MODE="external-ca"
        fi
        echo "      (Definitive. If the /etc/ipa/ca.crt section below disagrees, trust THIS line.)"
      fi
    fi
  else
    echo "      (could not extract certs with openssl — use pk12util list above)"
  fi
}

# ---- inspect each p12 ----
for f in "${FILES[@]}"; do inspect_p12 "$f"; done

# ---- /etc/ipa/ca.crt trust bundle — SECONDARY cross-check only ----
# A bundle can hold just an anchor, be stale, or belong to a different box, so it can
# read "self-signed" even when the real signing cert is external. If a signing cert was
# inspected above, THAT (CA_MODE) is authoritative; this is only a cross-check.
line
if [[ -r "$IPA_CA" ]]; then
  echo "TRUST BUNDLE  ($IPA_CA)   [secondary — the CA MODE line above is authoritative]"
  openssl crl2pkcs7 -nocrl -certfile "$IPA_CA" 2>/dev/null | openssl pkcs7 -print_certs -noout 2>/dev/null > "$WORK/chain.txt" || true
  cat "$WORK/chain.txt"
  n=$(grep -c '^subject=' "$WORK/chain.txt")
  selfsigned=$(paste <(grep '^subject=' "$WORK/chain.txt" | sed 's/^subject= *//') \
                     <(grep '^issuer='  "$WORK/chain.txt" | sed 's/^issuer= *//') \
               | awk -F'\t' '$1==$2{print "yes"}' | head -1)
  sub
  bundle=""
  if [[ "$n" -ge 2 ]]; then
    bundle="sub-CA"
    echo "  bundle shows: a SUB-CA + its issuer (root/intermediate) — external-ca."
  elif [[ "$selfsigned" == "yes" ]]; then
    bundle="self-signed"
    echo "  bundle shows: a single SELF-SIGNED cert."
  else
    echo "  bundle: could not classify from these lines."
  fi
  if [[ -n "$CA_MODE" ]]; then
    echo "  AUTHORITATIVE (from the signing cert) = $CA_MODE."
    if { [[ "$CA_MODE" == "external-ca" && "$bundle" == "self-signed" ]] || \
         [[ "$CA_MODE" == "self-signed" && "$bundle" == "sub-CA" ]]; }; then
      echo "  ⚠ bundle DISAGREES with the signing cert — trust the signing cert ($CA_MODE)."
      echo "    (This ca.crt is a bundle/anchor or from a different host; not the CA identity.)"
    fi
  else
    echo "  (No signing cert was inspected, so this bundle is the only signal — for a definitive"
    echo "   answer, run with the cacert.p12 that holds caSigningCert, or --nssdb.)"
  fi
else
  echo "TRUST BUNDLE: $IPA_CA not readable (pass --ipa-ca PATH, or rely on the CA MODE line above)"
fi

# ---- optional: confirm live CA private keys in the running NSS DB ----
if [[ -n "$NSSDB" ]]; then
  line; echo "LIVE CA NSS DB  ($NSSDB)  — confirming private keys exist"
  if [[ ! -d "$NSSDB" ]]; then
    echo "  !! not a directory (need root; typical: /var/lib/pki/pki-tomcat/alias)"
  else
    pwf=""
    # Dogtag stores the NSS token password either as a raw one-liner (conf/pwdfile.txt) or
    # as internal=<pw> in conf/password.conf. Try both (need root to read them).
    if [[ -r "$NSSDB/../conf/pwdfile.txt" ]]; then
      pwf="$NSSDB/../conf/pwdfile.txt"
    elif [[ -r "$NSSDB/pwdfile.txt" ]]; then
      pwf="$NSSDB/pwdfile.txt"
    elif [[ -r "$NSSDB/../conf/password.conf" ]]; then
      awk -F= '/^internal=/{sub(/^internal=/,""); print; exit}' "$NSSDB/../conf/password.conf" > "$WORK/nsspw" 2>/dev/null && pwf="$WORK/nsspw"
    fi
    if [[ -z "$pwf" ]]; then
      pwf="$(read_pw_file "  NSS DB password (blank if none): " NSSPASS)"
    fi
    echo "  Private keys in the live CA database (certutil -K):"
    certutil -K -d "$NSSDB" -f "$pwf" 2>/dev/null | sed 's/^/    /' || echo "    (could not read — wrong password / need sudo)"
    echo "  Any line above ending 'caSigningCert cert-pki-ca' proves the live signing key is present."
    rm -f "$WORK/nsspw"
    # AUTHORITATIVE CA mode straight from the live signing cert (definitive, no bundle involved)
    sub; echo "  CA MODE (AUTHORITATIVE — live caSigningCert subject vs issuer):"
    if certutil -L -d "$NSSDB" -n 'caSigningCert cert-pki-ca' >"$WORK/sc.txt" 2>/dev/null; then
      scs=$(grep -m1 'Subject: ' "$WORK/sc.txt" | sed 's/.*Subject: *//; s/^"//; s/"$//')
      sci=$(grep -m1 'Issuer: '  "$WORK/sc.txt" | sed 's/.*Issuer: *//; s/^"//; s/"$//')
      echo "    Subject: $scs"
      echo "    Issuer : $sci"
      if [[ "$(printf %s "$scs" | tr -d ' ')" == "$(printf %s "$sci" | tr -d ' ')" ]]; then
        echo "    => SELF-SIGNED ROOT."
      else
        echo "    => EXTERNAL-CA SUB-CA. Signed by: $sci"
        echo "       (that issuer is the root/intermediate that signs your sibling test CA)."
      fi
    else
      echo "    (could not read caSigningCert — wrong password / need sudo)"
    fi

    # ---- CA HIERARCHY & KEY MAP — label every CA, mark which private keys are here ----
    line; echo "CA HIERARCHY & KEY MAP  (what each CA is, and whether its key is here)"
    certutil -K -d "$NSSDB" -f "$pwf" 2>/dev/null | sed -n 's/.*[Cc]ertificate DB:[[:space:]]*//p' | sort -u > "$WORK/keynicks"
    certutil -L -d "$NSSDB" 2>/dev/null | sed -E '1,/Trust Attributes/d' \
      | sed -E 's/[[:space:]]+[[:alnum:]]*,[[:alnum:]]*,[[:alnum:]]*[[:space:]]*$//' | sed '/^[[:space:]]*$/d' > "$WORK/certnicks"
    have_root_key="no"; have_ipa_key="no"; root_seen="no"; ipa_seen="no"
    while IFS= read -r nick; do
      [[ -n "$nick" ]] || continue
      certutil -L -d "$NSSDB" -n "$nick" -a >"$WORK/n.pem" 2>/dev/null || continue
      [[ "$(openssl x509 -in "$WORK/n.pem" -noout -ext basicConstraints 2>/dev/null | grep -io 'CA:TRUE')" == "CA:TRUE" ]] || continue
      s=$(openssl x509 -in "$WORK/n.pem" -noout -subject 2>/dev/null | sed 's/^subject= *//')
      i=$(openssl x509 -in "$WORK/n.pem" -noout -issuer  2>/dev/null | sed 's/^issuer= *//')
      haskey="no"; grep -qxF "$nick" "$WORK/keynicks" && haskey="yes"
      ss="no"; [[ "$(printf %s "$s"|tr -d ' ')" == "$(printf %s "$i"|tr -d ' ')" ]] && ss="yes"
      ipa="no"; printf %s "$nick" | grep -q 'caSigningCert cert-pki-ca' && ipa="yes"
      if [[ "$ss" == yes && "$ipa" == yes ]]; then
        label="ROOT CA = your IPA CA (self-signed realm: the IPA CA is its OWN root)"
        root_seen=yes; ipa_seen=yes; [[ "$haskey" == yes ]] && { have_root_key=yes; have_ipa_key=yes; }
      elif [[ "$ss" == yes ]]; then
        label="ROOT CA (top of trust — self-signed)"; root_seen=yes; [[ "$haskey" == yes ]] && have_root_key=yes
      elif [[ "$ipa" == yes ]]; then
        label="IPA CA — your FreeIPA sub-CA (what cacert.p12 holds)"; ipa_seen=yes; [[ "$haskey" == yes ]] && have_ipa_key=yes
      else
        label="INTERMEDIATE CA (a middle layer)"
      fi
      echo "  • [$label]"
      echo "      name      : $nick"
      echo "      subject   : $s"
      echo "      signed by : $i"
      echo "      key here  : $( [[ "$haskey" == yes ]] && echo 'YES  <-- can sign with this' || echo 'no (public cert only)' )"
    done < "$WORK/certnicks"

    sub; echo "  ===> WHAT YOU CAN DO (stand up a test realm trusted by the same endpoints):"
    if [[ "$have_ipa_key" == yes ]]; then
      echo "    PATH B  (sign the test CA with your IPA CA): READY — you hold the IPA CA key."
    else
      echo "    PATH B: IPA CA key not found here (need sudo / correct --nssdb)."
    fi
    if [[ "$have_root_key" == yes && "$ipa_seen" == yes ]]; then
      echo "    PATH A  (isolated sibling under the root): READY HERE — the ROOT key is in this DB."
    elif [[ "$root_seen" == yes ]]; then
      echo "    PATH A  (isolated sibling under the root): you have the root CERT but NOT its key here."
      echo "            -> submit your test CSR to the root's owner to sign, or find the root key file."
    else
      echo "    PATH A: the root's cert isn't in this DB — the root lives OUTSIDE FreeIPA."
      echo "            -> whoever signed this IPA CA at install holds it; submit your CSR to them."
    fi
    echo "    Full step-by-step for each path: see SIGNING.md."
  fi
fi
line
echo "Done. Nothing above contains private-key material — safe to share the output."
