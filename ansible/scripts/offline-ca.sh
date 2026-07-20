#!/usr/bin/env bash
# offline-ca.sh — create and manage the estate's offline CA (system openssl only).
#
# A rare, manual, offline step — run once per root lifetime — that creates:
#
#   root CA (offline, ~25y)  ──signs──▶  issuing CA (imported into HashiCorp
#                                        Vault's PKI engine, ~10y)
#
# plus coldca-parity operations: a subordinate allowlist with expected-subject
# guards, subject-checked CA-CSR signing off the root, replay-safe re-runs,
# chain verification, and inspection. When generation finishes it prints the
# exact commands to escrow the results in Ansible Vault.
#
# Design rules:
#   * No dependencies beyond bash + openssl + POSIX coreutils. No jq, no
#     python, no custom binaries — nothing that needs software vetting.
#   * Secrets never appear on a command line: the CA passphrase comes
#     from $CA_PASSPHRASE or a hidden prompt, and reaches openssl
#     over a dedicated file descriptor (fd 3), never argv and never the child
#     environment. The tool sets the passphrase only as an unexported shell
#     variable — it never exports it, so child processes cannot read it from
#     /proc/<pid>/environ.
#   * Every issuance draws a fresh 128-bit random serial (-set_serial), so no
#     serial-file state is kept and parallel or repeated ceremonies never
#     collide.
#   * Identity guards fail closed: an existing root is never overwritten; the
#     root certificate is fingerprint-pinned at init and re-checked before it
#     signs; and a CSR is only ever signed against an allowlist slot whose
#     expected subject it matches.
#   * A signed certificate reaches its final path only after full self-checks
#     (chain, key binding, CA profile) pass against a temp copy — the final
#     path never holds an unverified cert.

set -euo pipefail

SCRIPT_NAME="$(basename "$0")"
SCRIPT_VERSION="1.4.2"
PKI_DIR="${CA_DIR:-./offline-ca}"
PASS_ENV="CA_PASSPHRASE"
NEW_PASS_ENV="CA_NEW_PASSPHRASE"

# ── output helpers ───────────────────────────────────────────────────────────

if [[ -t 1 ]]; then
  C_BOLD=$'\033[1m'; C_GREEN=$'\033[32m'; C_YELLOW=$'\033[33m'
  C_RED=$'\033[31m'; C_RESET=$'\033[0m'
else
  C_BOLD=""; C_GREEN=""; C_YELLOW=""; C_RED=""; C_RESET=""
fi

info() { printf '%s\n' "${C_GREEN}[ok]${C_RESET} $*"; }
warn() { printf '%s\n' "${C_YELLOW}[!!]${C_RESET} $*" >&2; }
die()  { printf '%s\n' "${C_RED}[error]${C_RESET} $*" >&2; exit 1; }

# Temp files that must be discarded on any exit (including a die). Files that
# succeed are renamed into place first, so cleaning them here is a harmless
# no-op; files stranded by a failure are removed.
_TMP_FILES=()
register_temp() { _TMP_FILES+=("$1"); }
cleanup_temps() {
  (( ${#_TMP_FILES[@]} )) || return 0
  local f
  for f in "${_TMP_FILES[@]}"; do
    [[ -n "$f" && -e "$f" ]] && rm -f "$f"
  done
  return 0
}
trap cleanup_temps EXIT

usage() {
  cat <<EOF
${C_BOLD}${SCRIPT_NAME}${C_RESET} — create and manage the offline CA (root CA + Vault issuing CA)

Backed entirely by the system openssl. Run once per root lifetime (10-25y);
everything downstream (Vault import, FreeIPA signing, leaf issuance) is
automated from the escrowed output.

${C_BOLD}USAGE${C_RESET}
  ${SCRIPT_NAME} <command> [options]

  All commands accept --dir <dir> (default: ./offline-ca, or \$CA_DIR).

  ONE passphrase protects both CA private keys: init creates it, and every
  later command asks for that same passphrase. It is read from
  \$${PASS_ENV} or a hidden prompt — never from arguments, and never exported.

${C_BOLD}COMMANDS${C_RESET}
  setup         First-time setup — runs init + intermediate (+ escrow emit) in
                one go. This is the command for a brand-new CA.
                  --root-subject    "CN=Example Root CA R1,O=Example,C=AU"    (required)
                  --issuing-subject "CN=Example Issuing CA R1,O=Example,C=AU" (required)
                  --root-days N     (default 9131 = ~25y)
                  --issuing-days N  (default 3653 = ~10y)
                  --algo A          rsa4096 | ec384 (default rsa4096)
                init/intermediate below exist separately for RENEWAL: the
                issuing CA is re-minted every ~10y without touching the root.

  init          Mint the root CA (refuses to overwrite an existing root)
                  --subject "CN=Example Root CA R1,O=Example,C=AU"  (required)
                  --days N       validity (default 9131 = ~25y)
                  --algo A       rsa4096 | ec384 (default rsa4096)
                Pins the root's SHA-256 fingerprint into root/config; signing
                operations fail closed if root.crt no longer matches it.

  intermediate  Mint the issuing CA keypair + CSR and sign it under the root
                  --subject "CN=Example Issuing CA R1,O=Example,C=AU" (required)
                  --days N       validity (default 3653 = ~10y)
                  --algo A       rsa4096 | ec384 (default rsa4096)
                  --pathlen N    basicConstraints pathlen (default 1, so it
                                 can sign a subordinate CA such as FreeIPA's)

  sub add       Add a subordinate allowlist slot (what the root may sign)
                  --id NAME --expected-subject "CN=...,O=..." [--days N]
                  (ids match ^[A-Za-z0-9._-]+\$; default validity 7305 = ~20y)
  sub list      Show the allowlist

  inspect FILE  Show a CSR or certificate (subject, issuer, dates, CA/pathlen)
                  [--subordinate NAME]  also check the subject against that
                                        slot; exits 1 on mismatch. When FILE is
                                        a CSR it must be a valid PKCS#10 request
                                        with an adequately strong key.
                  [--format json]       machine-readable verdict

  sign CSR      Sign a subordinate CA CSR off the root — allowlist-guarded,
                replay-safe (an existing matching cert is re-used, never
                re-signed), audit-logged. The CSR must verify as PKCS#10 and
                carry an RSA>=2048 or EC P-256/P-384 key.
                  --subordinate NAME    allowlist slot to sign against (required)
                  [--out FILE]          cert output (default signed/<NAME>.crt)
                  [--chain-out FILE]    cert+root chain (default signed/<NAME>-chain.crt)
                  [--pathlen N]         default 0
                  [--format json]       machine-readable result incl. "replayed"
                  [--force]             re-sign even if a matching cert exists;
                                        the old cert is first backed up to
                                        <out>.bak.<serial>

  verify CERT   Verify a cert chains to the root CA
                  [--csr FILE]          also assert the cert matches this CSR's key
                  [--chain FILE]        supply intermediates

  check-issuer  Read-only root health check: root.crt still matches its pinned
                fingerprint, the passphrase decrypts the key, the key matches
                the certificate, nothing is written
                  [--format json]       fingerprint / subject / not_after

  rotate-passphrase
                Re-encrypt the root (and issuing-CA) keys under a new
                passphrase — verify-first, all-or-nothing: every key is
                re-encrypted to a temp and confirmed to open under the NEW
                passphrase before any key is replaced. Certs and fingerprints
                are unchanged. New passphrase from \$${NEW_PASS_ENV}
                or a double hidden prompt.

  version       Print the tool version

  emit          (Re)write the Ansible-Vault-ready YAML bundle and print the
                escrow instructions. Refuses to overwrite an existing bundle
                (a louder refusal if it is already an ansible-vault escrow);
                writes via a temp file in the same directory then renames.
                  [--out FILE]          default <tree>/bundle/offline_ca.yml
                  [--force]             replace an existing bundle

  status        Show what exists in the CA directory

${C_BOLD}TYPICAL SETUP (one command, once per root lifetime)${C_RESET}
  ${SCRIPT_NAME} setup \\
    --root-subject    "CN=Example Root CA R1,O=Example,C=AU" \\
    --issuing-subject "CN=Example Vault Issuing CA R1,O=Example,C=AU"
  # prompts once to create the CA passphrase, then prints the exact
  # ansible-vault escrow instructions when it finishes.

${C_BOLD}SIGNING A FREEIPA EXTERNAL-CA CSR DIRECTLY OFF THE ROOT${C_RESET}
  ${SCRIPT_NAME} sub add --id ipa --expected-subject "CN=Certificate Authority,O=EXAMPLE.ORG"
  ${SCRIPT_NAME} inspect /path/to/ipa.csr --subordinate ipa
  ${SCRIPT_NAME} sign /path/to/ipa.csr --subordinate ipa
  ${SCRIPT_NAME} verify offline-ca/signed/ipa.crt --csr /path/to/ipa.csr
EOF
}

# ── generic helpers ──────────────────────────────────────────────────────────

need_openssl() {
  command -v openssl >/dev/null 2>&1 || die "openssl not found on PATH"
}

# Runs openssl with stderr captured: silent on success (genpkey/x509 are
# noisy), replayed to the operator on failure so errors stay diagnosable.
ossl() {
  local errf rc=0
  errf="$(mktemp)"
  openssl "$@" 2>"$errf" || rc=$?
  if [[ $rc -ne 0 ]]; then
    cat "$errf" >&2
  fi
  rm -f "$errf"
  return "$rc"
}

# The passphrase reaches openssl over a dedicated file descriptor (fd 3),
# never argv and never the child environment: callers pass "-pass[in] fd:3"
# and attach 3<<<"$(pass_value)". The value lives in an unexported shell var.
pass_value() { printf '%s' "${!PASS_ENV}"; }
new_pass_value() { printf '%s' "${!NEW_PASS_ENV}"; }

# Trims leading/trailing whitespace on stdin.
trim() { sed 's/^[[:space:]]*//; s/[[:space:]]*$//'; }

# Escapes a string for embedding inside a JSON double-quoted scalar: backslash,
# double-quote, and C0 control characters. Pure bash + printf (no jq/python).
# Bytes >= 0x20 (including UTF-8 continuation bytes) pass through verbatim, so
# multibyte subjects reconstruct as valid JSON UTF-8.
json_escape() {
  local s="$1" out="" i c code
  local LC_ALL=C
  for (( i = 0; i < ${#s}; i++ )); do
    c="${s:i:1}"
    case "$c" in
      \\)    out+="\\\\" ;;
      \")    out+="\\\"" ;;
      $'\b') out+='\b' ;;
      $'\f') out+='\f' ;;
      $'\n') out+='\n' ;;
      $'\r') out+='\r' ;;
      $'\t') out+='\t' ;;
      *)
        if [[ "$c" < ' ' ]]; then
          printf -v code '%d' "'$c"
          printf -v c '\\u%04x' "$code"
        fi
        out+="$c"
        ;;
    esac
  done
  printf '%s' "$out"
}

