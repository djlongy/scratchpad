#!/usr/bin/env bash
# Upload every *.whl in a directory to a GitLab project's PyPI package registry.
#
# Needs: bash 3.2+, curl, python3. No twine, no venv.
#
# Config (env):
#   WHEEL_DIR            directory to scan (default ./packages); $1 overrides it
#   DRY_RUN=1            list what would happen, upload nothing
#   PUBLIC_PULL=1        set package_registry_access_level=public on the project
#                        so anonymous `pip install` works on a private project.
#                        Needs a Maintainer token: a CI_JOB_TOKEN gets HTTP 401 on PUT
#                        /projects/:id and the script fails loudly on that.
#
#   Inside GitLab CI (auto-detected): CI_API_V4_URL, CI_PROJECT_ID, CI_JOB_TOKEN
#   Anywhere else:                    GITLAB_URL, GITLAB_TOKEN, and
#                                     GITLAB_PROJECT_ID or GITLAB_PROJECT_PATH
#   GITLAB_TOKEN, when set, wins over CI_JOB_TOKEN (e.g. a Maintainer token
#   stored as a CI variable so PUBLIC_PULL can run in a pipeline).
#
# Wheels whose filename is already in the registry are skipped (GitLab's PyPI
# endpoint does not honour twine --skip-existing; it answers 400 "File name has
# already been taken"). Skips exit 0.
#
# TLS: a valid CA-signed cert needs nothing. Only for a private/self-signed CA
# point CURL_CA_BUNDLE at the CA file (curl reads it natively) and give pip
# --cert (never --trusted-host unless you accept no verification at all).
set -euo pipefail

WHEEL_DIR=${1:-${WHEEL_DIR:-./packages}}
DRY_RUN=${DRY_RUN:-0}
PUBLIC_PULL=${PUBLIC_PULL:-0}

die() { printf 'error: %s\n' "$*" >&2; exit 1; }
log() { printf '%s\n' "$*"; }

# --- resolve endpoint + credentials -------------------------------------------
if [ -n "${GITLAB_TOKEN:-}" ]; then
  api=${GITLAB_URL:+${GITLAB_URL%/}/api/v4}
  api=${api:-${CI_API_V4_URL:-}}
  [ -n "$api" ] || die "GITLAB_URL is required with GITLAB_TOKEN"
  token=$GITLAB_TOKEN
  auth_header="PRIVATE-TOKEN: $token"
  basic_user=${GITLAB_USER:-__token__}
  project=${GITLAB_PROJECT_ID:-${CI_PROJECT_ID:-}}
  if [ -z "$project" ]; then
    [ -n "${GITLAB_PROJECT_PATH:-}" ] || die "set GITLAB_PROJECT_ID or GITLAB_PROJECT_PATH"
    project=$(python3 -c 'import sys,urllib.parse;print(urllib.parse.quote(sys.argv[1],safe=""))' "$GITLAB_PROJECT_PATH")
  fi
elif [ -n "${CI_JOB_TOKEN:-}" ]; then
  api=${CI_API_V4_URL:-}
  project=${CI_PROJECT_ID:-}
  [ -n "$api" ] && [ -n "$project" ] || die "CI_API_V4_URL and CI_PROJECT_ID are required with CI_JOB_TOKEN"
  token=$CI_JOB_TOKEN
  auth_header="JOB-TOKEN: $token"
  basic_user=gitlab-ci-token
else
  die "no credentials: set GITLAB_TOKEN (+GITLAB_URL, GITLAB_PROJECT_ID|GITLAB_PROJECT_PATH) or run inside GitLab CI"
fi
[ -n "${CI_SERVER_TLS_CA_FILE:-}" ] && export CURL_CA_BUNDLE=${CURL_CA_BUNDLE:-$CI_SERVER_TLS_CA_FILE}

# Never put the token on a command line that ps can see: curl reads it from a file.
umask 077
netrc_like=$(mktemp)
trap 'rm -rf "$netrc_like" "$netrc_like.pypi" "$netrc_like.resp" "$simple_dir"' EXIT
simple_dir=
printf 'header = "%s"\n' "$auth_header" > "$netrc_like"
printf 'user = "%s:%s"\n' "$basic_user" "$token" > "$netrc_like.pypi"

resp=$netrc_like.resp
api_call() { # method path [curl args...] -> status in $http_code, body in $resp
  local method=$1 path=$2; shift 2
  http_code=$(curl -sS -K "$netrc_like" -o "$resp" -w '%{http_code}' -X "$method" "$api$path" "$@")
}

