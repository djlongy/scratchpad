#!/usr/bin/env bash
# Write a minimal Quay config.yaml for one side.
#   usage: scripts/quay-config.sh SIDE HOSTNAME[:PORT]        e.g. scripts/quay-config.sh low localhost:18081
# Keys are random per run and are the only secrets in the file; the DB password matches compose.yaml.
set -euo pipefail
side=${1:?side (low|high)}; host=${2:?hostname[:port]}
here=$(cd "$(dirname "$0")/.." && pwd)
dir=$here/quay/$side
mkdir -p "$dir"
rand() { python3 -c 'import secrets; print(secrets.token_urlsafe(48))'; }
cat > "$dir/config.yaml" <<EOF
SERVER_HOSTNAME: "$host"
PREFERRED_URL_SCHEME: http
SETUP_COMPLETE: true
AUTHENTICATION_TYPE: Database
FEATURE_USER_INITIALIZE: true      # POST /api/v1/user/initialize creates the first superuser
FEATURE_USER_CREATION: false
FEATURE_DIRECT_LOGIN: true
FEATURE_MAILING: false
FEATURE_SECURITY_SCANNER: false
FEATURE_PROXY_CACHE: true          # proxy-cache organisations: needed to pull upstream through this registry
SUPER_USERS:
  - admin
SECRET_KEY: "$(rand)"
DATABASE_SECRET_KEY: "$(rand)"
DB_URI: "postgresql://quay:quaypass@${side}-db:5432/quay"
DB_CONNECTION_ARGS:
  autorollback: true
  threadlocals: true
BUILDLOGS_REDIS:
  host: ${side}-redis
  port: 6379
USER_EVENTS_REDIS:
  host: ${side}-redis
  port: 6379
DISTRIBUTED_STORAGE_CONFIG:
  default:
    - LocalStorage
    - storage_path: /datastorage/registry
DISTRIBUTED_STORAGE_PREFERENCE:
  - default
DISTRIBUTED_STORAGE_DEFAULT_LOCATIONS: []
LOGS_MODEL: database
TESTING: false
EOF
chmod 644 "$dir/config.yaml"
echo "wrote $dir/config.yaml for $host"