# True when the PEM file is a PKCS#10 request. Accepts both the RFC7468
# "CERTIFICATE REQUEST" label and the legacy "NEW CERTIFICATE REQUEST".
is_csr() {
  grep -Eq 'BEGIN (NEW )?CERTIFICATE REQUEST' "$1"
}

# Normalizes a comma-form DN for order-insensitive comparison:
# split on commas, trim, sort, re-join. RDN values must not contain commas.
dn_normalize() {
  tr ',' '\n' <<<"$1" | trim | LC_ALL=C sort | paste -sd'|' -
}

# Converts "CN=A,O=B,C=AU" to openssl -subj form "/C=AU/O=B/CN=A".
# A DN already in slash form passes through unchanged.
dn_to_slash() {
  local dn="$1"
  [[ "$dn" == /* ]] && { printf '%s' "$dn"; return; }
  [[ "$dn" == *'/'* ]] && die "DN values must not contain '/': $dn"
  local out="" part
  local IFS=','
  read -ra _parts <<<"$dn"
  for ((i = ${#_parts[@]} - 1; i >= 0; i--)); do
    part="$(trim <<<"${_parts[$i]}")"
    [[ "$part" == *=* ]] || die "malformed DN component '${part}' in: $dn"
    out+="/${part}"
  done
  printf '%s' "$out"
}

# Reads a file's subject in RFC2253 form (works for CSRs and certs).
subject_of() {
  local file="$1"
  if is_csr "$file"; then
    openssl req -in "$file" -noout -subject -nameopt RFC2253
  else
    openssl x509 -in "$file" -noout -subject -nameopt RFC2253
  fi | sed 's/^subject=//' | trim
}

pubkey_of() {
  local file="$1"
  if is_csr "$file"; then
    openssl req -in "$file" -noout -pubkey
  else
    openssl x509 -in "$file" -noout -pubkey
  fi
}

# Rejects a CSR that is not a well-formed, adequately strong PKCS#10 request:
#   * self-signature must verify (rejects corrupt or tampered requests)
#   * RSA keys must be >= 2048 bits
#   * EC keys must be on P-256 or P-384
validate_csr() {
  local csr="$1" text algo bits curve
  is_csr "$csr" || die "not a PKCS#10 certificate request (no CERTIFICATE REQUEST PEM block): ${csr}"
  openssl req -in "$csr" -noout -verify >/dev/null 2>&1 \
    || die "CSR self-signature does not verify (corrupt, truncated, or tampered request): ${csr}"
  text="$(openssl req -in "$csr" -noout -text 2>/dev/null)" \
    || die "cannot parse CSR text: ${csr}"
  algo="$(sed -n 's/.*Public Key Algorithm: *//p' <<<"$text" | head -n1)"
  case "$algo" in
    rsaEncryption)
      bits="$(sed -n 's/.*Public-Key: (\([0-9]\{1,\}\) bit).*/\1/p' <<<"$text" | head -n1)"
      [[ -n "$bits" ]] || die "cannot determine RSA key size of ${csr}"
      (( bits >= 2048 )) || die "CSR RSA key is ${bits}-bit — refusing (minimum 2048 bits)"
      ;;
    id-ecPublicKey)
      curve="$(sed -n 's/.*NIST CURVE: *//p' <<<"$text" | head -n1)"
      [[ -n "$curve" ]] || curve="$(sed -n 's/.*ASN1 OID: *//p' <<<"$text" | head -n1)"
      case "$curve" in
        P-256|P-384|prime256v1|secp384r1) : ;;
        *) die "CSR EC curve '${curve:-unknown}' is not allowed — only P-256 or P-384" ;;
      esac
      ;;
    *)
      die "CSR key algorithm '${algo:-unknown}' is neither RSA nor EC — refusing"
      ;;
  esac
}

