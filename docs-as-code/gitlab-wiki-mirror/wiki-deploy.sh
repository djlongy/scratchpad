#!/usr/bin/env bash
# Bidirectional sync between docs/ in this repo and the project's GitLab wiki.
#
#   usage: scripts/wiki-deploy.sh DOCS_DIR WIKI_DIR        e.g. scripts/wiki-deploy.sh docs wiki
#
# 1. clone the wiki into WIKI_DIR (recreated every run; keep it gitignored)
# 2. wiki -> repo: wiki-pull.py turns every wiki commit made since the last CI sync into a
#    repo commit authored by the person who edited the wiki, three-way merged, newer edit wins
# 3. push those commits to the docs branch (with ci.skip, so the pipeline does not loop)
# 4. repo -> wiki: wiki-sync.py regenerates the wiki from docs/, committed as the CI author
#
# Step 4 only runs after step 3 succeeded, so a wiki edit is never overwritten before it has
# landed in the repo.
#
# Environment. GitLab CI sets the CI_* variables; locally copy .env.example to .env, fill it
# in and `source .env` (both are gitignored).
#   WIKI_TOKEN          project access token with write_repository (required for https)
#   CI_SERVER_FQDN      gitlab host (or GITLAB_HOST)
#   CI_PROJECT_PATH     group/project (or GITLAB_PROJECT_PATH)
#   CI_DEFAULT_BRANCH   branch the pulled wiki edits are pushed to (default: main)
#   CI_JOB_TOKEN        set by CI; used to push the repo. Locally the checkout's own
#                       `origin` remote and credentials are used instead.
#   WIKI_URL            override the wiki clone URL (tests use a file:// repo)
#   REPO_PUSH_URL       override where pulled edits are pushed (default: CI job-token URL in
#                       CI, `origin` locally)
#   CI_AUTHOR_NAME      author name of the CI sync commits (default: "<project> ci")
#   CI_AUTHOR_EMAIL     its email (default: ci@<host>)
#   MKDOCS_CONFIG       mkdocs.yml to read exclude_docs / not_in_nav from (default: mkdocs.yml)
#   DRY_RUN=1           clone and report; commit and push nothing
set -euo pipefail

docs=${1:?usage: wiki-deploy.sh DOCS_DIR WIKI_DIR}
wiki=${2:?usage: wiki-deploy.sh DOCS_DIR WIKI_DIR}
scripts=$(cd "$(dirname "$0")" && pwd)
docs=$(cd "$docs" && pwd)
# Resolve the repo from the folder above docs/, so a stray docs/.git cannot answer instead.
repo=$(git -C "$(dirname "$docs")" rev-parse --show-toplevel)
# A stray docs/.git (seen on reused runner build dirs) would make every git command below
# act on that nested repo instead of this one. In CI it is junk from an earlier job: drop it.
if [ -e "$docs/.git" ]; then
  echo "warning: $docs/.git exists (last commit: $(git -C "$docs" log -1 --format='%h %an %s' 2>/dev/null || echo '?'))" >&2
  if [ -n "${CI:-}" ]; then
    rm -rf "$docs/.git"
    echo "warning: removed the nested repository (CI build dir)" >&2
  else
    echo "error: remove $docs/.git first; docs/ must be a plain folder of this repo" >&2
    exit 1
  fi
fi

host=${CI_SERVER_FQDN:-${GITLAB_HOST:-}}
project=${CI_PROJECT_PATH:-${GITLAB_PROJECT_PATH:-}}
branch=${CI_DEFAULT_BRANCH:-main}
wiki_url=${WIKI_URL:-}
if [ -z "$wiki_url" ]; then
  [ -n "$host" ] && [ -n "$project" ] || { echo "error: set CI_SERVER_FQDN/GITLAB_HOST and CI_PROJECT_PATH/GITLAB_PROJECT_PATH (or WIKI_URL)" >&2; exit 1; }
  [ -n "${WIKI_TOKEN:-}" ] || { echo "error: WIKI_TOKEN is required to clone https://$host/$project.wiki.git" >&2; exit 1; }
  wiki_url="https://$host/$project.wiki.git"
