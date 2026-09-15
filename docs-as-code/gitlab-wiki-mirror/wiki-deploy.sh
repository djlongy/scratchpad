#!/usr/bin/env bash
# Two-way sync between docs/ in this repo and the project's GitLab wiki.
#
#   usage: scripts/wiki-deploy.sh DOCS_DIR WIKI_DIR        e.g. scripts/wiki-deploy.sh docs wiki
#
# One cycle:
#   1. clone the wiki into WIKI_DIR (recreated every run; keep it gitignored)
#   2. wiki -> repo: wiki-pull.py turns every wiki commit since the last CI sync into a repo
#      commit by the person who edited the wiki (three-way merge, newer edit wins)
#   3. push those commits to the docs branch with ci.skip (so the pipeline does not loop),
#      rebasing onto the branch tip first if someone pushed meanwhile
#   4. repo -> wiki: wiki-sync.py regenerates the wiki from docs/, committed as the CI author
#   5. push the wiki
# If step 5 is rejected because a person edited the wiki while the cycle ran, the whole cycle
# runs again (bounded), so that edit is pulled too and nothing is overwritten or lost. Step 4
# never runs before step 3 succeeded, so a wiki edit always lands in the repo first.
#
# Identity: this script never uses the caller's ssh keys or the checkout's origin. Everything
# goes over https with a token, as a pipeline service account would, and git is told never to
# prompt (GIT_TERMINAL_PROMPT=0). GitLab CI sets the CI_* variables; locally copy
# .env.example to .env, fill it in, `source .env` (both are gitignored).
#   WIKI_TOKEN          project (or personal) access token: scope write_repository, role
#                       Maintainer if the docs branch is protected. Clones and pushes the wiki
#                       and pushes the repo.
#   CI_JOB_TOKEN        set by CI; pushes the repo when WIKI_TOKEN is not set
#   CI_SERVER_FQDN      gitlab host (or GITLAB_HOST)
#   CI_PROJECT_PATH     group/project (or GITLAB_PROJECT_PATH)
#   CI_DEFAULT_BRANCH   branch the pulled wiki edits are pushed to (default: main)
#   CI_AUTHOR_NAME      author of the CI sync commits (default: "<project> ci"), CI_AUTHOR_EMAIL
#   MKDOCS_CONFIG       mkdocs.yml to read exclude_docs / not_in_nav from (default: mkdocs.yml)
#   WIKI_URL            override the wiki clone URL (tests use a file:// repo)
#   REPO_PUSH_URL       override where pulled edits are pushed (tests use a file:// repo)
#   DRY_RUN=1           clone and report; commit and push nothing
set -euo pipefail

log() { printf '%s\n' "$*"; }
die() { printf 'error: %s\n' "$*" >&2; exit 1; }