# Ensures the CA passphrase is available; prompts (hidden) when absent.
# init prompts twice for confirmation; everything else prompts once. The value
# is only ever set as an unexported shell variable — never exported, so child
# processes cannot read it from the environment. A parent may still supply it
# via an exported env var (that exposure is the parent's choice, not ours).
load_passphrase() {
  local confirm="${1:-no}"
  if [[ -n "${!PASS_ENV:-}" ]]; then
    return
  fi
  [[ -t 0 ]] || die "\$${PASS_ENV} is not set and stdin is not a TTY — export it first"
  local p1 p2
  # One passphrase protects BOTH the root and issuing-CA keys. init CREATES it
  # (confirmed twice); every other command asks for that same existing one.
  if [[ "$confirm" == "confirm" ]]; then
    read -rs -p "Create a passphrase to protect the CA private keys: " p1; echo >&2
    [[ -n "$p1" ]] || die "empty passphrase refused"
    read -rs -p "Confirm passphrase: " p2; echo >&2
    [[ "$p1" == "$p2" ]] || die "passphrases do not match"
  else
    read -rs -p "Enter the CA passphrase (the one set during init): " p1; echo >&2
    [[ -n "$p1" ]] || die "empty passphrase refused"
  fi
  printf -v "${PASS_ENV}" '%s' "$p1"
}

# Ensures the NEW passphrase (rotate-passphrase) is available; prompts twice
# when the env var is absent. The value lands in an unexported shell var.
load_new_passphrase() {
  if [[ -n "${!NEW_PASS_ENV:-}" ]]; then
    return
  fi
  [[ -t 0 ]] || die "\$${NEW_PASS_ENV} is not set and stdin is not a TTY — export it first"
  local p1 p2
  read -rs -p "New CA passphrase (replaces the current one for BOTH keys): " p1; echo >&2
  [[ -n "$p1" ]] || die "empty passphrase refused"
  read -rs -p "Confirm new passphrase: " p2; echo >&2
  [[ "$p1" == "$p2" ]] || die "passphrases do not match"
  printf -v "${NEW_PASS_ENV}" '%s' "$p1"
}

# Fails fast (with a clear message) when the passphrase cannot open a key.
check_passphrase() {
  local key="$1"
  openssl pkey -in "$key" -passin fd:3 -noout 2>/dev/null 3<<<"$(pass_value)" \
    || die "cannot unlock ${key} — wrong CA passphrase (it must be the one set during init)"
}

fingerprint_of() {
  openssl x509 -in "$1" -noout -fingerprint -sha256 | cut -d= -f2
}

enddate_of() {
  openssl x509 -in "$1" -noout -enddate | sed 's/^notAfter=//'
}

# Best-effort human-readable "now + N days" in the UTC form openssl prints.
# Tries GNU then BSD date; falls back to a plain phrase.
days_from_now_utc() {
  local days="$1"
  date -u -d "+${days} days" '+%b %e %H:%M:%S %Y GMT' 2>/dev/null \
    || date -u -v "+${days}d" '+%b %e %H:%M:%S %Y GMT' 2>/dev/null \
    || printf 'now + %s days' "$days"
}

# A fresh 128-bit random serial per issuance (no serial-file state is kept).
rand_serial() { printf '0x%s' "$(openssl rand -hex 16)"; }

# Appends one line to the audit log (created 0600 on first write):
#   <utc-timestamp> <action> subordinate=<id> <detail...>
audit_log() {
  local action="$1" slot="$2" detail="${3:-}"
  local logf="${PKI_DIR}/root/audit.log"
  [[ -f "$logf" ]] || { : >"$logf"; chmod 600 "$logf"; }
  printf '%s %s subordinate=%s %s\n' \
    "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$action" "$slot" "$detail" >>"$logf"
}

root_crt()    { printf '%s' "${PKI_DIR}/root/root.crt"; }
root_key()    { printf '%s' "${PKI_DIR}/root/root.key"; }
root_config() { printf '%s' "${PKI_DIR}/root/config"; }
sub_file()    { printf '%s' "${PKI_DIR}/root/subordinates.tsv"; }
int_dir()     { printf '%s' "${PKI_DIR}/intermediate"; }

# Drops a self-ignoring .gitignore into the CA directory so its contents —
# private keys and plaintext escrow bundles — can never be committed, no matter
# which repo the tool is run inside. `*` also ignores the .gitignore itself.
# (A bundle written OUTSIDE the CA directory via `emit --out` is not covered.)
ensure_tree_gitignore() {
  [[ -d "${PKI_DIR}" && ! -f "${PKI_DIR}/.gitignore" ]] || return 0
  cat >"${PKI_DIR}/.gitignore" <<'EOF'
# Written by offline-ca.sh — this directory holds CA PRIVATE KEYS and plaintext
# escrow bundles. Never commit it: the only form that belongs in git is the
# ansible-vault-ENCRYPTED bundle placed in your inventory's group_vars.
*
EOF
}

require_root() {
  ensure_tree_gitignore
  [[ -f "$(root_crt)" && -f "$(root_key)" ]] \
    || die "no root CA in ${PKI_DIR} — run '${SCRIPT_NAME} init' first"
}

# Fails closed if root.crt no longer matches the fingerprint pinned at init.
# A tree whose config predates pinning gets an actionable re-pin one-liner.
verify_root_pin() {
  local cfg pinned actual
  cfg="$(root_config)"
  [[ -f "$cfg" ]] \
    || die "root config ${cfg} is missing — cannot verify the root fingerprint pin; the CA directory is incomplete"
  pinned="$(sed -n 's/^fingerprint=//p' "$cfg" | head -n1)"
  if [[ -z "$pinned" ]]; then
    die "root config ${cfg} predates fingerprint pinning. After confirming root.crt is authentic, re-pin it with:
    printf 'fingerprint=%s\\n' \"\$(openssl x509 -in '$(root_crt)' -noout -fingerprint -sha256 | cut -d= -f2)\" >> '${cfg}'"
  fi
  actual="$(fingerprint_of "$(root_crt)")"
  [[ "$pinned" == "$actual" ]] \
    || die "root.crt fingerprint ${actual} does NOT match the pinned value ${pinned} — the root certificate has been swapped or corrupted; refusing to proceed"
}

# Confirms the subordinate allowlist file exists before any lookup, so a
# deleted/missing tsv is an actionable message rather than a raw grep/awk error.
require_sub_file() {
  local f
  f="$(sub_file)"
  [[ -f "$f" ]] \
    || die "subordinate allowlist ${f} is missing — the CA directory is incomplete or was partially deleted. It is created by '${SCRIPT_NAME} init'; a lone missing allowlist can be recreated with:  touch '${f}'"
}

# Exact (fixed-string) slot presence test. Assumes require_sub_file already ran.
slot_exists() {
  awk -F'\t' -v id="$1" 'BEGIN{rc=1} $1==id{rc=0} END{exit rc}' "$(sub_file)"
}

# Prints field N of the exact-match slot (2 = expected subject, 3 = days).
slot_field() {
  awk -F'\t' -v id="$1" -v n="$2" '$1==id{print $n; exit}' "$(sub_file)"
}

# Rejects slot ids outside the safe character set (also keeps ids usable as
# fixed strings and free of tabs/regex metacharacters).
validate_slot_id() {
  [[ "$1" =~ ^[A-Za-z0-9._-]+$ ]] \
    || die "invalid slot id '$1' — allowed characters: letters, digits, dot, underscore, hyphen"
}