fi
ci_name=${CI_AUTHOR_NAME:-"${project##*/} ci"}
ci_email=${CI_AUTHOR_EMAIL:-"ci@${host%%:*}"}
config=${MKDOCS_CONFIG:-$repo/mkdocs.yml}
dry=${DRY_RUN:-0}
log() { printf '%s\n' "$*"; }

# Runner images can carry a global git config that breaks cloning; start from none.
export GIT_CONFIG_GLOBAL=/dev/null GIT_CONFIG_SYSTEM=/dev/null
export GIT_TERMINAL_PROMPT=0

# The token never goes on a command line or into .git/config: git reads it from a
# credential store file that only this process can read and that is removed on exit.
umask 077
cred=$(mktemp)
trap 'rm -f "$cred"' EXIT
if [ -n "${WIKI_TOKEN:-}" ] && [ -n "$host" ]; then
  printf 'https://oauth2:%s@%s\n' "$WIKI_TOKEN" "$host" > "$cred"
fi
gitc() { git -c "credential.helper=store --file=$cred" "$@"; }

# ---- 1. fresh wiki clone --------------------------------------------------------------------
rm -rf "$wiki"
gitc clone -q "$wiki_url" "$wiki"
wiki=$(cd "$wiki" && pwd)
wiki_branch=$(git -C "$wiki" symbolic-ref --short HEAD 2>/dev/null || echo main)
log "wiki:   $wiki_url ($wiki_branch, $(git -C "$wiki" rev-list --count HEAD) commits)"
log "docs:   $docs on $(git -C "$repo" rev-parse --short HEAD)"

# ---- 2. wiki -> repo ------------------------------------------------------------------------
git -C "$repo" config user.name  >/dev/null 2>&1 || git -C "$repo" config user.name  "$ci_name"
git -C "$repo" config user.email >/dev/null 2>&1 || git -C "$repo" config user.email "$ci_email"
before=$(git -C "$repo" rev-parse HEAD)
pull_args=(--ci-author "$ci_name" --repo "$repo")
[ -f "$config" ] && pull_args+=(--config "$config")
[ "$dry" = 1 ] && pull_args+=(--dry-run)
python3 "$scripts/wiki-pull.py" "$docs" "$wiki" "${pull_args[@]}"
after=$(git -C "$repo" rev-parse HEAD)

# ---- 3. push pulled edits ---------------------------------------------------------------------
if [ "$after" != "$before" ]; then
  push_url=${REPO_PUSH_URL:-}
  if [ -z "$push_url" ]; then
    if [ -n "${CI_JOB_TOKEN:-}" ]; then
      push_url="https://gitlab-ci-token:${CI_JOB_TOKEN}@${host}/${project}.git"
    else
      push_url=origin
    fi
  fi
  log "pushing $(git -C "$repo" rev-list --count "$before..$after") wiki commit(s) to $branch"
  # ci.skip: the pushed commits are already reflected in the wiki by step 4 below.
  git -C "$repo" push -q -o ci.skip "$push_url" "HEAD:$branch"
fi

# ---- 4. repo -> wiki ------------------------------------------------------------------------
sync_args=()
[ -f "$config" ] && sync_args+=(--config "$config")
python3 "$scripts/wiki-sync.py" "$docs" "$wiki" "${sync_args[@]}"
git -C "$wiki" config user.name  "$ci_name"
git -C "$wiki" config user.email "$ci_email"
git -C "$wiki" add -A
if git -C "$wiki" diff --cached --quiet && [ "$after" = "$before" ]; then
  log "wiki already current"
elif [ "$dry" = 1 ]; then
  log "dry-run: wiki would change:"
  git -C "$wiki" diff --cached --stat | tail -20
else
  # --allow-empty: after a pull the wiki may already hold the content, but the CI commit is
  # the marker wiki-pull.py measures "since the last sync" from, so it is written regardless.
  git -C "$wiki" commit -q --allow-empty -m "Sync docs from $(git -C "$repo" rev-parse --short HEAD)"
  gitc -C "$wiki" push -q origin "HEAD:$wiki_branch"
  log "wiki updated from $(git -C "$repo" rev-parse --short HEAD)"
fi