# ---- settings ----------------------------------------------------------------------------
configure() {
  docs=${1:?usage: wiki-deploy.sh DOCS_DIR WIKI_DIR}
  wiki_dir=${2:?usage: wiki-deploy.sh DOCS_DIR WIKI_DIR}
  scripts=$(cd "$(dirname "$0")" && pwd)
  docs=$(cd "$docs" && pwd)
  # Resolve the repo from the folder above docs/, so a stray docs/.git cannot answer instead.
  repo=$(git -C "$(dirname "$docs")" rev-parse --show-toplevel)
  guard_nested_repo
  host=${CI_SERVER_FQDN:-${GITLAB_HOST:-}}
  project=${CI_PROJECT_PATH:-${GITLAB_PROJECT_PATH:-}}
  branch=${CI_DEFAULT_BRANCH:-main}
  ci_name=${CI_AUTHOR_NAME:-"${project##*/} ci"}
  ci_email=${CI_AUTHOR_EMAIL:-"ci@${host%%:*}"}
  config=${MKDOCS_CONFIG:-$repo/mkdocs.yml}
  dry=${DRY_RUN:-0}
  attempts=${SYNC_ATTEMPTS:-3}

  wiki_url=${WIKI_URL:-}
  push_url=${REPO_PUSH_URL:-}
  if [ -z "$wiki_url" ] || [ -z "$push_url" ]; then
    [ -n "$host" ] && [ -n "$project" ] || die "set CI_SERVER_FQDN/GITLAB_HOST and CI_PROJECT_PATH/GITLAB_PROJECT_PATH (or WIKI_URL and REPO_PUSH_URL)"
  fi
  if [ -z "$wiki_url" ]; then
    [ -n "${WIKI_TOKEN:-}" ] || die "WIKI_TOKEN is required to clone https://$host/$project.wiki.git"
    wiki_url="https://oauth2@$host/$project.wiki.git"
  fi
  if [ -z "$push_url" ]; then
    if [ -n "${WIKI_TOKEN:-}" ]; then
      push_url="https://oauth2@$host/$project.git"
    elif [ -n "${CI_JOB_TOKEN:-}" ]; then
      push_url="https://gitlab-ci-token@$host/$project.git"
    else
      die "no token to push $project with: set WIKI_TOKEN (or run in CI, or REPO_PUSH_URL)"
    fi
  fi
}

# A stray docs/.git (seen on reused runner build dirs) would make every git command act on
# that nested repo instead of this one. In CI it is junk from an earlier job: drop it.
guard_nested_repo() {
  [ -e "$docs/.git" ] || return 0
  echo "warning: $docs/.git exists (last commit: $(git -C "$docs" log -1 --format='%h %an %s' 2>/dev/null || echo '?'))" >&2
  [ -n "${CI:-}" ] || die "remove $docs/.git first; docs/ must be a plain folder of this repo"
  rm -rf "$docs/.git"
  echo "warning: removed the nested repository (CI build dir)" >&2
}

# ---- credentials ---------------------------------------------------------------------------
# One credential store file, readable only by this process, removed on exit. git picks the
# entry by host and username, and the username is in each URL above, so the wiki token and the
# job token never get confused. Nothing secret appears on a command line or in .git/config.
credentials() {
  export GIT_TERMINAL_PROMPT=0
  export GIT_CONFIG_GLOBAL=/dev/null GIT_CONFIG_SYSTEM=/dev/null   # runner images carry stray config
  umask 077
  cred=$(mktemp)
  trap 'rm -f "$cred"' EXIT
  if [ -n "$host" ]; then
    [ -n "${WIKI_TOKEN:-}" ]   && printf 'https://oauth2:%s@%s\n' "$WIKI_TOKEN" "$host" >> "$cred"
    [ -n "${CI_JOB_TOKEN:-}" ] && printf 'https://gitlab-ci-token:%s@%s\n' "$CI_JOB_TOKEN" "$host" >> "$cred"
  fi
  return 0
}
gitc() { git -c "credential.helper=store --file=$cred" "$@"; }

# ---- the steps -----------------------------------------------------------------------------
clone_wiki() {
  rm -rf "$wiki_dir"
  gitc clone -q "$wiki_url" "$wiki_dir"
  wiki=$(cd "$wiki_dir" && pwd)
  wiki_branch=$(git -C "$wiki" symbolic-ref --short HEAD 2>/dev/null || echo main)
  local commits; commits=$(git -C "$wiki" rev-list --count HEAD 2>/dev/null || echo 0)
  log "wiki:   $wiki_url ($wiki_branch, $commits commits)"
  log "docs:   $docs on $(git -C "$repo" rev-parse --short HEAD)"
}