# Prints the v3 extension profiles used for every CA cert this tool mints.
# $1 = pathlen for the subordinate profile.
ext_profiles() {
  local pathlen="$1"
  cat <<EOF
[v3_root]
basicConstraints = critical, CA:true
keyUsage = critical, keyCertSign, cRLSign
subjectKeyIdentifier = hash

[v3_sub]
basicConstraints = critical, CA:true, pathlen:${pathlen}
keyUsage = critical, keyCertSign, cRLSign
subjectKeyIdentifier = hash
authorityKeyIdentifier = keyid,issuer
EOF
}

# Generates an encrypted private key. $1 = out path, $2 = algo. Removes a
# partial key on failure so the overwrite guard does not strand a re-run.
gen_key() {
  local out="$1" algo="$2"
  case "$algo" in
    rsa4096)
      ossl genpkey -algorithm RSA -pkeyopt rsa_keygen_bits:4096 \
        -aes-256-cbc -pass fd:3 -out "$out" 3<<<"$(pass_value)" \
        || { rm -f "$out"; die "openssl genpkey (rsa4096) failed"; } ;;
    ec384)
      ossl genpkey -algorithm EC -pkeyopt ec_paramgen_curve:P-384 \
        -aes-256-cbc -pass fd:3 -out "$out" 3<<<"$(pass_value)" \
        || { rm -f "$out"; die "openssl genpkey (ec384) failed"; } ;;
    *) die "unknown --algo '${algo}' (rsa4096 | ec384)" ;;
  esac
  chmod 600 "$out"
}

# Refuses a child validity that would outlive the root. Uses openssl -checkend
# (portable) for the decision; names both dates in the refusal.
assert_within_root_validity() {
  local days="$1" secs
  [[ "$days" =~ ^[0-9]+$ ]] || die "invalid --days '${days}' — must be a positive integer"
  secs=$(( days * 86400 ))
  if ! openssl x509 -in "$(root_crt)" -noout -checkend "$secs" >/dev/null 2>&1; then
    die "requested validity of ${days} days (child notAfter ~$(days_from_now_utc "$days")) would outlive the root CA (root notAfter $(enddate_of "$(root_crt)")) — reduce --days so the subordinate expires on or before the root"
  fi
}

# Asserts a freshly-signed CA cert carries exactly the profile this tool mints:
# BasicConstraints critical CA:TRUE with the requested pathlen, KeyUsage
# critical {Certificate Sign, CRL Sign} and no others, and no Extended Key
# Usage. $1 = cert, $2 = requested pathlen.
assert_ca_profile() {
  local cert="$1" want_pathlen="$2" text bc ku
  text="$(openssl x509 -in "$cert" -noout -text 2>/dev/null)" \
    || die "profile assert: cannot parse the issued certificate ${cert}"

  grep -qE 'X509v3 Basic Constraints: critical' <<<"$text" \
    || die "profile assert: Basic Constraints is not marked critical"
  bc="$(grep -A1 'X509v3 Basic Constraints' <<<"$text" | tail -n1 | trim)"
  [[ "$bc" == "CA:TRUE, pathlen:${want_pathlen}" ]] \
    || die "profile assert: Basic Constraints is '${bc}', expected 'CA:TRUE, pathlen:${want_pathlen}'"

  grep -qE 'X509v3 Key Usage: critical' <<<"$text" \
    || die "profile assert: Key Usage is not marked critical"
  ku="$(grep -A1 'X509v3 Key Usage' <<<"$text" | tail -n1 | trim)"
  [[ "$ku" == "Certificate Sign, CRL Sign" ]] \
    || die "profile assert: Key Usage is '${ku}', expected 'Certificate Sign, CRL Sign'"

  if grep -q 'X509v3 Extended Key Usage' <<<"$text"; then
    die "profile assert: the issued CA certificate unexpectedly carries an Extended Key Usage"
  fi
}

# Signs $csr with the root key using a fresh random serial. Writes to $out.
# Returns non-zero (and removes a partial output) on openssl failure so the
# caller can clean up its temp and report context.
root_sign_csr() {
  local csr="$1" out="$2" days="$3" pathlen="$4"
  local ext
  ext="$(mktemp)"
  register_temp "$ext"
  ext_profiles "$pathlen" >"$ext"
  if ! ossl x509 -req -in "$csr" \
      -CA "$(root_crt)" -CAkey "$(root_key)" -passin fd:3 \
      -set_serial "$(rand_serial)" -days "$days" -sha384 \
      -extfile "$ext" -extensions v3_sub -out "$out" 3<<<"$(pass_value)"; then
    rm -f "$ext" "$out"
    return 1
  fi
  rm -f "$ext"
}

# Signs $csr off the root into $dest atomically: signs to a 0600 temp beside
# $dest, then verifies the chain, the pubkey binding, and the CA profile
# against the temp; only when all pass is the temp renamed onto $dest. Any
# failure removes the temp and leaves $dest untouched. $1=csr $2=dest $3=days
# $4=pathlen.
sign_verified() {
  local csr="$1" dest="$2" days="$3" pathlen="$4" tmp
  tmp="$(mktemp "${dest}.XXXXXX")"
  register_temp "$tmp"
  chmod 600 "$tmp"
  root_sign_csr "$csr" "$tmp" "$days" "$pathlen" \
    || die "openssl failed to sign ${csr}"
  openssl verify -CAfile "$(root_crt)" "$tmp" >/dev/null 2>&1 \
    || die "self-check failed: the freshly signed cert does not verify against the root"
  [[ "$(pubkey_of "$csr")" == "$(pubkey_of "$tmp")" ]] \
    || die "self-check failed: the signed cert's public key does not match the CSR"
  assert_ca_profile "$tmp" "$pathlen"
  mv "$tmp" "$dest"
}

# ── escrow instructions ──────────────────────────────────────────────────────

print_escrow_instructions() {
  local bundle="$1"
  cat <<EOF

${C_BOLD}── NEXT STEP: encrypt with ansible-vault and commit ────────────────${C_RESET}
The bundle at ${C_BOLD}${bundle}${C_RESET} contains the issuing-CA private key in
PLAINTEXT (the root key inside it stays passphrase-encrypted). Encrypt it
${C_BOLD}now${C_RESET}, before it goes anywhere near git — either way works:

  A) Keep it as its own encrypted vars file:
     1. ansible-vault encrypt '${bundle}'
        (prompts for your vault password unless ansible.cfg supplies one)
     2. head -1 '${bundle}'          # must read \$ANSIBLE_VAULT;1.1;AES256
     3. mv '${bundle}' <your inventory>/group_vars/all/offline_ca.yml

  B) Merge it into an existing encrypted vars file (e.g. vault.yml):
     ansible-vault edit <your inventory>/group_vars/all/vault.yml
     ...and paste the bundle's contents in, then delete '${bundle}'.

  Then commit — the encrypted vars ARE the disaster-recovery escrow: any clone
  of the repo + your ansible-vault password = the entire PKI. (Ansible loads
  every group_vars file in scope, so the file name never matters — only the
  variable names inside.)

Also commit the PUBLIC root cert for trust-store distribution (safe as
plaintext):  $(root_crt)

