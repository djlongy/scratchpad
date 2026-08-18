#!/usr/bin/env bash
# High side: import drop/ folder into Pulp (pypi/rpm/galaxy/file) + optional Harbor for OCI.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
OTK_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

DROP="${1:-${OTK_DROP:-$OTK_ROOT/drop}}"
PULP_URL="${PULP_URL:-http://127.0.0.1:18080}"
PULP_USER="${PULP_USER:-admin}"
PULP_PASS="${PULP_PASSWORD:-}"
REPO_PREFIX="${PULP_REPO_PREFIX:-otk}"
SKIP_HARBOR="${SKIP_HARBOR:-1}"

export DROP PULP_URL PULP_USER PULP_PASS REPO_PREFIX SKIP_HARBOR OTK_ROOT
# FOREMAN_* is passed through the environment when Satellite ingest is wanted.

python3 "$SCRIPT_DIR/lib/drop_import_pulp.py"