pull_wiki_edits() {
  git -C "$repo" config user.name  >/dev/null 2>&1 || git -C "$repo" config user.name  "$ci_name"
  git -C "$repo" config user.email >/dev/null 2>&1 || git -C "$repo" config user.email "$ci_email"
  before=$(git -C "$repo" rev-parse HEAD)
  local args=(--ci-author "$ci_name" --ci-email "$ci_email" --repo "$repo")
  [ -f "$config" ] && args+=(--config "$config")
  [ "$dry" = 1 ] && args+=(--dry-run)
  python3 "$scripts/wiki-pull.py" "$docs" "$wiki" ${args[@]+"${args[@]}"}
  after=$(git -C "$repo" rev-parse HEAD)
}

push_repo() {
  [ "$after" != "$before" ] || return 0
  log "pushing $(git -C "$repo" rev-list --count "$before..$after") wiki commit(s) to $branch"
  # ci.skip: the pushed commits are already reflected in the wiki by the sync that follows.
  if ! gitc -C "$repo" push -q -o ci.skip "$push_url" "HEAD:$branch" 2>/dev/null; then
    log "$branch moved while pulling; rebasing the wiki commits onto its tip"
    gitc -C "$repo" fetch -q "$push_url" "$branch" || die "cannot fetch $branch from $push_url"
    git -C "$repo" rebase -q FETCH_HEAD || { git -C "$repo" rebase --abort; die "wiki commits conflict with new commits on $branch; resolve in the repo and re-run"; }
    gitc -C "$repo" push -o ci.skip "$push_url" "HEAD:$branch" || die "push to $branch rejected twice; the wiki is left untouched"
  fi
}

sync_wiki() {
  local args=()
  [ -f "$config" ] && args+=(--config "$config")
  python3 "$scripts/wiki-sync.py" "$docs" "$wiki" ${args[@]+"${args[@]}"}
  git -C "$wiki" config user.name  "$ci_name"
  git -C "$wiki" config user.email "$ci_email"
  git -C "$wiki" add -A
  # Current means: the tree already matches docs/ AND the tip is a CI sync commit. A human
  # commit at the tip (an edit already pulled, or a wipe) still needs the marker commit that
  # wiki-pull.py measures "since the last sync" from.
  local tip_author; tip_author=$(git -C "$wiki" log -1 --format='%an <%ae>' 2>/dev/null || true)
  if git -C "$wiki" diff --cached --quiet && [ "$tip_author" = "$ci_name <$ci_email>" ]; then
    log "wiki already current"
    return 0
  fi
  if [ "$dry" = 1 ]; then
    log "dry-run: wiki would change:"
    git -C "$wiki" diff --cached --stat | tail -20
    return 0
  fi
  # --allow-empty: after a pull the wiki may already hold the content, but the CI commit is
  # the marker wiki-pull.py measures "since the last sync" from, so it is written regardless.
  git -C "$wiki" commit -q --allow-empty -m "Sync docs from $(git -C "$repo" rev-parse --short HEAD)"
  wiki_changed=1
}

push_wiki() {
  [ "${wiki_changed:-0}" = 1 ] || return 0
  if gitc -C "$wiki" push -q origin "HEAD:$wiki_branch" 2>/dev/null; then
    log "wiki updated from $(git -C "$repo" rev-parse --short HEAD)"
  else
    log "wiki push rejected: the wiki changed while this cycle ran"
    return 1
  fi
}

# Called from an `if`, where `set -e` is off, so every step's status is checked by hand:
# a failed repo push must stop the cycle before the wiki is rewritten.
cycle() {
  wiki_changed=0
  clone_wiki || return 1
  pull_wiki_edits || return 1
  if [ "$dry" != 1 ]; then push_repo || return 1; fi
  sync_wiki || return 1
  if [ "$dry" != 1 ]; then push_wiki || return 1; fi
}

main() {
  configure "$@"
  credentials
  local n
  for n in $(seq 1 "$attempts"); do
    if cycle; then return 0; fi
    log "retrying the whole cycle ($n of $attempts) so the new wiki edit is pulled first"
  done
  die "wiki still changing after $attempts attempts; nothing was overwritten, re-run later"
}

main "$@"