Keep the CA passphrase (\$${PASS_ENV}) escrowed separately —
password manager + one offline copy. It protects the root key even inside
the encrypted bundle. The CA directory itself (${PKI_DIR}) can then be
archived offline or deleted; the bundle is the system of record.
EOF
}

# ── commands ─────────────────────────────────────────────────────────────────

# First-time setup: root + issuing CA + escrow bundle in one command. init and
# intermediate stay available individually because their lifetimes differ — the
# issuing CA is renewed (intermediate alone, in a fresh --dir or after deliberate
# removal) without ever re-running init on the ~25y root.
cmd_setup() {
  local root_subject="" issuing_subject="" root_days=9131 issuing_days=3653 algo=rsa4096
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --root-subject)    root_subject="$2"; shift 2 ;;
      --issuing-subject) issuing_subject="$2"; shift 2 ;;
      --root-days)       root_days="$2"; shift 2 ;;
      --issuing-days)    issuing_days="$2"; shift 2 ;;
      --algo)            algo="$2"; shift 2 ;;
      --dir)             PKI_DIR="$2"; shift 2 ;;
      *) die "setup: unknown option '$1'" ;;
    esac
  done
  [[ -n "$root_subject" ]] || die "setup: --root-subject is required"
  [[ -n "$issuing_subject" ]] || die "setup: --issuing-subject is required"
  _IN_SETUP=1
  cmd_init --subject "$root_subject" --days "$root_days" --algo "$algo" --dir "$PKI_DIR"
  echo
  cmd_intermediate --subject "$issuing_subject" --days "$issuing_days" --algo "$algo" --dir "$PKI_DIR"
}

cmd_init() {
  local subject="" days=9131 algo=rsa4096
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --subject) subject="$2"; shift 2 ;;
      --days)    days="$2"; shift 2 ;;
      --algo)    algo="$2"; shift 2 ;;
      --dir)     PKI_DIR="$2"; shift 2 ;;
      *) die "init: unknown option '$1'" ;;
    esac
  done
  [[ -n "$subject" ]] || die "init: --subject is required"
  [[ -f "$(root_key)" || -f "$(root_crt)" ]] \
    && die "a root already exists in ${PKI_DIR}/root — refusing to overwrite (this guard is deliberate; use a new --dir for a new root)"

  # Convert (and validate) the DN before creating anything, so a malformed
  # subject cannot strand a key behind the overwrite guard.
  local subj_slash
  subj_slash="$(dn_to_slash "$subject")"

  load_passphrase confirm
  mkdir -p "${PKI_DIR}/root"
  chmod 700 "${PKI_DIR}" "${PKI_DIR}/root"
  ensure_tree_gitignore

  info "generating ${algo} root key (encrypted)"
  gen_key "$(root_key)" "$algo"

  # openssl req takes extensions from its -config file (unlike x509 -extfile),
  # so the v3 profile is appended to the request config.
  local reqcnf
  reqcnf="$(mktemp)"
  register_temp "$reqcnf"
  cat >"$reqcnf" <<EOF
[req]
distinguished_name = req_dn
prompt = no
[req_dn]
EOF
  ext_profiles 1 >>"$reqcnf"
  info "self-signing root certificate (${days} days)"
  if ! ossl req -new -x509 -key "$(root_key)" -passin fd:3 \
      -subj "$subj_slash" -days "$days" -sha384 \
      -config "$reqcnf" -extensions v3_root \
      -out "$(root_crt)" 3<<<"$(pass_value)"; then
    rm -f "$reqcnf" "$(root_key)" "$(root_crt)"
    die "openssl req -x509 failed — partial root files removed so a re-run starts clean"
  fi
  rm -f "$reqcnf"
  touch "$(sub_file)"

  {
    echo "created=$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    echo "subject=${subject}"
    echo "algo=${algo}"
    echo "days=${days}"
    echo "fingerprint=$(fingerprint_of "$(root_crt)")"
  } >"$(root_config)"

  info "root CA minted: $(subject_of "$(root_crt)")"
  info "sha256 fingerprint: $(fingerprint_of "$(root_crt)")"
  warn "record that fingerprint somewhere humans can find it (it authenticates the root forever)"
  info "the passphrase you just set protects BOTH CA keys — 'intermediate', 'sign', 'emit' and 'rotate-passphrase' all ask for this same passphrase"
  echo
  if [[ "${_IN_SETUP:-0}" != "1" ]]; then
    echo "Next: '${SCRIPT_NAME} intermediate --subject ...' then '${SCRIPT_NAME} emit'."
  fi
}

cmd_intermediate() {
  local subject="" days=3653 algo=rsa4096 pathlen=1
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --subject) subject="$2"; shift 2 ;;
      --days)    days="$2"; shift 2 ;;
      --algo)    algo="$2"; shift 2 ;;
      --pathlen) pathlen="$2"; shift 2 ;;
      --dir)     PKI_DIR="$2"; shift 2 ;;
      *) die "intermediate: unknown option '$1'" ;;
    esac
  done
  [[ -n "$subject" ]] || die "intermediate: --subject is required"
  require_root
  local d
  d="$(int_dir)"
  [[ -f "${d}/int.key" ]] \
    && die "an issuing CA already exists at ${d} — refusing to overwrite (mint a renewal into a new --dir, or remove it deliberately first)"

  local subj_slash
  subj_slash="$(dn_to_slash "$subject")"

  load_passphrase
  check_passphrase "$(root_key)"
  mkdir -p "$d"
  chmod 700 "$d"

  info "generating ${algo} issuing-CA key (encrypted)"
  gen_key "${d}/int.key" "$algo"

  info "creating CSR"
  ossl req -new -key "${d}/int.key" -passin fd:3 \
    -subj "$subj_slash" -sha384 -out "${d}/int.csr" 3<<<"$(pass_value)" \
    || die "openssl req failed creating the issuing-CA CSR"

  assert_within_root_validity "$days"

  info "signing under the root (${days} days, pathlen:${pathlen})"
  sign_verified "${d}/int.csr" "${d}/int.crt" "$days" "$pathlen"
  cat "${d}/int.crt" "$(root_crt)" >"${d}/chain.crt"

  info "issuing CA minted: $(subject_of "${d}/int.crt")"
  cmd_emit --dir "$PKI_DIR"
}

cmd_sub() {
  local action="${1:-}"; shift || true
  case "$action" in
    add)
      local id="" expected="" days=7305
      while [[ $# -gt 0 ]]; do
        case "$1" in
          --id)               id="$2"; shift 2 ;;
          --expected-subject) expected="$2"; shift 2 ;;
          --days)             days="$2"; shift 2 ;;
          --dir)              PKI_DIR="$2"; shift 2 ;;
          *) die "sub add: unknown option '$1'" ;;
        esac
      done
      require_root
      [[ -n "$id" && -n "$expected" ]] || die "sub add: --id and --expected-subject are required"
      validate_slot_id "$id"
      [[ "$expected" == *$'\t'* ]] && die "sub add: tabs are not allowed in the expected subject"
      local f
      f="$(sub_file)"
      if [[ -f "$f" ]] && slot_exists "$id"; then
        local existing
        existing="$(slot_field "$id" 2)"
        if [[ "$(dn_normalize "$existing")" == "$(dn_normalize "$expected")" ]]; then
          info "slot '${id}' already present with the same expected subject (no-op)"
          return 0
        fi
        die "slot '${id}' already exists with a DIFFERENT expected subject (${existing}) — refusing to change it silently"
      fi
      printf '%s\t%s\t%s\n' "$id" "$expected" "$days" >>"$f"
      info "slot '${id}' added: expected subject '${expected}', validity ${days} days"
      ;;
    list)
      while [[ $# -gt 0 ]]; do
        case "$1" in
          --dir) PKI_DIR="$2"; shift 2 ;;
          *) die "sub list: unknown option '$1'" ;;
        esac
      done
      require_root
      if [[ ! -s "$(sub_file)" ]]; then
        echo "no subordinate slots defined"
        return 0
      fi
      printf '%-16s %-60s %s\n' "ID" "EXPECTED SUBJECT" "DAYS"
      awk -F'\t' '{ printf "%-16s %-60s %s\n", $1, $2, $3 }' "$(sub_file)"
      ;;
    *) die "sub: expected 'add' or 'list'" ;;
  esac
}

