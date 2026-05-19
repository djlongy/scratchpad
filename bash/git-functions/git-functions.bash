# bash functions for day-to-day git work — interactive branch checkout,
# named stash save/pop/apply with an fzf picker, and pruning of orphaned
# branches. Powered by fzf.
# Append these to your .bashrc file (or `source` it from there).

# Git checkout interactive
gci() {
  if ! command -v fzf &>/dev/null; then
    echo "gci: fzf is not installed. Install it via your OS package manager (e.g. brew/apt/dnf/pacman)." >&2
    return 1
  fi

  local current
  current=$(git branch --show-current)

  local branches
  branches=$(
    {
      git for-each-ref --sort=-committerdate \
        --format='%(refname:short)|%(refname:short)|%(committerdate:relative)|%(subject)' \
        refs/heads

      git for-each-ref --sort=-committerdate \
        --format='%(refname:short)|%(committerdate:relative)|%(subject)' \
        refs/remotes/origin | \
        awk -F'|' '$1 != "origin/HEAD" {
          name = $1
          sub(/^origin\//, "", name)
          print name "|" $1 "|" $2 "|" $3
        }'
    } | awk -F'|' '!seen[$1]++'
  )

  local branch
  branch=$(printf '%s\n' "$branches" | \
    awk -F'|' -v curr="$current" 'BEGIN { OFS="\t" }
      {
        marker = ($1 == curr) ? "* " : "  "
        branch_color = ($1 == curr) ? "\033[1;32m" : "\033[33m"
        branch_col = sprintf("%-40s", $1)
        date_col = sprintf("%-20s", $3)
        branch_display = branch_color marker branch_col "\033[0m"
        date_display = "\033[36m" date_col "\033[0m"
        print $1, $2, branch_display, date_display, $4
      }' | \
    fzf --ansi --height=40% --reverse --delimiter=$'\t' --with-nth=3,4,5 \
      --preview "git log --oneline --graph --date=short --color=always --pretty='format:%C(auto)%cd %h%d %s' {2} --" | \
    awk -F'\t' '{print $1}')

  [[ -n "$branch" ]] && git checkout "$branch" 2>&1
}

# ── git stash wrapper ────────────────────────────────────────────────────────
#
#   gst save <name>     Stash tracked changes with a named message.
#                       Rejects if a stash with that exact name already exists.
#   gst pop  [name]     Pop a stash. With <name>: match by exact message.
#                       Without args: fzf picker with diff preview.
#   gst apply [name]    Apply a stash (keeps it after restoring).
#                       With <name>: match by exact message.
#                       Without args: fzf picker with diff preview.
#   gst list            Show all stashes (alias: ls)
#   gst help            Show usage
#
gst() {
  if ! command -v fzf &>/dev/null; then
    echo "gst: fzf is not installed. Install it via your OS package manager (e.g. brew/apt/dnf/pacman)." >&2
    return 1
  fi

  if ! git rev-parse --git-dir &>/dev/null; then
    echo "gst: not inside a git repository" >&2
    return 1
  fi

  local subcmd="${1:-help}"
  (( $# > 0 )) && shift

  case "$subcmd" in
    save)            _gst_save "$@" ;;
    pop)             _gst_restore pop "$@" ;;
    apply)           _gst_restore apply "$@" ;;
    list|ls)         git stash list ;;
    help|-h|--help)  _gst_help ;;
    *)
      echo "gst: unknown subcommand '${subcmd}'" >&2
      _gst_help >&2
      return 1
      ;;
  esac
}

_gst_help() {
  cat <<'EOF'
gst — named git stash wrapper

Usage:
  gst save <name>     Stash tracked changes with a named message
                      (rejects if a stash named <name> already exists)
  gst pop  [name]     Pop a stash. With <name>: match by exact message.
                      Without args: fzf picker with diff preview.
  gst apply [name]    Apply a stash (keeps it). With <name>: match by exact message.
                      Without args: fzf picker with diff preview.
  gst list            Show all stashes
  gst help            Show this help
EOF
}

_gst_save() {
  local name="${1:-}"
  if [[ -z "$name" ]]; then
    echo "gst save: missing <name>" >&2
    echo "Usage: gst save <name>" >&2
    return 1
  fi

  if git diff --quiet && git diff --cached --quiet; then
    echo "gst save: nothing to stash (no tracked changes)" >&2
    return 1
  fi

  # Reject duplicate names. Stash subjects look like "On <branch>: <name>"
  # (or "WIP on <branch>: <hash> <subject>" for unnamed stashes); strip the
  # prefix up to the first ": " before comparing.
  if git stash list --format='%gs' | sed -E 's/^[^:]*: //' | grep -Fxq -- "$name"; then
    echo "gst save: a stash named '${name}' already exists. Pop it or pick a different name." >&2
    return 1
  fi

  git stash push -m "$name"
}

_gst_restore() {
  local mode="$1"; shift
  local name="${1:-}"

  if [[ -z "$(git stash list)" ]]; then
    echo "gst ${mode}: no stashes" >&2
    return 1
  fi

  local target
  if [[ -n "$name" ]]; then
    local matches count
    matches=$(git stash list --format='%gd%x09%gs' | awk -F'\t' -v n="$name" '
      {
        msg = $2
        sub(/^[^:]*: /, "", msg)
        if (msg == n) print $1
      }
    ')
    count=$(printf '%s' "$matches" | grep -c . || true)
    if [[ "$count" -eq 0 ]]; then
      echo "gst ${mode}: no stash matches name '${name}'" >&2
      return 1
    fi
    if [[ "$count" -gt 1 ]]; then
      echo "gst ${mode}: multiple stashes match '${name}'; run 'gst ${mode}' without a name to choose interactively" >&2
      return 1
    fi
    target="$matches"
  else
    target=$(
      git stash list --format='%gd%x09%cr%x09%gs' |
      fzf --ansi --height=40% --reverse \
          --delimiter=$'\t' --with-nth=1,2,3 \
          --preview='git stash show -p --color=always {1}' \
          --preview-window=right:60%:wrap \
          --prompt="stash ${mode}> " |
      cut -f1
    )
    [[ -z "$target" ]] && return 0
  fi

  git stash "$mode" "$target"
}

# Delete local branches where remote is gone
alias gbp='git branch -vv | grep ": gone]" | awk "{print \$1}" | sed "s/^\* //" | xargs -r git branch -D'