# --- PUBLIC_PULL ----------------------------------------------------------------
if [ "$PUBLIC_PULL" = 1 ]; then
  if [ "$DRY_RUN" = 1 ]; then
    log "dry-run: would PUT /projects/$project package_registry_access_level=public"
  else
    api_call PUT "/projects/$project" --data package_registry_access_level=public
    [ "$http_code" = 200 ] || die "PUBLIC_PULL: PUT /projects/$project returned HTTP $http_code (needs a Maintainer token; a CI_JOB_TOKEN cannot do this): $(cat "$resp")"
    log "package registry access level is now: $(python3 -c 'import sys,json;print(json.load(sys.stdin).get("package_registry_access_level"))' < "$resp")"
  fi
fi

# --- collect wheels ---------------------------------------------------------------
[ -d "$WHEEL_DIR" ] || die "no such directory: $WHEEL_DIR"
set -- "$WHEEL_DIR"/*.whl
[ -e "$1" ] || { log "no wheels in $WHEEL_DIR, nothing to do"; exit 0; }

# --- existence check ----------------------------------------------------------------
# The simple index page for a name lists every file the registry holds for it,
# so the check is per filename: GitLab groups several wheels (cp39, cp312, ...)
# under one name==version, and a name==version check would skip a new build of
# a version that is already there. 302 (pypi.org forwarding) and 404 both mean
# the name is not in the registry.
simple_dir=$(mktemp -d)
published() { # wheel path -> 0 if that exact filename is already in the registry
  local page=$simple_dir/$norm_name.html
  if [ ! -f "$page" ]; then
    http_code=$(curl -sS -K "$netrc_like.pypi" -o "$page" -w '%{http_code}' "$api/projects/$project/packages/pypi/simple/$norm_name/")
    case $http_code in
      200) ;;
      302|404) : > "$page" ;;
      *) die "GET simple/$norm_name/ returned HTTP $http_code: $(cat "$page")" ;;
    esac
  fi
  grep -qF "$(basename "$1")" "$page"
}

# --- upload -------------------------------------------------------------------------
rc=0
for whl in "$@"; do
  stem=${whl##*/}; stem=${stem%.whl}
  # PEP 427: {name}-{version}[-{build}]-{python}-{abi}-{platform}; name/version never contain '-'.
  nf=$(printf '%s' "$stem" | awk -F- '{print NF}')
  if [ "$nf" != 5 ] && [ "$nf" != 6 ]; then
    log "SKIP  $whl: not a PEP 427 wheel filename"; rc=1; continue
  fi
  name=${stem%%-*}
  version=${stem#*-}; version=${version%%-*}
  norm_name=$(printf '%s' "$name" | tr '[:upper:]' '[:lower:]' | sed -E 's/[-_.]+/-/g')

  if published "$whl"; then
    log "skip  $norm_name==$version already published ($(basename "$whl"))"; continue
  fi
  if [ "$DRY_RUN" = 1 ]; then
    log "dry-run: would upload $norm_name==$version ($(basename "$whl"))"; continue
  fi

  # digests + Requires-Python (from the wheel's METADATA) so the simple index
  # carries data-requires-python and a 3.9 pip never picks a 3.10-only wheel.
  meta=$(python3 -c '
import sys, hashlib, zipfile
d = open(sys.argv[1], "rb").read()
print(hashlib.md5(d).hexdigest()); print(hashlib.sha256(d).hexdigest())
z = zipfile.ZipFile(sys.argv[1])
m = [n for n in z.namelist() if n.endswith(".dist-info/METADATA")][0]
rp = [l for l in z.read(m).decode("utf-8", "replace").splitlines() if l.startswith("Requires-Python:")]
print(rp[0].split(":", 1)[1].strip() if rp else "")' "$whl")
  md5=$(printf '%s\n' "$meta" | sed -n 1p)
  sha256=$(printf '%s\n' "$meta" | sed -n 2p)
  requires_python=$(printf '%s\n' "$meta" | sed -n 3p)
  rp_arg=()
  [ -n "$requires_python" ] && rp_arg=(-F "requires_python=$requires_python")

  http_code=$(curl -sS -K "$netrc_like.pypi" -o "$resp" -w '%{http_code}' \
    -F ':action=file_upload' -F 'protocol_version=1' \
    -F "name=$name" -F "version=$version" \
    -F "md5_digest=$md5" -F "sha256_digest=$sha256" ${rp_arg[@]+"${rp_arg[@]}"} \
    -F "content=@$whl" \
    "$api/projects/$project/packages/pypi")
  case $http_code in
    201) log "ok    uploaded $norm_name==$version ($(basename "$whl"))" ;;
    *)   if grep -q 'already been taken' "$resp"; then
           log "skip  $norm_name==$version already published (race) ($(basename "$whl"))"
         else
           log "FAIL  $whl: HTTP $http_code $(cat "$resp")"; rc=1
         fi ;;
  esac
done
exit $rc