cmd_inspect() {
  local file="" slot="" format="text"
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --subordinate) slot="$2"; shift 2 ;;
      --format)      format="$2"; shift 2 ;;
      --dir)         PKI_DIR="$2"; shift 2 ;;
      -*)            die "inspect: unknown option '$1'" ;;
      *)             file="$1"; shift ;;
    esac
  done
  [[ -n "$file" ]] || die "inspect: a CSR or certificate path is required"
  [[ -f "$file" ]] || die "inspect: no such file: ${file}"

  local kind subject
  if is_csr "$file"; then kind=csr; else kind=certificate; fi
  subject="$(subject_of "$file")"

  local matches=""
  if [[ -n "$slot" ]]; then
    require_root
    require_sub_file
    validate_slot_id "$slot"
    slot_exists "$slot" \
      || die "inspect: no subordinate slot '${slot}' (see '${SCRIPT_NAME} sub list')"
    [[ "$kind" == "csr" ]] && validate_csr "$file"
    local expected
    expected="$(slot_field "$slot" 2)"
    if [[ "$(dn_normalize "$subject")" == "$(dn_normalize "$expected")" ]]; then
      matches=true
    else
      matches=false
    fi
  fi

  if [[ "$format" == "json" ]]; then
    printf '{"kind": "%s", "subject": "%s"' "$(json_escape "$kind")" "$(json_escape "$subject")"
    [[ -n "$matches" ]] && printf ', "subordinate": "%s", "matches_expected_subject": %s' "$(json_escape "$slot")" "$matches"
    printf '}\n'
  else
    echo "kind:    ${kind}"
    echo "subject: ${subject}"
    if [[ "$kind" == "certificate" ]]; then
      echo "issuer:  $(openssl x509 -in "$file" -noout -issuer -nameopt RFC2253 | sed 's/^issuer=//' | trim)"
      echo "dates:   $(openssl x509 -in "$file" -noout -startdate -enddate | paste -sd' ' -)"
      openssl x509 -in "$file" -noout -text | grep -A1 'Basic Constraints' | tail -1 | trim | sed 's/^/CA:      /'
    fi
    [[ -n "$matches" ]] && echo "matches expected subject for '${slot}': ${matches}"
  fi
  [[ "$matches" == "false" ]] && exit 1
  return 0
}

# Prints one sign result. $1 = replayed true|false, $2 = cert, $3 = chain,
# $4 = slot, $5 = format.
sign_result() {
  local replayed="$1" cert="$2" chain="$3" slot="$4" format="$5"
  local serial
  serial="$(openssl x509 -in "$cert" -noout -serial | cut -d= -f2)"
  if [[ "$format" == "json" ]]; then
    printf '{"replayed": %s, "subordinate": "%s", "subject": "%s", "serial_hex": "%s", "fingerprint_sha256": "%s", "not_after": "%s", "certificate": "%s", "chain": "%s"}\n' \
      "$replayed" "$(json_escape "$slot")" "$(json_escape "$(subject_of "$cert")")" \
      "$(json_escape "$serial")" "$(json_escape "$(fingerprint_of "$cert")")" \
      "$(json_escape "$(enddate_of "$cert")")" "$(json_escape "$cert")" "$(json_escape "$chain")"
    return 0
  fi
  if [[ "$replayed" == "true" ]]; then
    info "replayed: existing certificate for '${slot}' matches this CSR (cert: ${cert}, chain: ${chain})"
  else
    info "signed: $(subject_of "$cert") (serial ${serial})"
    info "cert:  ${cert}"
    info "chain: ${chain}"
  fi
}

cmd_sign() {
  local csr="" slot="" out="" chain_out="" pathlen=0 force=no days_override="" format="text"
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --subordinate) slot="$2"; shift 2 ;;
      --out)         out="$2"; shift 2 ;;
      --chain-out)   chain_out="$2"; shift 2 ;;
      --pathlen)     pathlen="$2"; shift 2 ;;
      --days)        days_override="$2"; shift 2 ;;
      --format)      format="$2"; shift 2 ;;
      --force)       force=yes; shift ;;
      --dir)         PKI_DIR="$2"; shift 2 ;;
      -*)            die "sign: unknown option '$1'" ;;
      *)             csr="$1"; shift ;;
    esac
  done
  [[ -n "$csr" ]] || die "sign: a CSR path is required"
  [[ -f "$csr" ]] || die "sign: no such file: ${csr}"
  [[ -n "$slot" ]] || die "sign: --subordinate is required (the allowlist slot to sign against)"
  require_root
  verify_root_pin
  require_sub_file
  validate_slot_id "$slot"
  slot_exists "$slot" \
    || die "sign: no subordinate slot '${slot}' — add it first: ${SCRIPT_NAME} sub add --id ${slot} --expected-subject '...'"
  validate_csr "$csr"

  local expected days
  expected="$(slot_field "$slot" 2)"
  days="$(slot_field "$slot" 3)"
  [[ -n "$days_override" ]] && days="$days_override"

  local subject
  subject="$(subject_of "$csr")"
  if [[ "$(dn_normalize "$subject")" != "$(dn_normalize "$expected")" ]]; then
    audit_log sign_refused "$slot" "csr_subject=${subject}"
    die "sign: CSR subject '${subject}' does not match slot '${slot}' expected subject '${expected}' — refusing to sign a wrong-identity CA"
  fi

  [[ -n "$out" ]] || out="${PKI_DIR}/signed/${slot}.crt"
  [[ -n "$chain_out" ]] || chain_out="${PKI_DIR}/signed/${slot}-chain.crt"
  mkdir -p "$(dirname "$out")" "$(dirname "$chain_out")"

  # Replay guard: an existing cert for the same key + subject that still
  # chains to the root is re-used, not re-signed.
  if [[ -f "$out" && "$force" == "no" ]]; then
    if [[ "$(pubkey_of "$csr")" == "$(pubkey_of "$out")" ]] \
      && [[ "$(dn_normalize "$(subject_of "$out")")" == "$(dn_normalize "$subject")" ]] \
      && openssl verify -CAfile "$(root_crt)" "$out" >/dev/null 2>&1; then
      cat "$out" "$(root_crt)" >"$chain_out"
      audit_log sign_replayed "$slot" "cert=${out}"
      sign_result true "$out" "$chain_out" "$slot" "$format"
      return 0
    fi
    audit_log sign_refused "$slot" "existing_cert_mismatch=${out}"
    die "sign: ${out} exists but does NOT match this CSR — refusing to overwrite (inspect it, then use --force or a different --out)"
  fi

  load_passphrase
  check_passphrase "$(root_key)"
  assert_within_root_validity "$days"

  # Forced re-sign: back the existing cert up before it is replaced.
  if [[ "$force" == "yes" && -f "$out" ]]; then
    local old_serial
    old_serial="$(openssl x509 -in "$out" -noout -serial 2>/dev/null | cut -d= -f2 || true)"
    [[ -n "$old_serial" ]] || old_serial="unknown"
    cp -p "$out" "${out}.bak.${old_serial}"
    audit_log force_resign "$slot" "backed_up=${out}.bak.${old_serial}"
  fi

  [[ "$format" == "json" ]] || info "signing '${slot}' (${days} days, pathlen:${pathlen})"
  audit_log sign_attempt "$slot" "csr_subject=${subject}"
  sign_verified "$csr" "$out" "$days" "$pathlen"
  cat "$out" "$(root_crt)" >"$chain_out"
  audit_log sign_committed "$slot" "serial=$(openssl x509 -in "$out" -noout -serial | cut -d= -f2)"
  sign_result false "$out" "$chain_out" "$slot" "$format"
}

