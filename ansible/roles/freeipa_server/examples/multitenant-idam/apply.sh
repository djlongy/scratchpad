#!/usr/bin/env bash
# apply.sh <tenant> [<tenant> ...] [-- <extra ansible-playbook args>]
#
# Applies one or more tenants against the shared realm. Selection is by inventory
# composition: each named tenant adds `-i inventories/<tenant>`, always alongside
# `-i inventories/_common`. One tenant = isolated + fast; several = a union/audit
# run. There is no -e selector and no count to maintain.
#
#   ./apply.sh acme                  # team ACME only
#   ./apply.sh acme globex           # union (audit) over both
#   ./apply.sh acme -- --check       # pass-through args after --
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"

inv=(-i "$here/inventories/_common")
extra=()
seen_sep=0
for arg in "$@"; do
  if [[ "$seen_sep" == 1 ]]; then extra+=("$arg"); continue; fi
  if [[ "$arg" == "--" ]]; then seen_sep=1; continue; fi
  dir="$here/inventories/$arg"
  [[ -d "$dir" ]] || { echo "unknown tenant '$arg' (no $dir)" >&2; exit 2; }
  inv+=(-i "$dir")
done
[[ ${#inv[@]} -gt 2 ]] || { echo "usage: apply.sh <tenant> [<tenant> ...] [-- args]" >&2; exit 1; }

exec ansible-playbook "${inv[@]}" "$here/site.yml" --tags idam "${extra[@]}"
