#!/usr/bin/env bash
# First-time wiring so a project's wiki edits reach its pipeline.
#
#   usage: GITLAB_HOST=gitlab.example GITLAB_TOKEN=<api-scope PAT> \
#          runtime/wiki/wiki-bootstrap.sh group/project
#
# Two things, only where they are missing:
#   1. the wiki-sync webhook and the pipeline trigger token it carries, by running
#      reconcile.py — the same code the sync job runs on every default-branch push,
#      so there is one implementation of the wiring and not two that can disagree
#   2. an hourly pipeline schedule on the default branch, "wiki sync safety net",
#      which catches an edit whose webhook delivery was missed
#
# Run it again any time: every step looks the thing up first and reports "already
# present". Once the first sync pipeline has run with `webhook-reconcile: on`, the
# pipeline maintains step 1 by itself and this script is only needed to create the
# schedule, or to wire a project whose token is too narrow for the job to do it.
#
# GITLAB_TOKEN needs the api scope and the Maintainer role on the project. The
# trigger token it creates is never printed, and neither is the webhook URL that
# carries it.
set -euo pipefail

project=${1:?usage: wiki-bootstrap.sh group/project}
host=${GITLAB_HOST:?set GITLAB_HOST, e.g. gitlab.example}
: "${GITLAB_TOKEN:?set GITLAB_TOKEN to an api-scope token}"

api="https://$host/api/v4"
here=$(cd "$(dirname "$0")" && pwd)
umask 077
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT

# curl with the token in a header file, so it never reaches a command line or the job log
printf 'header = "PRIVATE-TOKEN: %s"\n' "$GITLAB_TOKEN" > "$work/curlrc"
gl() { curl -fsS --config "$work/curlrc" "$@"; }
enc() { python3 -c 'import sys,urllib.parse;print(urllib.parse.quote(sys.argv[1],safe=""))' "$1"; }
jqf() { python3 -c 'import json,sys;print(json.load(open(sys.argv[1])).get(sys.argv[2],""))' "$1" "$2"; }

gl "$api/projects/$(enc "$project")" > "$work/p.json"
pid=$(jqf "$work/p.json" id)
branch=$(jqf "$work/p.json" default_branch)
echo "project $project (id $pid, default branch $branch)"

# ---- 1. trigger token and the webhook that carries it --------------------------------------
# reconcile.py reads the token from the environment rather than an argument, because
# an argument is visible in `ps` and in a subprocess timeout's traceback.
WIKI_ADMIN_TOKEN="$GITLAB_TOKEN" \
  python3 "$here/reconcile.py" --api-url "$api" --project-id "$pid" --default-branch "$branch"

# ---- 2. hourly schedule, the safety net if a webhook delivery is ever missed ----------------
gl "$api/projects/$pid/pipeline_schedules" > "$work/sched.json"
sid=$(python3 -c 'import json,sys
print(next((s["id"] for s in json.load(open(sys.argv[1])) if s.get("description")=="wiki sync safety net"), ""))' "$work/sched.json")
if [ -n "$sid" ]; then
  echo "  schedule already present (id $sid)"
else
  gl -X POST --data-urlencode "description=wiki sync safety net" \
     --data-urlencode "ref=$branch" --data-urlencode "cron=0 * * * *" \
     "$api/projects/$pid/pipeline_schedules" > "$work/sout.json"
  echo "  schedule created (id $(jqf "$work/sout.json" id), hourly on $branch)"
fi

cat <<EOF

still yours to do, once:
  - $branch is a protected branch (a protected CI variable is invisible to any other ref)
  - the group or project CI variable WIKI_TOKEN is visible to this project
  - $project has a docs folder with at least an index.md, and .gitlab-ci.yml includes
    /pipelines/docs-wiki.yml from the shared CI project at an approved tag, or
    /templates/docs-wiki-sync/template.yml when the project runs checks of its own
    and owns its stages: and workflow:
prove it: edit a wiki page in the UI, then watch the pipeline at
  https://$host/$project/-/pipelines
EOF