cmd_check() {
  local format="text"
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --format) format="$2"; shift 2 ;;
      --dir)    PKI_DIR="$2"; shift 2 ;;
      *) die "check-issuer: unknown option '$1'" ;;
    esac
  done
  require_root
  verify_root_pin
  load_passphrase
  check_passphrase "$(root_key)"

  # The decrypted key must actually match the root certificate.
  local kpub cpub
  kpub="$(openssl pkey -in "$(root_key)" -passin fd:3 -pubout 2>/dev/null 3<<<"$(pass_value)")" \
    || die "check-issuer: could not derive the public key"
  cpub="$(openssl x509 -in "$(root_crt)" -noout -pubkey)"
  [[ "$kpub" == "$cpub" ]] \
    || die "check-issuer: root.key does NOT match root.crt — the tree is inconsistent"

  if [[ "$format" == "json" ]]; then
    printf '{"subject": "%s", "fingerprint_sha256": "%s", "not_after": "%s"}\n' \
      "$(json_escape "$(subject_of "$(root_crt)")")" \
      "$(json_escape "$(fingerprint_of "$(root_crt)")")" \
      "$(json_escape "$(enddate_of "$(root_crt)")")"
  else
    info "issuer loads: root.crt matches its pin, the passphrase decrypts the key, and the key matches the certificate"
    echo "subject:     $(subject_of "$(root_crt)")"
    echo "fingerprint: $(fingerprint_of "$(root_crt)")"
    echo "not_after:   $(enddate_of "$(root_crt)")"
  fi
}

cmd_rotate() {
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --dir) PKI_DIR="$2"; shift 2 ;;
      *) die "rotate-passphrase: unknown option '$1'" ;;
    esac
  done
  require_root
  load_passphrase
  load_new_passphrase

  # Verify first: every key must unlock under the CURRENT passphrase before any
  # file is touched.
  check_passphrase "$(root_key)"
  local -a keys=( "$(root_key)" )
  if [[ -f "$(int_dir)/int.key" ]]; then
    check_passphrase "$(int_dir)/int.key"
    keys+=( "$(int_dir)/int.key" )
  fi

  # Phase 1 — re-encrypt every key to a 0600 temp beside it and confirm the
  # temp opens under the NEW passphrase. Nothing in place is changed yet, so a
  # failure here (any key) leaves ALL keys on the old passphrase.
  local -a temps=()
  local key tmp
  for key in "${keys[@]}"; do
    tmp="$(mktemp "${key}.rotate.XXXXXX")"
    register_temp "$tmp"
    chmod 600 "$tmp"
    if ! openssl pkey -in "$key" -passin fd:3 -aes-256-cbc -passout fd:4 -out "$tmp" \
        2>/dev/null 3<<<"$(pass_value)" 4<<<"$(new_pass_value)"; then
      die "rotate-passphrase: re-encrypting ${key} failed — nothing was changed"
    fi
    if ! openssl pkey -in "$tmp" -passin fd:3 -noout 2>/dev/null 3<<<"$(new_pass_value)"; then
      die "rotate-passphrase: the re-encrypted ${key} does not open under the new passphrase — nothing was changed"
    fi
    temps+=( "$tmp" )
  done

  # Phase 2 — every temp verified; commit them all.
  local i
  for i in "${!keys[@]}"; do
    mv "${temps[$i]}" "${keys[$i]}"
  done

  info "root key re-encrypted under the new passphrase (cert + fingerprint unchanged)"
  local rotated="root"
  if (( ${#keys[@]} > 1 )); then
    rotated="root,intermediate"
    info "issuing-CA key re-encrypted under the new passphrase"
  fi
  audit_log rotate_passphrase - "keys=${rotated}"

  cat <<EOF

next steps:
  1. Update whichever store holds the CA passphrase (password manager,
     offline copy) — the OLD passphrase no longer opens anything.
  2. Re-run '${SCRIPT_NAME} emit' with the NEW passphrase and re-encrypt the
     bundle with ansible-vault: the escrowed pki_root_ca_key_encrypted
     ciphertext still expects the OLD passphrase until you do.
EOF
}

cmd_verify() {
  local cert="" csr="" chain=""
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --csr)   csr="$2"; shift 2 ;;
      --chain) chain="$2"; shift 2 ;;
      --dir)   PKI_DIR="$2"; shift 2 ;;
      -*)      die "verify: unknown option '$1'" ;;
      *)       cert="$1"; shift ;;
    esac
  done
  [[ -n "$cert" ]] || die "verify: a certificate path is required"
  [[ -f "$cert" ]] || die "verify: no such file: ${cert}"
  require_root

  local -a vargs=(-CAfile "$(root_crt)")
  [[ -n "$chain" ]] && vargs+=(-untrusted "$chain")
  if openssl verify "${vargs[@]}" "$cert" >/dev/null 2>&1; then
    info "chain OK: $(subject_of "$cert") chains to $(subject_of "$(root_crt)")"
  else
    die "verify FAILED: ${cert} does not chain to the root CA"
  fi

  if [[ -n "$csr" ]]; then
    [[ -f "$csr" ]] || die "verify: no such CSR: ${csr}"
    [[ "$(pubkey_of "$csr")" == "$(pubkey_of "$cert")" ]] \
      || die "verify FAILED: certificate public key does not match the CSR"
    info "key OK: certificate matches the CSR public key"
  fi
}

cmd_emit() {
  local out="" force=no
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --out)   out="$2"; shift 2 ;;
      --force) force=yes; shift ;;
      --dir)   PKI_DIR="$2"; shift 2 ;;
      *) die "emit: unknown option '$1'" ;;
    esac
  done
  require_root
  local d
  d="$(int_dir)"
  [[ -f "${d}/int.crt" ]] || die "emit: no issuing CA yet — run '${SCRIPT_NAME} intermediate' first"
  [[ -n "$out" ]] || out="${PKI_DIR}/bundle/offline_ca.yml"
  mkdir -p "$(dirname "$out")"

  # Clobber guard: never truncate an existing bundle unless forced. A bundle
  # that is already an ansible-vault escrow is the disaster-recovery copy for
  # the whole PKI — overwriting it is the loudest refusal.
  if [[ -e "$out" && "$force" == "no" ]]; then
    local firstline=""
    [[ -f "$out" ]] && firstline="$(head -n1 "$out" 2>/dev/null || true)"
    if [[ "$firstline" == "\$ANSIBLE_VAULT"* ]]; then
      die "emit: ${out} already exists AND begins with \$ANSIBLE_VAULT — it is an ENCRYPTED ESCROW BUNDLE. Overwriting it DESTROYS the disaster-recovery escrow for the entire PKI. Refusing. Move it aside deliberately, or pass --force to replace it (the existing encrypted escrow will be lost)."
    fi
    die "emit: ${out} already exists — refusing to overwrite. Inspect it, then pass --force to replace it (atomic temp+rename), or --out <path> to write elsewhere."
  fi

  load_passphrase
  check_passphrase "${d}/int.key"

  # The issuing-CA key is emitted in plaintext because the automated Vault
  # import cannot prompt for a passphrase; the bundle's protection layer is
  # the ansible-vault encryption applied in the next step. The root key stays
  # passphrase-encrypted even inside the bundle.
  local int_key_plain
  int_key_plain="$(openssl pkey -in "${d}/int.key" -passin fd:3 2>/dev/null 3<<<"$(pass_value)")" \
    || die "emit: failed to decrypt the issuing-CA key"

  # Assemble into a temp beside the target, then rename — the destination is
  # never a half-written bundle, and the clobber guard above already vetted it.
  local tmp
  tmp="$(mktemp "${out}.XXXXXX")"
  register_temp "$tmp"
  chmod 600 "$tmp"
  if ! {
    echo "---"
    echo "# Offline-CA escrow bundle — generated $(date -u +%Y-%m-%dT%H:%M:%SZ) by ${SCRIPT_NAME}."
    echo "# ENCRYPT WITH ansible-vault BEFORE COMMITTING — pki_issuing_ca_key is plaintext."
    echo "# The root key below remains protected by the CA passphrase."
    echo
    echo "# Public root certificate — the trust anchor every host and client pins."
    echo "# Used for: OS trust stores (e.g. copy to /etc/pki/ca-trust/source/anchors/"
    echo "# && update-ca-trust), and as the -CAfile in any chain verification"
    echo "# (openssl verify -CAfile root.crt -untrusted chain.crt cert.crt)."
    echo "pki_root_ca_cert: |"
    sed 's/^/  /' "$(root_crt)"
    echo
    echo "# Root private key — PKCS#8, encrypted with the CA passphrase; automation"
    echo "# never decrypts it. Used for: disaster recovery and issuing-CA renewal —"
    echo "# write this block back to <dir>/root/root.key (and the cert above to"
    echo "# <dir>/root/root.crt) and ${SCRIPT_NAME}'s sign / intermediate / verify"
    echo "# commands work again from any machine with bash + openssl."
    echo "pki_root_ca_key_encrypted: |"
    sed 's/^/  /' "$(root_key)"
    echo
    echo "# Issuing CA certificate — imported into HashiCorp Vault's PKI engine"
    echo "# together with its key below, as one bundle:"
    echo "# (cat issuing.crt issuing.key | vault write <mount>/issuers/import/bundle pem_bundle=-)."
    echo "# Vault then signs everything day-to-day; the root stays offline."
    echo "pki_issuing_ca_cert: |"
    sed 's/^/  /' "${d}/int.crt"
    echo
    echo "# Issuing CA private key — plaintext INSIDE this ansible-vault-encrypted file"
    echo "# because Vault's import cannot prompt for a passphrase; it is the other half"
    echo "# of the pem_bundle import above. Never store or commit it unencrypted."
    echo "pki_issuing_ca_key: |"
    printf '%s\n' "$int_key_plain" | sed 's/^/  /'
    echo
    echo "# Issuing CA cert + root, in order — the intermediate chain servers present"
    echo "# alongside their leaf certs, and the -untrusted file in openssl verify."
    echo "pki_issuing_ca_chain: |"
    sed 's/^/  /' "${d}/chain.crt"
  } >"$tmp"; then
    die "emit: failed to assemble the bundle"
  fi
  chmod 600 "$tmp"
  mv "$tmp" "$out"

  info "bundle written: ${out} (mode 0600)"
  print_escrow_instructions "$out"
}

cmd_status() {
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --dir) PKI_DIR="$2"; shift 2 ;;
      *) die "status: unknown option '$1'" ;;
    esac
  done
  ensure_tree_gitignore
  echo "CA directory: ${PKI_DIR}"
  if [[ -f "$(root_crt)" ]]; then
    echo "root:         $(subject_of "$(root_crt)")"
    echo "              $(openssl x509 -in "$(root_crt)" -noout -enddate | sed 's/notAfter=/expires /')"
  else
    echo "root:         (none — run '${SCRIPT_NAME} init')"
  fi
  if [[ -f "$(int_dir)/int.crt" ]]; then
    echo "issuing CA:   $(subject_of "$(int_dir)/int.crt")"
    echo "              $(openssl x509 -in "$(int_dir)/int.crt" -noout -enddate | sed 's/notAfter=/expires /')"
  else
    echo "issuing CA:   (none — run '${SCRIPT_NAME} intermediate')"
  fi
  if [[ -s "$(sub_file)" ]]; then
    echo "allowlist:    $(wc -l <"$(sub_file)" | trim) slot(s) — '${SCRIPT_NAME} sub list'"
  else
    echo "allowlist:    (empty)"
  fi
  # find exits non-zero when signed/ does not exist yet; guard so set -e (with
  # pipefail) cannot abort status on a tree that has never signed anything.
  local signed=0
  if [[ -d "${PKI_DIR}/signed" ]]; then
    signed="$(find "${PKI_DIR}/signed" -name '*.crt' ! -name '*-chain.crt' | wc -l | trim)"
  fi
  echo "signed certs: ${signed}"
  if [[ -f "${PKI_DIR}/bundle/offline_ca.yml" ]]; then
    if head -1 "${PKI_DIR}/bundle/offline_ca.yml" | grep -q ANSIBLE_VAULT; then
      echo "bundle:       ${PKI_DIR}/bundle/offline_ca.yml (ansible-vault ENCRYPTED)"
    else
      echo "bundle:       ${PKI_DIR}/bundle/offline_ca.yml (${C_YELLOW}PLAINTEXT — encrypt it${C_RESET})"
    fi
  else
    echo "bundle:       (none — run '${SCRIPT_NAME} emit')"
  fi
}

# ── dispatch ─────────────────────────────────────────────────────────────────

main() {
  need_openssl
  local cmd="${1:-}"
  [[ -n "$cmd" ]] || { usage; exit 0; }
  shift || true
  case "$cmd" in
    setup)        cmd_setup "$@" ;;
    init)         cmd_init "$@" ;;
    intermediate) cmd_intermediate "$@" ;;
    sub)          cmd_sub "$@" ;;
    inspect)      cmd_inspect "$@" ;;
    sign)         cmd_sign "$@" ;;
    verify)       cmd_verify "$@" ;;
    emit)         cmd_emit "$@" ;;
    status)       cmd_status "$@" ;;
    check-issuer|check) cmd_check "$@" ;;
    rotate-passphrase)  cmd_rotate "$@" ;;
    version)      echo "${SCRIPT_NAME} ${SCRIPT_VERSION}" ;;
    -h|--help|help) usage ;;
    *) usage; die "unknown command '${cmd}'" ;;
  esac
}

main "$@"
